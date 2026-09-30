"""Userbot automation: an account driven by a script through ``/api/v1/userbot/*``.

Automation mode is a flag on the account (``users.userbot_enabled``) plus one
API token named ``userbot`` with read/write access to the ``userbot`` scope.
Administrators can lock the mode on or off (``users.userbot_mode_lock``).
Other features (profiles, the admin user list) read the same columns.
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql
from ...db import db
from . import tokens

LOCK_MODES = ("unlocked", "force_enabled", "force_disabled")
MAX_REAL_NAME = 100
MAX_BIO = 500


class UserbotError(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def token_info(user_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT id, created_at, last_used_at FROM api_service__tokens "
        "WHERE user_id = ? AND name = ? AND active = 1 ORDER BY id DESC LIMIT 1",
        (user_id, tokens.USERBOT_TOKEN_NAME),
    )


def _revoke_tokens(user_id: str) -> None:
    db.execute("UPDATE api_service__tokens SET active = 0 WHERE user_id = ? AND name = ? AND active = 1",
               (user_id, tokens.USERBOT_TOKEN_NAME))


def enable(user: dict[str, Any]) -> str:
    """Turn automation on (or issue a fresh key while it is on); return the raw key."""
    if not tokens.service_settings()["enabled"]:
        raise UserbotError("api_service.flash.service_disabled")
    if not tokens.has_api_access(user):
        raise UserbotError("api_service.flash.no_api_access")
    if (user.get("userbot_mode_lock") or "unlocked") == "force_disabled":
        raise UserbotError("api_service.userbot.locked_off")
    grant = tokens.Grant(True, True, ("userbot",))
    with db.transaction():
        _revoke_tokens(user["id"])
        raw, _token_id = tokens.create(user["id"], name=tokens.USERBOT_TOKEN_NAME, grant=grant)
        if not user.get("userbot_enabled"):
            db.execute("UPDATE users SET userbot_enabled = 1, userbot_enable_count = userbot_enable_count + 1 "
                       "WHERE id = ?", (user["id"],))
    return raw


def disable(user: dict[str, Any]) -> None:
    if (user.get("userbot_mode_lock") or "unlocked") == "force_enabled":
        raise UserbotError("api_service.userbot.locked_on")
    with db.transaction():
        _revoke_tokens(user["id"])
        if user.get("userbot_enabled"):
            db.execute("UPDATE users SET userbot_enabled = 0, userbot_disable_count = userbot_disable_count + 1 "
                       "WHERE id = ?", (user["id"],))


def set_lock(user: dict[str, Any], mode: str) -> None:
    """Administrator lock. Locking off revokes the key; locking on lets the owner issue one."""
    if mode not in LOCK_MODES:
        raise UserbotError("api_service.userbot.invalid_lock")
    with db.transaction():
        db.execute("UPDATE users SET userbot_mode_lock = ? WHERE id = ?", (mode, user["id"]))
        if mode == "force_disabled":
            _revoke_tokens(user["id"])
            db.execute("UPDATE users SET userbot_enabled = 0 WHERE id = ?", (user["id"],))
        elif mode == "force_enabled":
            db.execute("UPDATE users SET userbot_enabled = 1 WHERE id = ?", (user["id"],))


def profile(user_id: str) -> dict[str, Any]:
    row = db.one("SELECT real_name, bio, page_published, page_disabled_by_admin FROM user_profiles "
                 "WHERE user_id = ?", (user_id,))
    return {
        "real_name": row["real_name"] if row else "",
        "bio": row["bio"] if row else "",
        "page_published": bool(row["page_published"]) if row else False,
        "page_disabled_by_admin": bool(row["page_disabled_by_admin"]) if row else False,
    }


def update_profile(user_id: str, *, real_name: str, bio: str, page_published: bool | None) -> dict[str, Any]:
    current = profile(user_id)
    if page_published and current["page_disabled_by_admin"]:
        raise UserbotError("api_service.userbot.profile_disabled")
    published = current["page_published"] if page_published is None else page_published
    db.execute(
        "INSERT INTO user_profiles (user_id, real_name, bio, page_published, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET real_name = excluded.real_name, bio = excluded.bio, "
        "page_published = excluded.page_published, updated_at = excluded.updated_at",
        (user_id, real_name[:MAX_REAL_NAME], bio[:MAX_BIO], 1 if published else 0, now_sql()),
    )
    return profile(user_id)
