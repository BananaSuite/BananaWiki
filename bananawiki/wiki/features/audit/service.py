"""The audit log: who did what, when and from where.

Other features record security-relevant actions with :func:`record`::

    from ..audit import record

    record("kanban.board_deleted", target_type="board", target_id=board["id"],
           details={"title": board["title"]})

The acting account and the client IP default to the current request (None
outside one). ``details`` must be JSON-serialisable and must never contain
secrets (passwords, tokens). Nothing is written while the audit feature is
switched off, and a failure to write is logged, never raised, so auditing
can never break the action it describes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import has_request_context

from ....core.timeutil import now_sql, sql_in
from ....core.web import client_ip
from ... import auth, registry, settings
from ...db import db

log = logging.getLogger("bananawiki.audit")

FEATURE_ID = "audit"
PAGE_SIZE = 50
MAX_DETAILS_BYTES = 4000
RETENTION_BOUNDS = (0, 3650)  # days; 0 keeps entries forever
_CURRENT = object()


def _request_actor() -> str | None:
    if not has_request_context():
        return None
    user = auth.real_user()
    return str(user["id"]) if user else None


def _details_json(details: dict[str, Any] | None) -> str:
    text = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > MAX_DETAILS_BYTES:
        text = json.dumps({"truncated": True}, sort_keys=True)
    return text


def record(
    action: str,
    *,
    actor_id: Any = _CURRENT,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
    ip: Any = _CURRENT,
) -> None:
    """Append one entry to the audit log (see the module docstring)."""
    try:
        if not registry.is_enabled(FEATURE_ID):
            return
        actor = _request_actor() if actor_id is _CURRENT else actor_id
        address = (client_ip() if has_request_context() else None) if ip is _CURRENT else ip
        db.insert("audit_log", {
            "created_at": now_sql(),
            "actor_id": None if actor is None else str(actor),
            "action": str(action)[:100],
            "target_type": target_type,
            "target_id": None if target_id is None else str(target_id),
            "ip": address,
            "details": _details_json(details),
        })
    except Exception:  # noqa: BLE001 - auditing must never break the audited action
        log.exception("Could not record audit entry %s", action)


def actions() -> list[str]:
    return db.column("SELECT DISTINCT action FROM audit_log ORDER BY action")


def _filters(action: str, actor: str) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if action:
        clauses.append("a.action = ?")
        params.append(action)
    if actor:
        clauses.append("(u.username = ? COLLATE NOCASE OR a.actor_id = ?)")
        params.extend([actor, actor])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def entries(*, action: str = "", actor: str = "", page: int = 1) -> tuple[list[dict[str, Any]], int, int]:
    """One page of entries (newest first), the page shown and the number of pages."""
    where, params = _filters(action, actor)
    base = f"FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id{where}"
    total = int(db.scalar(f"SELECT COUNT(*) {base}", params, default=0))
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = db.all(
        f"SELECT a.*, u.username AS actor_username {base} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    for row in rows:
        try:
            parsed = json.loads(row["details"] or "{}")
        except ValueError:
            parsed = {}
        row["details"] = parsed if isinstance(parsed, dict) else {}
    return rows, page, pages


def retention_days() -> int:
    try:
        return int(settings.get("audit_log_retention_days", 365))
    except (TypeError, ValueError):
        return 365


def prune() -> int:
    """Delete entries older than the retention period (background job)."""
    days = retention_days()
    if days <= 0:
        return 0
    return db.execute("DELETE FROM audit_log WHERE created_at < ?", (sql_in(days=-days),)).rowcount
