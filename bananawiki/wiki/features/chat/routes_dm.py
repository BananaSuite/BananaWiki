"""Direct messages: /chats…"""

from __future__ import annotations

import logging
from typing import Any

from flask import abort, jsonify, render_template, request, url_for

from ....core.web import client_ip
from ... import accounts, auth, storage
from ...db import db
from ...i18n import t
from . import dms, exports, policy, retention, store
from .store import DM
from .web import (
    ACTIONS_PER_MINUTE,
    EXPORTS_PER_MINUTE,
    POLL_PER_MINUTE,
    SEND_PER_MINUTE,
    Refused,
    bp,
    conversation,
    halt,
    messages_payload,
    read_message_input,
    refuse,
    refused,
    succeed,
    throttle,
    too_many,
)

log = logging.getLogger("bananawiki.chat")
USER_SUGGESTIONS = 10


def _require_dm():
    if not policy.dm_allowed():
        halt(auth.deny(403, "chat.error.dm_unavailable"))


def _load_chat(chat_id: int) -> dict[str, Any]:
    """The conversation, if the current user takes part in it (404 otherwise)."""
    _require_dm()
    chat = dms.get(chat_id)
    user = auth.current_user()
    if chat is None or not dms.is_participant(chat, user["id"]):
        halt(auth.deny(404, "chat.error.not_found"))
    return chat


@bp.route("/chats")
def dm_list():
    _require_dm()
    user = auth.current_user()
    return render_template("chat/dm_list.html", chats=dms.list_for(user["id"]),
                           can_start=policy.can_start_dm(user))


@bp.route("/chats/new", methods=["GET", "POST"])
def dm_new():
    _require_dm()
    user = auth.current_user()
    if not policy.can_start_dm(user):
        return refuse("chat.error.dm_creation_disabled", url_for("chat.dm_list"), 403)
    username = (request.values.get("username") or request.values.get("user") or "").strip()
    if request.method == "GET":
        return render_template("chat/dm_new.html", username=username)
    if not throttle("dm_new", ACTIONS_PER_MINUTE):
        return too_many()
    target = accounts.by_username(username) if username else None
    if not username:
        return refuse("chat.error.username_required", url_for("chat.dm_new"))
    if target is None or not policy.can_be_messaged(target):
        return refuse("chat.error.user_unavailable", url_for("chat.dm_new", username=username))
    if target["id"] == user["id"]:
        return refuse("chat.error.dm_self", url_for("chat.dm_new"))
    chat = dms.get_or_create(user["id"], target["id"])
    return succeed(url_for("chat.dm_view", chat_id=chat["id"]), chat_id=chat["id"])


@bp.route("/chats/users")
def user_suggestions():
    """Usernames starting with ``q`` for the "new conversation" and "add member" fields."""
    if not (policy.dm_allowed() or policy.group_allowed()):
        return auth.deny(403, "chat.error.dm_unavailable")
    query = (request.args.get("q") or "").strip()
    if not query or not auth.has_permission("search.users"):
        return jsonify({"users": []})
    if not throttle("suggest", POLL_PER_MINUTE):
        return too_many()
    pattern = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    names = db.column(
        "SELECT username FROM users WHERE username LIKE ? ESCAPE '\\' AND id != ? AND suspended = 0 "
        "AND COALESCE(approval_status, 'approved') = 'approved' ORDER BY username COLLATE NOCASE LIMIT ?",
        (pattern, auth.current_user()["id"], USER_SUGGESTIONS),
    )
    return jsonify({"users": names})


@bp.route("/chats/<int:chat_id>")
def dm_view(chat_id: int):
    chat = _load_chat(chat_id)
    user = auth.current_user()
    other = accounts.by_id(dms.other_id(chat, user["id"]))
    dms.mark_read(chat, user["id"])
    return render_template(
        "chat/dm_view.html", chat=chat, other=other, conversation=conversation(DM, chat["id"]),
        can_upload=policy.can_upload(user), max_length=policy.max_message_length(),
        max_attachment_bytes=policy.max_attachment_bytes(), per_day=policy.attachments_per_day(),
        retention=retention.active_rules("dm"), counts=store.count(DM, chat["id"]),
    )


@bp.route("/chats/<int:chat_id>/messages")
def dm_messages(chat_id: int):
    chat = _load_chat(chat_id)
    if not throttle("poll", POLL_PER_MINUTE):
        return too_many()
    return jsonify(messages_payload(DM, chat["id"]))


@bp.route("/chats/<int:chat_id>/read", methods=["POST"])
def dm_read(chat_id: int):
    chat = _load_chat(chat_id)
    dms.mark_read(chat, auth.current_user()["id"])
    return jsonify({"ok": True})


@bp.route("/chats/<int:chat_id>/send", methods=["POST"])
def dm_send(chat_id: int):
    chat = _load_chat(chat_id)
    user = auth.current_user()
    back = url_for("chat.dm_view", chat_id=chat_id)
    if not throttle("send", SEND_PER_MINUTE):
        return too_many()
    try:
        content, stored = read_message_input(user)
    except Refused as error:
        return refused(error, back)
    try:
        message_id = dms.send(chat, user["id"], content, ip_address=client_ip(), stored=stored)
    except store.AttachmentLimitExceeded as error:
        if stored is not None:
            storage.delete(store.FOLDER, stored.filename)
        return refused(Refused("chat.error.daily_limit", 429, limit=error.limit), back)
    except BaseException:
        if stored is not None:
            storage.delete(store.FOLDER, stored.filename)
        raise
    return succeed(back, message_id=message_id)


@bp.route("/chats/<int:chat_id>/delete_message", methods=["POST"])
def dm_delete_message(chat_id: int):
    chat = _load_chat(chat_id)
    user = auth.current_user()
    back = url_for("chat.dm_view", chat_id=chat_id)
    if not throttle("action", ACTIONS_PER_MINUTE):
        return too_many()
    message = store.message(DM, request.form.get("message_id", type=int) or 0)
    if message is None or message["parent_id"] != chat["id"]:
        return refuse("chat.error.message_not_found", back, 404)
    if message["sender_id"] != user["id"]:
        return refuse("chat.error.not_your_message", back, 403)
    if not message["is_deleted"]:
        store.wipe(DM, message["id"])
    return succeed(back, "chat.message.deleted_flash", message_id=message["id"])


@bp.route("/chats/attachments/<int:attachment_id>/download")
def dm_attachment(attachment_id: int):
    """Participants (and administrators, for moderation) may download attachments."""
    user = auth.current_user()
    item = store.attachment(DM, attachment_id)
    admin = auth.is_admin(user)
    if item is None or item["is_deleted"] or not (admin or policy.dm_allowed(user)):
        abort(404)
    chat = dms.get(item["parent_id"])
    if chat is None or not (admin or dms.is_participant(chat, user["id"])):
        abort(404)
    return storage.send(store.FOLDER, item["filename"], download_name=item["original_name"],
                        blob_id=item["blob_id"], max_age=0)


@bp.route("/chats/<int:chat_id>/export")
def dm_export(chat_id: int):
    chat = _load_chat(chat_id)
    user = auth.current_user()
    if not throttle("export", EXPORTS_PER_MINUTE):
        return too_many()
    names = {row["id"]: row["username"] for row in db.all(
        "SELECT id, username FROM users WHERE id IN (?, ?)", (chat["user1_id"], chat["user2_id"]))}
    other = names.get(dms.other_id(chat, user["id"])) or "chat"
    header = [
        t("chat.export.dm_title"),
        t("chat.export.participants", first=names.get(chat["user1_id"], "?"),
               second=names.get(chat["user2_id"], "?")),
    ]
    return exports.export(DM, chat["id"], title=f"dm_{other}", header=header, include_ip=auth.is_admin(user))


@bp.route("/chats/<int:chat_id>/clear", methods=["POST"])
def dm_clear(chat_id: int):
    """Either participant may clear the conversation; it is cleared for both."""
    chat = _load_chat(chat_id)
    if not throttle("action", ACTIONS_PER_MINUTE):
        return too_many()
    messages, attachments = store.count(DM, chat["id"])
    dms.clear(chat)
    log.info("Chat %s cleared by %s", chat["id"], auth.current_user()["id"])
    return succeed(url_for("chat.dm_view", chat_id=chat_id), "chat.clear.done",
                   values={"messages": messages, "attachments": attachments})
