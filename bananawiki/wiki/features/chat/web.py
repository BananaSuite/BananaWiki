"""The ``chat`` blueprint and helpers shared by its views.

Views answer HTML forms with a redirect and a flash message, and the chat
script's ``fetch`` calls (``Accept: application/json``) with JSON, so every
action works with and without JavaScript.
"""

from __future__ import annotations

from typing import Any

from flask import abort, current_app, g, jsonify, make_response, redirect, request

from ....core.timeutil import now_sql, parse, sql_in, to_sql
from ....core.web import client_network
from ... import auth, storage
from ...i18n import t
from ...registry import feature_blueprint
from ..audit import record
from . import dms, policy, store
from .store import DM, Store

bp = feature_blueprint("chat", "chat", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/chat")

POLL_PER_MINUTE = 120
SEND_PER_MINUTE = 30
ACTIONS_PER_MINUTE = 30
EXPORTS_PER_MINUTE = 5
IMPERSONATED_READ_WINDOW = 10 * 60
# Marking a conversation read changes nothing anyone else sees; reading it is recorded by the views.
_UNAUDITED_ACTIONS = frozenset({"chat.dm_read", "chat.group_read"})


class Refused(Exception):
    """An action was refused; ``key`` is the translation key of the reason."""

    def __init__(self, key: str, status: int = 400, **values: Any):
        super().__init__(key)
        self.key = key
        self.status = status
        self.values = values


def throttle(bucket: str, limit: int, window: int = 60) -> bool:
    """Record a hit for the current user; False when *limit* was used up in *window* seconds."""
    user = auth.current_user()
    key = f"chat:{bucket}:{user['id'] if user else client_network()}"
    return current_app.extensions["bananawiki.limiter"].hit(key, limit, window)


def halt(result) -> None:
    """Stop the request with *result* (a response or a ``(body, status)`` tuple)."""
    abort(make_response(result))


def too_many():
    return auth.deny(429, "chat.error.rate_limited")


def refuse(key: str, target: str, status: int = 400, **values: Any):
    if auth.wants_json():
        return jsonify({"error": t(key, **values)}), status
    auth.flash_t(key, "error", **values)
    return redirect(target)


def refused(error: Refused, target: str):
    return refuse(error.key, target, error.status, **error.values)


def succeed(target: str, key: str | None = None, *, values: dict[str, Any] | None = None, **payload: Any):
    if auth.wants_json():
        body = {"ok": True, **payload}
        if key:
            body["message"] = t(key, **(values or {}))
        return jsonify(body)
    if key:
        auth.flash_t(key, "success", **(values or {}))
    return redirect(target)


def audit_impersonated(action: str, target_type: str, target_id: int, *, polling: bool = False,
                       **details: Any) -> None:
    """Record an administrator reading a member's conversation while impersonating them.

    A member's own reads are not audited, but an administrator viewing the
    wiki as the member reads the conversation as a participant, without the
    entries of the administrator routes: the same entry is recorded, with the
    administrator as actor (``record`` names the signed-in account) and the
    account they act as. Reading the history as JSON (*polling*) is recorded
    once per :data:`IMPERSONATED_READ_WINDOW` seconds, which opening the page
    always starts, so an open page does not fill the audit log.
    """
    if not auth.is_impersonating():
        return
    if action in ("chat.dm_read", "chat.group_read"):
        window = f"chat:impersonated_read:{auth.real_user()['id']}:{target_type}:{target_id}"
        opened = current_app.extensions["bananawiki.limiter"].hit(window, 1, IMPERSONATED_READ_WINDOW)
        if polling and not opened:
            return
    record(action, target_type=target_type, target_id=target_id,
           details={"as_user": auth.current_user()["username"], **details})


def withhold_previews(rows: list[dict[str, Any]], *, shown=lambda row: False) -> list[dict[str, Any]]:
    """While impersonating, leave the latest messages out of a conversation list.

    The list would show the start of every private conversation without the
    entry that reading it records; opening a conversation is audited. Rows
    for which *shown* is true (the global room, open to all) keep theirs.
    """
    if auth.is_impersonating():
        for row in rows:
            if row.get("last_message") and not shown(row):
                row["last_message"] = None
                row["preview_withheld"] = True
    return rows


@bp.after_request
def _audit_impersonated_action(response):
    """Record each chat action an administrator takes in a member's name while impersonating them.

    Messages sent, deleted or reported, conversations started or cleared,
    members blocked or moderated: the other participants see only the
    member's name. Refused attempts are recorded too, with their status.
    """
    if request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint in _UNAUDITED_ACTIONS \
            or not auth.is_impersonating():
        return response
    values = request.view_args or {}
    target = next(((kind, values[f"{kind}_id"]) for kind in ("chat", "group") if f"{kind}_id" in values), (None, None))
    details = {"as_user": auth.current_user()["username"], "endpoint": request.endpoint,
               "status": response.status_code}
    if g.get("chat_message_id"):
        details["message_id"] = g.chat_message_id
    record("chat.impersonated_action", target_type=target[0], target_id=target[1], details=details)
    return response


def page_number() -> int:
    return max(1, request.args.get("page", default=1, type=int) or 1)


def read_message_input(user: dict[str, Any]) -> tuple[str, storage.StoredFile | None]:
    """Validate the posted text and optional attachment; store the attachment.

    Raises :class:`Refused`. The caller must delete the stored file if saving
    the message fails afterwards.
    """
    content = (request.form.get("content") or "").strip()
    limit = policy.max_message_length()
    if len(content) > limit:
        raise Refused("chat.error.too_long", limit=limit)
    upload = request.files.get("attachment")
    stored = None
    if upload is not None and upload.filename:
        if not policy.can_upload(user):
            raise Refused("chat.error.uploads_disabled", 403)
        per_day = policy.attachments_per_day()
        if store.attachments_sent_since(user["id"], sql_in(days=-1)) >= per_day:
            raise Refused("chat.error.daily_limit", 429, limit=per_day)
        ext = storage.extension(storage.clean_filename(upload.filename))
        if ext in policy.BLOCKED_EXTENSIONS:
            raise Refused("upload.error.type_not_allowed", ext=ext)
        try:
            stored = storage.save(upload, store.FOLDER, allowed=None, max_bytes=policy.max_attachment_bytes())
        except storage.UploadError as error:
            raise Refused(error.key, **error.values) from error
    if not content and stored is None:
        raise Refused("chat.error.empty")
    return content, stored


def serialize_all(target: Store, rows: list[dict[str, Any]], *, can_delete_any: bool = False,
                  moderation: bool = False) -> list[dict[str, Any]]:
    """*rows* as the current user sees them; *moderation*: the administrators' view (see :func:`store.serialize`)."""
    user = auth.current_user()
    viewer = user["id"] if user else None
    deletable_since = dms.deletable_since() if target.kind == DM.kind else None
    return [store.serialize(target, row, viewer_id=viewer, can_delete_any=can_delete_any,
                            own_deletable_since=deletable_since, moderation=moderation,
                            ip_hidden=moderation and not policy.address_visible(user, row))
            for row in rows]


def _readers(hide_blocked: bool, moderation: bool) -> dict[str, str | None]:
    """What the current user does not see: messages withheld from them, those of people they blocked.

    The administrators' moderation views hide nothing.
    """
    user = auth.current_user()
    if moderation or not user:
        return {"hide_blocked_by": None, "reader": None}
    return {"hide_blocked_by": user["id"] if hide_blocked else None, "reader": user["id"]}


def messages_payload(target: Store, parent_id: int, *, can_delete_any: bool = False,
                     moderation: bool = False, floor: int = 0, hide_blocked: bool = False) -> dict[str, Any]:
    """JSON for the incremental fetch (messages after *floor* only).

    Direct messages withheld from the current user are left out, and with
    *hide_blocked* so are messages of the people they blocked (group chats,
    see :mod:`.blocks`).

    * ``?before=<id>`` - one page of older messages (``has_more`` tells whether
      there are even older ones);
    * ``?after=<id>&since=<cursor>`` - messages newer than *id*, the ids of
      messages deleted since the previous poll, the oldest remaining id (older
      messages on the page were cleared) and the cursor for the next poll.
    """
    readers = _readers(hide_blocked, moderation)
    before = request.args.get("before", type=int)
    if before:
        rows = store.before(target, parent_id, before, store.clamp_limit(request.args.get("limit")), floor=floor,
                            **readers)
        oldest = rows[0]["id"] if rows else None
        return {"messages": serialize_all(target, rows, can_delete_any=can_delete_any, moderation=moderation),
                "has_more": store.has_older(target, parent_id, oldest, floor=floor, **readers)}
    cursor = now_sql()
    after = max(0, request.args.get("after", default=0, type=int) or 0)
    rows = store.after(target, parent_id, after, floor=floor, **readers)
    since = parse(request.args.get("since"), bounded=True)
    return {
        "messages": serialize_all(target, rows, can_delete_any=can_delete_any, moderation=moderation),
        "more": len(rows) >= store.MAX_PAGE_SIZE,
        "first_id": store.first_id(target, parent_id, floor=floor, reader=readers["reader"]),
        "deleted": store.deleted_since(target, parent_id, to_sql(since), reader=readers["reader"]) if since else [],
        "cursor": cursor,
    }


def conversation(target: Store, parent_id: int, *, can_delete_any: bool = False,
                 moderation: bool = False, floor: int = 0, hide_blocked: bool = False) -> dict[str, Any]:
    """The server-rendered part of a chat page: one page of messages (after *floor*) and paging state."""
    readers = _readers(hide_blocked, moderation)
    before = request.args.get("before", type=int)
    rows = store.before(target, parent_id, before, store.PAGE_SIZE, floor=floor, **readers)
    messages = serialize_all(target, rows, can_delete_any=can_delete_any, moderation=moderation)
    oldest = rows[0]["id"] if rows else None
    return {
        "messages": messages,
        "oldest_id": oldest or 0,
        "latest_id": rows[-1]["id"] if rows else (before or 0),
        "has_older": store.has_older(target, parent_id, oldest, floor=floor, **readers),
        "paged": bool(before),
        "cursor": now_sql(),
    }
