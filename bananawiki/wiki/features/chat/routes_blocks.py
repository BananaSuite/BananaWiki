"""Blocking and unblocking members: /chats/blocks… (see :mod:`.blocks`).

The forms sit in the account settings, in a direct conversation and in a
group's member list; each sends ``next`` to come back where it was. Nothing
is recorded in the audit log: a block is the member's private choice.
"""

from __future__ import annotations

from typing import Any

from flask import request, url_for

from ....core.web import safe_next
from ... import accounts, auth
from . import blocks, policy
from .web import ACTIONS_PER_MINUTE, bp, halt, refuse, succeed, throttle, too_many


def _require_chat() -> dict[str, Any]:
    user = auth.current_user()
    if not user or not (policy.dm_allowed(user) or policy.group_allowed(user)):
        halt(auth.deny(403, "chat.error.dm_unavailable"))
    return user  # type: ignore[return-value]


def _back() -> str:
    return safe_next(url_for("users.settings", _anchor="blocked-people"), request.form.get("next"))


@bp.route("/chats/blocks", methods=["POST"])
def block_user():
    user = _require_chat()
    back = _back()
    if not throttle("block", ACTIONS_PER_MINUTE):
        return too_many()
    user_id = (request.form.get("user_id") or "").strip()
    username = (request.form.get("username") or "").strip()
    if not (user_id or username):
        return refuse("chat.error.username_required", back)
    target = accounts.by_id(user_id) if user_id else accounts.by_username(username)
    if target is None:
        return refuse("chat.blocks.error.unknown_user", back, 404)
    try:
        blocks.block(user, target)
    except blocks.BlockRefused as error:
        return refuse(error.key, back, error.status, **error.values)
    return succeed(back, "chat.blocks.blocked", values={"user": target["username"]})


@bp.route("/chats/blocks/remove", methods=["POST"])
def unblock_user():
    user = _require_chat()
    back = _back()
    if not throttle("block", ACTIONS_PER_MINUTE):
        return too_many()
    user_id = (request.form.get("user_id") or "").strip()
    target = accounts.by_id(user_id) if user_id else None
    if target is None or not blocks.unblock(user["id"], target["id"]):
        return refuse("chat.blocks.error.not_blocked", back, 404)
    return succeed(back, "chat.blocks.unblocked", values={"user": target["username"]})
