"""Userbot endpoints (``userbot`` scope). See :mod:`..userbot`."""

from __future__ import annotations

from .... import auth
from .. import serialize, userbot
from ..errors import ApiError, flag, json_body, text
from . import bp, caller, ok, requires


@bp.get("/userbot/me")
@requires("userbot")
def userbot_me():
    user = caller()
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
    user = caller()
    if not auth.has_permission("profile.edit_own", user):
        raise ApiError(403, "cannot_edit_profile")
    data = json_body()
    published = data.get("page_published")
    try:
        profile = userbot.update_profile(
            user["id"],
            real_name=text(data.get("real_name"), "real_name", maximum=userbot.MAX_REAL_NAME),
            bio=text(data.get("bio"), "bio", maximum=userbot.MAX_BIO),
            page_published=None if published is None else flag(published, "page_published"),
        )
    except userbot.UserbotError as error:
        raise ApiError(403, "profile_disabled", error.key) from None
    return ok(profile=profile)
