"""Canvases and Kanban boards the current editor may embed, for the embed block's picker.

Only the public, read-only listing functions of those features are used:
``canvas.access.visible_layouts`` and ``kanban.access.visible_boards`` apply
the same rules as their own lists, so the picker never names a canvas or
board the editor could not open. A switched-off feature lists nothing.
"""

from __future__ import annotations

from typing import Any

from flask import url_for

from ... import registry
from . import document

MAX_RESULTS = 20
MAX_QUERY = 100


def _matches(query: str, *values: Any) -> bool:
    return not query or any(query in str(value or "").casefold() for value in values)


def _canvases(user: dict[str, Any], query: str) -> list[dict[str, Any]]:
    from ..canvas import access

    found = []
    for layout in access.visible_layouts(user):
        if layout.get("is_archived") or not _matches(query, layout["title"], layout["slug"]):
            continue
        found.append({"kind": "canvas", "ref": layout["slug"], "title": layout["title"] or layout["slug"],
                      "description": (layout.get("description") or "")[:160],
                      "url": url_for("canvas.view", slug=layout["slug"])})
        if len(found) >= MAX_RESULTS:
            break
    return found


def _boards(user: dict[str, Any], query: str) -> list[dict[str, Any]]:
    from ..kanban import access

    found = []
    for board in access.visible_boards(user):
        ref = str(board["id"])
        if not _matches(query, board["title"], ref):
            continue
        found.append({"kind": "kanban", "ref": ref, "title": board["title"] or ref,
                      "description": (board.get("description") or "")[:160],
                      "url": url_for("kanban.board", board_id=board["id"])})
        if len(found) >= MAX_RESULTS:
            break
    return found


_SOURCES = {"canvas": _canvases, "kanban": _boards}


def available_kinds() -> list[str]:
    return [kind for kind in document.EMBED_KINDS if registry.is_enabled(kind)]


def search(user: dict[str, Any], query: str = "", kind: str | None = None) -> list[dict[str, Any]]:
    """``[{kind, ref, title, description, url}]`` matching *query*, at most ``MAX_RESULTS`` per kind."""
    needle = query.strip().casefold()[:MAX_QUERY]
    results: list[dict[str, Any]] = []
    for name in available_kinds():
        if kind in (None, name):
            results.extend(_SOURCES[name](user, needle))
    return results
