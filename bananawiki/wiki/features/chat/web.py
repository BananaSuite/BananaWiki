"""The ``chat`` blueprint and helpers shared by its views.

Views answer HTML forms with a redirect and a flash message, and the chat
script's ``fetch`` calls (``Accept: application/json``) with JSON, so every
action works with and without JavaScript.
"""

from __future__ import annotations

from typing import Any

from flask import abort, current_app, jsonify, make_response, redirect, request
from werkzeug.utils import secure_filename

from ....core.timeutil import now_sql, parse, sql_in, to_sql
from ....core.web import client_ip
from ... import auth, storage
from ...i18n import t
from ...registry import feature_blueprint
from . import policy, store
from .store import Store

bp = feature_blueprint("chat", "chat", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/chat")

POLL_PER_MINUTE = 120
SEND_PER_MINUTE = 30
ACTIONS_PER_MINUTE = 30
EXPORTS_PER_MINUTE = 5


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
    key = f"chat:{bucket}:{user['id'] if user else client_ip()}"
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
        ext = storage.extension(secure_filename(upload.filename))
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
                  with_ip: bool = False) -> list[dict[str, Any]]:
    user = auth.current_user()
    viewer = user["id"] if user else None
    return [store.serialize(target, row, viewer_id=viewer, can_delete_any=can_delete_any, with_ip=with_ip)
            for row in rows]


def messages_payload(target: Store, parent_id: int, *, can_delete_any: bool = False,
                     with_ip: bool = False) -> dict[str, Any]:
    """JSON for the incremental fetch.

    * ``?before=<id>`` - one page of older messages (``has_more`` tells whether
      there are even older ones);
    * ``?after=<id>&since=<cursor>`` - messages newer than *id*, the ids of
      messages deleted since the previous poll, the oldest remaining id (older
      messages on the page were cleared) and the cursor for the next poll.
    """
    before = request.args.get("before", type=int)
    if before:
        rows = store.before(target, parent_id, before, store.clamp_limit(request.args.get("limit")))
        oldest = rows[0]["id"] if rows else None
        return {"messages": serialize_all(target, rows, can_delete_any=can_delete_any, with_ip=with_ip),
                "has_more": store.has_older(target, parent_id, oldest)}
    cursor = now_sql()
    after = max(0, request.args.get("after", default=0, type=int) or 0)
    rows = store.after(target, parent_id, after)
    since = parse(request.args.get("since"), bounded=True)
    return {
        "messages": serialize_all(target, rows, can_delete_any=can_delete_any, with_ip=with_ip),
        "more": len(rows) >= store.MAX_PAGE_SIZE,
        "first_id": store.first_id(target, parent_id),
        "deleted": store.deleted_since(target, parent_id, to_sql(since)) if since else [],
        "cursor": cursor,
    }


def conversation(target: Store, parent_id: int, *, can_delete_any: bool = False,
                 with_ip: bool = False) -> dict[str, Any]:
    """The server-rendered part of a chat page: one page of messages and paging state."""
    before = request.args.get("before", type=int)
    rows = store.before(target, parent_id, before, store.PAGE_SIZE)
    messages = serialize_all(target, rows, can_delete_any=can_delete_any, with_ip=with_ip)
    oldest = rows[0]["id"] if rows else None
    return {
        "messages": messages,
        "oldest_id": oldest or 0,
        "latest_id": rows[-1]["id"] if rows else (before or 0),
        "has_older": store.has_older(target, parent_id, oldest),
        "paged": bool(before),
        "cursor": now_sql(),
    }
