"""The signed-in account's own login sessions."""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql
from ...db import db
from ...i18n import t

HISTORY_LIMIT = 50

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Opera", "Opera"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("CriOS/", "Chrome"), ("Safari/", "Safari"))
_PLATFORMS = (("iPhone", "iOS"), ("iPad", "iOS"), ("Android", "Android"), ("Windows", "Windows"),
              ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"))


def describe_user_agent(user_agent: str | None) -> str:
    """A short "Browser on System" label for a stored user agent."""
    ua = user_agent or ""
    browser = next((name for marker, name in _BROWSERS if marker in ua), t("users.sessions.unknown_browser"))
    platform = next((name for marker, name in _PLATFORMS if marker in ua), t("users.sessions.unknown_device"))
    return t("users.sessions.device", browser=browser, platform=platform)


def _decorate(rows: list[dict[str, Any]], current_id: str | None) -> list[dict[str, Any]]:
    for row in rows:
        row["device"] = describe_user_agent(row["user_agent"])
        row["is_current"] = row["id"] == current_id
        row["ended_at"] = row.get("revoked_at") or row["expires_at"]
    return rows


_COLUMNS = "id, created_at, last_seen_at, expires_at, revoked_at, remember_me, auth_method, last_ip, user_agent"


def active(user_id: str, current_id: str | None) -> list[dict[str, Any]]:
    rows = db.all(
        f"SELECT {_COLUMNS} FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? "
        "ORDER BY last_seen_at DESC",
        (user_id, now_sql()),
    )
    return _decorate(rows, current_id)


def history(user_id: str) -> list[dict[str, Any]]:
    rows = db.all(
        f"SELECT {_COLUMNS} FROM user_sessions WHERE user_id = ? AND (revoked_at IS NOT NULL OR expires_at <= ?) "
        "ORDER BY COALESCE(revoked_at, expires_at) DESC LIMIT ?",
        (user_id, now_sql(), HISTORY_LIMIT),
    )
    return _decorate(rows, None)


def revoke(user_id: str, session_id: str) -> bool:
    """End one of *user_id*'s own sessions; False when it is not theirs or already ended."""
    cursor = db.execute(
        "UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (now_sql(), session_id, user_id),
    )
    return cursor.rowcount == 1


def clear_history(user_id: str) -> int:
    cursor = db.execute(
        "DELETE FROM user_sessions WHERE user_id = ? AND (revoked_at IS NOT NULL OR expires_at <= ?)",
        (user_id, now_sql()),
    )
    return cursor.rowcount
