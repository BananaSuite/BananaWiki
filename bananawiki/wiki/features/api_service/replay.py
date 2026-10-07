"""Current read access to resources returned by an idempotent response.

Replay records are historical answers, not authority to read a resource.
Visibility, categories and ownership can change during their 24-hour life.
Refuse the replay when a returned resource is no longer readable; the write
remains recorded and cannot run a second time with that key.
"""

from __future__ import annotations

from typing import Any

from ....core.json import loads as safe_json_loads
from ... import auth
from ..pages import categories
from ..pages import service as pages


def allowed(body: str, scope: str, user: dict[str, Any]) -> bool:
    try:
        payload = safe_json_loads(body)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    if scope == "pages":
        returned = [payload["page"]] if "page" in payload else payload.get("created", [])
        return all(isinstance(item, dict) and pages.can_view(pages.get(item.get("id")), user)
                   for item in returned)
    if scope == "categories" and "category" in payload:
        item = payload["category"]
        row = categories.get(item.get("id")) if isinstance(item, dict) else None
        return row is not None and auth.can_read_category(row["id"], user)
    if scope == "canvas" and "canvas" in payload:
        from ..canvas import access, service

        item = payload["canvas"]
        row = service.get(item.get("id")) if isinstance(item, dict) else None
        return row is not None and access.level(user, row) is not None
    if scope == "kanban":
        return _kanban_allowed(payload, user)
    return True


def etag(body: str) -> str | None:
    """Retain the documented revision header when replaying a page or canvas."""
    try:
        payload = safe_json_loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for name, field, prefix in (("page", "revision", "r"), ("canvas", "version", "v")):
        item = payload.get(name)
        number = item.get(field) if isinstance(item, dict) else None
        if type(number) is int and number >= 0:
            return f'"{prefix}{number}"'
    return None


def _kanban_allowed(payload: dict[str, Any], user: dict[str, Any]) -> bool:
    from ..kanban import access, store

    board_id = None
    if "board" in payload:
        item = payload["board"]
        board_id = item.get("id") if isinstance(item, dict) else None
    elif "column" in payload:
        item = payload["column"]
        row = store.get_column(item.get("id")) if isinstance(item, dict) else None
        board_id = row["board_id"] if row else None
    elif "ticket" in payload or "comment" in payload:
        item = payload.get("ticket", payload.get("comment"))
        ticket_id = (item.get("id") if "ticket" in payload else item.get("ticket_id")) if isinstance(item, dict) else None
        row = store.get_ticket(ticket_id)
        board_id = row["board_id"] if row else None
    else:
        # Count/order-only answers disclose no resource content.
        return True
    board = store.get_board(board_id)
    return board is not None and access.can_view(user, board)
