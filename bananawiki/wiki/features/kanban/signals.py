"""Registry events for other features (webhooks, notifications, audit).

Emitted after the change committed, with rows as plain dicts from the store:

* ``kanban.board.created|updated|deleted``: ``board``, ``actor_id``
* ``kanban.ticket.created|updated|deleted``: ``ticket`` (with ``board_id``), ``board``, ``actor_id``
* ``kanban.ticket.moved``: ``ticket``, ``board``, ``from_column_id``, ``to_column_id``, ``actor_id``
* ``kanban.comment.created``: ``comment``, ``ticket``, ``board``, ``actor_id``

``registry.emit`` isolates handlers, so a failing handler never breaks the
action. Changes of many tickets emit one event per ticket.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from flask import has_request_context

from ... import auth
from ...registry import emit
from . import store

Row = dict[str, Any]


def actor_id(user: Row | None) -> str | None:
    """The acting account: *user*, else the signed-in user of the current request."""
    if user is None and has_request_context():
        user = auth.current_user()
    return user["id"] if user else None


def board(event: str, board_row: Row | None, user: Row | None) -> None:
    if board_row is not None:
        emit(f"kanban.board.{event}", board=dict(board_row), actor_id=actor_id(user))


def board_changed(board_id: int, user: Row | None) -> None:
    board(event="updated", board_row=store.get_board(board_id), user=user)


def tickets(event: str, rows: Iterable[Row], board_row: Row, user: Row | None) -> None:
    """``kanban.ticket.<event>`` for every row (for ``deleted``, rows read before the deletion)."""
    actor = actor_id(user)
    for row in rows:
        emit(f"kanban.ticket.{event}", ticket=dict(row), board=dict(board_row), actor_id=actor)


def tickets_changed(event: str, ticket_ids: Iterable[int], board_id: int, user: Row | None) -> None:
    """Re-read the tickets and the board, then emit ``kanban.ticket.<event>`` for each."""
    board_row = store.get_board(board_id)
    if board_row is None:
        return
    rows = [row for row in (store.get_ticket(ticket_id) for ticket_id in ticket_ids) if row is not None]
    tickets(event, rows, board_row, user)


def moved(moves: Iterable[tuple[int, int]], board_id: int, user: Row | None) -> None:
    """``kanban.ticket.moved`` for ``(ticket id, column it came from)`` pairs."""
    board_row = store.get_board(board_id)
    if board_row is None:
        return
    actor = actor_id(user)
    for ticket_id, from_column_id in moves:
        row = store.get_ticket(ticket_id)
        if row is None:
            continue
        emit("kanban.ticket.moved", ticket=dict(row), board=dict(board_row), from_column_id=from_column_id,
             to_column_id=row["column_id"], actor_id=actor)


def comment(comment_row: Row, ticket_row: Row, user: Row | None) -> None:
    board_row = store.get_board(ticket_row["board_id"])
    if board_row is not None:
        emit("kanban.comment.created", comment=dict(comment_row), ticket=dict(ticket_row), board=dict(board_row),
             actor_id=actor_id(user))
