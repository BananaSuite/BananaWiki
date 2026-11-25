"""Retention cleanup and housekeeping jobs.

``chat.retention`` runs hourly and, once a day from 03:00 site time, deletes
the messages and the files older than the administrator's retention: four
numbers of days (messages and files, of direct messages and of groups); 0
keeps them. Version 24 folded the switches, the schedule and the 1.4 single
policy into those four numbers.

``chat.housekeeping`` runs daily. It erases what 1.4 kept of deleted
messages, and removes attachment files (and their legacy blob copies) that no
message points at any more, for example after an account was deleted.
Removing an attachment always removes its ``file_blobs`` copy too.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from ....core.timeutil import now_sql, parse, sql_in, utcnow
from ... import settings, storage
from ...db import db
from ...templating import site_timezone
from ..audit import record
from . import dms, store
from .store import DM, GROUP

log = logging.getLogger("bananawiki.chat")

SCOPES = ("dm", "group")
RETENTION_COLUMNS = tuple(f"chat_{scope}_{kind}_retention_days" for scope in SCOPES
                          for kind in ("message", "attachment"))
CLEANUP_HOUR = 3  # site time
ORPHAN_MIN_AGE_SECONDS = 86400


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def active_rules(scope: str) -> dict[str, int]:
    """Retention periods in days that apply to *scope* (empty: everything is kept)."""
    row = settings.load()
    active = {}
    for kind, name in (("message", "messages"), ("attachment", "attachments")):
        days = _int(row.get(f"chat_{scope}_{kind}_retention_days"))
        if days > 0:
            active[name] = days
    return active


def next_run(now: datetime | None = None) -> datetime | None:
    """When the next cleanup is due (UTC), or None while nothing is ever deleted."""
    if not any(active_rules(scope) for scope in SCOPES):
        return None
    now = now or utcnow()
    zone = site_timezone()
    last = parse(settings.get("last_chat_cleanup_at"))
    local_now = now.astimezone(zone)
    if last is None:
        target = local_now.replace(hour=CLEANUP_HOUR, minute=0, second=0, microsecond=0)
        if target > local_now:
            return target.astimezone(now.tzinfo)
        return now
    target = (last.astimezone(zone) + timedelta(days=1)).replace(hour=CLEANUP_HOUR, minute=0, second=0, microsecond=0)
    return target.astimezone(now.tzinfo)


def run_retention(*, force: bool = False) -> dict[str, int] | None:
    """Apply the retention policy when it is due (or now with *force*); return a summary."""
    due = next_run()
    if due is None or (not force and due > utcnow()):
        return None
    summary = {"messages": 0, "attachments": 0, "chats": 0}
    files: list[str] = []
    applied: dict[str, dict[str, int]] = {}
    with db.transaction():
        for scope, target in (("dm", DM), ("group", GROUP)):
            rules = active_rules(scope)
            if rules:
                applied[scope] = dict(rules)
            if "attachments" in rules:
                removed = store.delete_attachments_before(target, sql_in(days=-rules["attachments"]))
                summary["attachments"] += len(removed)
                files += removed
            if "messages" in rules:
                cutoff = sql_in(days=-rules["messages"])
                deleted, removed = store.delete_messages_before(target, cutoff)
                summary["messages"] += deleted
                summary["attachments"] += len(removed)
                files += removed
                if scope == "dm":
                    summary["chats"] += dms.delete_empty_before(cutoff)
        settings.update({"last_chat_cleanup_at": now_sql()}, internal=True)
    store.remove_files(files)
    log.info("Chat retention cleanup: %s", summary)
    if any(summary.values()):
        # Members' messages erased by the policy an administrator set: as auditable as clearing one conversation.
        record("chat.retention_applied", details={**summary, "rules_days": applied})
    return summary


def retention_job() -> None:
    run_retention()


def _orphan_files(min_age_seconds: int) -> list[str]:
    folder = storage.folder_path(store.FOLDER)
    known = set(db.column("SELECT filename FROM chat_attachments"))
    known.update(db.column("SELECT filename FROM group_attachments"))
    cutoff = time.time() - min_age_seconds
    orphans = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.name in known or not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                orphans.append(entry.name)
    return orphans


def housekeeping(*, min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS) -> dict[str, int]:
    """Erase leftovers of deleted messages and attachment files nothing refers to."""
    with db.transaction():
        files = store.purge_deleted(DM) + store.purge_deleted(GROUP)
        # Usage needs to outlive its file, but only for the sliding daily
        # window. Pruning shares the writer transaction used for uploads.
        db.execute("DELETE FROM chat__upload_usage WHERE created_at < ?", (sql_in(days=-1),))
    orphans = _orphan_files(min_age_seconds)
    blobs = 0
    if orphans:
        condition = store.unreferenced_blob_filter()
        with db.transaction():
            for start in range(0, len(orphans), 500):
                chunk = orphans[start:start + 500]
                blobs += db.execute(
                    f"DELETE FROM file_blobs WHERE filename IN ({','.join('?' * len(chunk))}) AND {condition}",
                    chunk,
                ).rowcount
    store.remove_files(files + orphans)
    summary = {"purged_attachments": len(files), "orphan_files": len(orphans), "orphan_blobs": blobs}
    if any(summary.values()):
        log.info("Chat housekeeping: %s", summary)
    return summary


def housekeeping_job() -> None:
    housekeeping()
