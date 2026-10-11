"""Userbot endpoints (``userbot`` scope). See :mod:`..userbot`.

They answer only while the account's automation mode is on: a token that
carries the scope from before it was reserved for the automation key cannot
outlive an administrator's lock.
"""

from __future__ import annotations

from .... import auth
from .. import serialize, userbot
from ..errors import ApiError, flag, json_body, text
from . import bp, caller, ok, requires


def _automation_account() -> dict:
    user = caller()
    if not user.get("userbot_enabled") or (user.get("userbot_mode_lock") or "unlocked") == "force_disabled":
        raise ApiError(403, "userbot_disabled")
    return user


@bp.get("/userbot/me")
@requires("userbot")
def userbot_me():
    user = _automation_account()
    info = userbot.token_info(user["id"])
    return ok(
        user={
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "userbot_enabled": bool(user.get("userbot_enabled")),
            "userbot_mode_lock": user.get("userbot_mode_lock") or "unlocked",
            "userbot_enable_count": int(user.get("userbot_enable_count") or 0),
            "userbot_disable_count": int(user.get("userbot_disable_count") or 0),
        },
        profile=userbot.profile(user["id"]),
        token={
            "created_at": serialize.iso(info["created_at"]) if info else None,
            "last_used_at": serialize.iso(info["last_used_at"]) if info else None,
        },
    )


@bp.post("/userbot/profile")
@requires("userbot", write=True)
def userbot_profile():
    """Needs write access on the token and, as on the profile page, ``profile.edit_own``."""
    user = _automation_account()
    if not auth.has_permission("profile.edit_own", user):
        raise ApiError(403, "cannot_edit_profile")
    data = json_body()
    real_name, bio, published = data.get("real_name"), data.get("bio"), data.get("page_published")
    try:
        profile = userbot.update_profile(
            user["id"],
            real_name=None if real_name is None else text(real_name, "real_name", maximum=userbot.MAX_REAL_NAME),
            bio=None if bio is None else text(bio, "bio", maximum=userbot.MAX_BIO),
            page_published=None if published is None else flag(published, "page_published"),
        )
    except userbot.UserbotError as error:
        raise ApiError(403, "profile_disabled", error.key) from None
    return ok(profile=profile)
