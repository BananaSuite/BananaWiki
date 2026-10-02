"""Retention cleanup and housekeeping jobs.

``chat.retention`` runs hourly and applies the administrator's retention
policy when the schedule says it is due (every ``chat_cleanup_frequency_days``
days at ``chat_cleanup_hour`` in the site time zone). Direct messages and
group chats have separate policies for messages and for attachments.

``chat.housekeeping`` runs daily. It erases what 1.4 kept of deleted
messages, and removes attachment files (and their legacy blob copies) that no
message points at any more, for example after an account was deleted.

Unlike 1.4, removing an attachment always removes its ``file_blobs`` copy too.
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
from . import dms, store
from .store import DM, GROUP

log = logging.getLogger("bananawiki.chat")

SCOPES = ("dm", "group")
FIELDS = ("auto_clear_messages", "auto_clear_attachments", "message_retention_days", "attachment_retention_days")
# Values 1.4 migrations wrote into the split columns; rows still holding them
# were never saved from the admin page, so the legacy single policy applies.
_SPLIT_DEFAULTS = {"auto_clear_messages": 0, "auto_clear_attachments": 1, "message_retention_days": 0,
                   "attachment_retention_days": 7}
ORPHAN_MIN_AGE_SECONDS = 86400


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def policy() -> dict[str, dict[str, int]]:
    """Effective DM and group retention settings, honouring the 1.4 single policy."""
    row = settings.load()
    split = bool(row.get("chat_cleanup_split_configured"))
    result: dict[str, dict[str, int]] = {}
    for scope in SCOPES:
        resolved = {}
        for field in FIELDS:
            specific = row.get(f"chat_{scope}_{field}")
            legacy = row.get(f"chat_{field}")
            if split or (specific is not None and specific != _SPLIT_DEFAULTS[field]):
                resolved[field] = _int(specific, _SPLIT_DEFAULTS[field])
            elif legacy is not None:
                resolved[field] = _int(legacy)
            else:
                resolved[field] = _SPLIT_DEFAULTS[field]
        result[scope] = resolved
    return result


def policy_columns() -> dict[str, int]:
    """The effective policy keyed by the per-scope setting columns (for the settings form)."""
    return {f"chat_{scope}_{field}": value for scope, rules in policy().items() for field, value in rules.items()}


def active_rules(scope: str) -> dict[str, int]:
    """Retention periods in days that apply to *scope* now (empty when cleanup is off)."""
    if not settings.get("chat_cleanup_enabled"):
        return {}
    rules = policy()[scope]
    active = {}
    if rules["auto_clear_messages"] and rules["message_retention_days"] > 0:
        active["messages"] = rules["message_retention_days"]
    if rules["auto_clear_attachments"] and rules["attachment_retention_days"] > 0:
        active["attachments"] = rules["attachment_retention_days"]
    return active


def next_run(now: datetime | None = None) -> datetime | None:
    """When the next cleanup is due (UTC), or None while cleanup is off."""
    if not settings.get("chat_cleanup_enabled"):
        return None
    now = now or utcnow()
    zone = site_timezone()
    hour = min(23, max(0, _int(settings.get("chat_cleanup_hour"), 3)))
    every = max(1, _int(settings.get("chat_cleanup_frequency_days"), 7))
    last = parse(settings.get("last_chat_cleanup_at"))
    local_now = now.astimezone(zone)
    if last is None:
        target = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target > local_now:
            return target.astimezone(now.tzinfo)
        return now
    target = (last.astimezone(zone) + timedelta(days=every)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return target.astimezone(now.tzinfo)


def run_retention(*, force: bool = False) -> dict[str, int] | None:
    """Apply the retention policy when it is due (or now with *force*); return a summary."""
    due = next_run()
    if due is None or (not force and due > utcnow()):
        return None
    summary = {"messages": 0, "attachments": 0, "chats": 0}
    files: list[str] = []
    with db.transaction():
        for scope, target in (("dm", DM), ("group", GROUP)):
            rules = active_rules(scope)
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
