"""Loading boards, columns, tickets and their children with the access check applied.

A board the user cannot see answers 404 (its existence is not revealed);
signed-out visitors are sent to sign in. A board the user can see but not
change answers 403.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import abort, current_app

from ... import auth
from . import access, store

Check = Callable[[Any, Any], bool]
NEEDS: dict[str, Check] = {
    "view": access.can_view,
    "comment": access.can_comment,
    "write": access.can_write,
    "owner": access.is_owner,
    "export": access.can_export,
}


def _refuse(visible: bool) -> None:
    if auth.current_user() is None:
        if auth.wants_json():
            abort(401)
        abort(auth.redirect_to_login())
    abort(403 if visible else 404)


def board(board_id: Any, need: str = "view") -> dict[str, Any]:
    row = store.get_board(board_id)
    if row is None:
        abort(404)
    user = auth.current_user()
    if not NEEDS[need](user, row):
        _refuse(access.can_view(user, row))
    return row


def column(column_id: Any, need: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = store.get_column(column_id)
    if row is None:
        abort(404)
    return board(row["board_id"], need), row


def ticket(ticket_id: Any, need: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = store.get_ticket(ticket_id)
    if row is None:
        abort(404)
    return board(row["board_id"], need), row


def rate_limited(name: str, limit: int, window: int = 60) -> None:
    """Per-account limit for expensive or spammy actions (uploads, comments, exports)."""
    user = auth.current_user()
    key = f"kanban:{name}:{user['id'] if user else 'anonymous'}"
    if not current_app.extensions["bananawiki.limiter"].hit(key, limit, window):
        abort(429)
