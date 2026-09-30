"""Who may use direct messages and group chats, and the limits that apply.

Access to messaging needs three things: the ``chat`` feature switched on
(the blueprint answers 404 otherwise), the permission (``chat.dm`` /
``chat.group``; accounts with ``chat_disabled`` never hold chat permissions
unless they are administrators) and the site switch (``chat_dm_enabled`` /
``chat_group_enabled``, which administrators bypass so they can moderate).
Every route, including every group moderation action, goes through these
checks.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from ... import auth, settings, storage

MB = 1024 * 1024
DEFAULT_MAX_LENGTH = 5000
MAX_LENGTH_CEILING = 100_000
DEFAULT_ATTACHMENT_MB = 5
DEFAULT_ATTACHMENTS_PER_DAY = 10

# Never accepted in chats, whatever the site upload policy says: executables and
# formats a browser would run or render as a document.
BLOCKED_EXTENSIONS = frozenset({
    "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "mst", "cpl", "hta", "inf", "ins", "isp", "jse",
    "lnk", "reg", "rgs", "sct", "shb", "shs", "vbe", "vbs", "wsc", "wsf", "wsh", "ws", "ps1", "ps2", "psc1",
    "psc2", "dll", "sys", "html", "htm", "xhtml", "shtml", "xht", "svg", "svgz", "mhtml", "mht", "swf", "jar",
    "class", "php", "php3", "php4", "php5", "php7", "phtml", "phar", "jsp", "jspx", "asp", "aspx", "ashx",
    "asmx", "cgi", "pl", "rb", "sh",
})


def _user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    return auth.current_user() if user is None else user


def _setting_on(name: str) -> bool:
    return bool(settings.get(name, 1))


def dm_allowed(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    if not user or not auth.has_permission("chat.dm", user):
        return False
    return _setting_on("chat_dm_enabled") or auth.is_admin(user)


def group_allowed(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    if not user or not auth.has_permission("chat.group", user):
        return False
    return _setting_on("chat_group_enabled") or auth.is_admin(user)


def can_start_dm(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return dm_allowed(user) and (_setting_on("chat_allow_dm_creation") or auth.is_admin(user))


def can_create_group(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    if not group_allowed(user) or not auth.has_permission("chat.create_group", user):
        return False
    return _setting_on("chat_allow_group_creation") or auth.is_admin(user)


def can_upload(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    if not user or not auth.has_permission("chat.upload", user):
        return False
    return _setting_on("chat_attachments_enabled") or auth.is_admin(user)


def can_be_messaged(target: dict[str, Any]) -> bool:
    """Whether *target* is an active account that can read direct messages."""
    if target.get("suspended") or (target.get("approval_status") or "approved") != "approved":
        return False
    return auth.has_permission("chat.dm", target)


def can_join_groups(target: dict[str, Any]) -> bool:
    if target.get("suspended") or (target.get("approval_status") or "approved") != "approved":
        return False
    return auth.has_permission("chat.group", target)


def _positive_int(name: str, default: int, ceiling: int) -> int:
    try:
        value = int(settings.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, ceiling))


def max_message_length() -> int:
    return _positive_int("chat_max_message_length", DEFAULT_MAX_LENGTH, MAX_LENGTH_CEILING)


def max_attachment_bytes() -> int:
    ceiling = current_app.config["BW"].max_attachment_size
    configured = _positive_int("chat_max_attachment_size_mb", DEFAULT_ATTACHMENT_MB, max(1, ceiling // MB)) * MB
    return storage.max_upload_bytes(min(configured, ceiling))


def attachments_per_day() -> int:
    return _positive_int("chat_attachments_per_day_limit", DEFAULT_ATTACHMENTS_PER_DAY, 100_000)
