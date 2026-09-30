"""Per-request audit log of API calls (``api_service__audit_log``).

Request bodies are never stored as sent. Write requests keep a short,
redacted summary: values of keys that look like credentials are replaced,
page content is reduced to its length, long strings are cut and the whole
summary is capped, so the log cannot become a second copy of passwords or
wiki content.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ....core.timeutil import now_sql, sql_in
from ...db import db

SUMMARY_LIMIT = 2000
STRING_LIMIT = 120
LIST_LIMIT = 20
DEPTH_LIMIT = 3
RETENTION_DAYS = 365
REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"pass|secret|token|key|auth|credential", re.IGNORECASE)
_BULKY_KEYS = frozenset({"content", "builder_json", "bio"})


def _summarise(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if depth >= DEPTH_LIMIT:
            return "{…}"
        return {str(k)[:64]: _summarise_field(str(k), v, depth + 1) for k, v in list(value.items())[:LIST_LIMIT]}
    if isinstance(value, list):
        if depth >= DEPTH_LIMIT:
            return "[…]"
        return [_summarise(item, depth + 1) for item in value[:LIST_LIMIT]]
    if isinstance(value, str) and len(value) > STRING_LIMIT:
        return value[:STRING_LIMIT] + "…"
    return value


def _summarise_field(key: str, value: Any, depth: int) -> Any:
    if _SECRET_KEY.search(key):
        return REDACTED
    if key in _BULKY_KEYS and isinstance(value, str):
        return f"[{len(value)} characters]"
    return _summarise(value, depth)


def summarise_body(body: Any) -> str:
    """A redacted, bounded JSON summary of a request body ('' when there is none)."""
    if body is None:
        return ""
    text = json.dumps(_summarise(body), ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 1] + "…"


def record(*, token_id: int | None, user: dict[str, Any], endpoint: str, method: str, status: int,
           ip: str, body: Any, duration_ms: int) -> None:
    db.insert("api_service__audit_log", {
        "token_id": token_id,
        "user_id": user["id"],
        "username": user.get("username", ""),
        "endpoint": endpoint[:500],
        "method": method[:10],
        "status_code": status,
        "ip_address": ip,
        "request_body": summarise_body(body),
        "duration_ms": duration_ms,
        "created_at": now_sql(),
    })


def entries(*, limit: int = 100, offset: int = 0, user_id: str | None = None) -> list[dict[str, Any]]:
    where, params = ("WHERE user_id = ?", [user_id]) if user_id else ("", [])
    return db.all(
        f"SELECT id, token_id, user_id, username, endpoint, method, status_code, ip_address, request_body, "
        f"duration_ms, created_at FROM api_service__audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )


def count(user_id: str | None = None) -> int:
    if user_id:
        return int(db.scalar("SELECT COUNT(*) FROM api_service__audit_log WHERE user_id = ?", (user_id,), default=0))
    return int(db.scalar("SELECT COUNT(*) FROM api_service__audit_log", default=0))


def clear(before_days: int) -> int:
    """Delete entries older than *before_days* days; return how many."""
    return db.execute("DELETE FROM api_service__audit_log WHERE created_at < ?",
                      (sql_in(days=-before_days),)).rowcount


def prune() -> None:
    """Background job: keep a year of audit history and an hour of rate-limit counters."""
    clear(RETENTION_DAYS)
    db.execute("DELETE FROM rate_limit_hits WHERE bucket = 'api_service' AND hit_at < ?", (sql_in(hours=-1),))
