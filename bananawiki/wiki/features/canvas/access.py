"""Who may see, change and manage canvases.

The rules are the ones every shared item follows (:mod:`bananawiki.wiki.sharing`):
the ``canvas.*`` permissions say what a person may do, each canvas's
visibility and shares (rows of ``canvas__permissions`` for one user or one
role) say who sees it. Nobody gets anything without ``canvas.view`` (which
also disappears while the feature is off). Archived canvases are only
visible to their creator and administrators.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any

from ... import sharing
from ...db import db

KIND = "canvas"
VISIBILITIES = sharing.VISIBILITIES
PERMISSIONS = sharing.LEVELS
SHAREABLE_ROLES = sharing.SHAREABLE_ROLES

User = dict[str, Any] | None
Layout = dict[str, Any]


def public_access_active() -> bool:
    """Anonymous visitors may read public canvases (public mode is on)."""
    return sharing.anonymous_level("public") is not None


def is_admin(user: User) -> bool:
    return sharing.is_admin(user)


def can_use(user: User) -> bool:
    """Signed in and holding ``canvas.view`` (false while the feature is off)."""
    return sharing.can_use(KIND, user)


def can_create(user: User) -> bool:
    """Create or import canvases."""
    return sharing.can_create(KIND, user)


def can_open_canvas(user: User) -> bool:
    """Whether the canvas list (and the sidebar link) is available to *user*."""
    if user is None:
        return public_access_active()
    return can_use(user)


def _rows_for(user: dict[str, Any], layout_ids: list[int] | None = None) -> dict[int, dict[str, str]]:
    """``{layout_id: {"user": level, "role": level}}`` for *user*."""
    sql = "SELECT layout_id, user_id, role, permission FROM canvas__permissions WHERE (user_id = ? OR role = ?)"
    params: list[Any] = [user["id"], user["role"]]
    if layout_ids is not None:
        if not layout_ids:
            return {}
        sql += f" AND layout_id IN ({','.join('?' for _ in layout_ids)})"
        params.extend(layout_ids)
    rows: dict[int, dict[str, str]] = {}
    for row in db.all(sql, params):
        kind = "user" if row["user_id"] else "role"
        found = sharing.level_name(row["permission"])
        if found:
            rows.setdefault(row["layout_id"], {})[kind] = found
    return rows


def _level(viewer: sharing.Viewer, layout: Layout, rows: dict[str, str]) -> str | None:
    return viewer.level(creator_id=layout["creator_id"], visibility=layout.get("visibility"),
                        person=rows.get("user"), role=rows.get("role"), hidden=bool(layout.get("is_archived")))


def level(user: User, layout: Layout | None) -> str | None:
    """``"edit"``, ``"view"`` or ``None`` for *user* on *layout*."""
    if layout is None:
        return None
    rows = _rows_for(user, [layout["id"]]).get(layout["id"], {}) if user is not None else {}
    return _level(sharing.Viewer.of(KIND, user), layout, rows)


def can_view(user: User, layout: Layout | None) -> bool:
    """The single read check: canvas page, data, sync, history, export and embeds."""
    return level(user, layout) is not None


def can_edit(user: User, layout: Layout | None) -> bool:
    return user is not None and level(user, layout) == "edit"


def is_owner(user: User, layout: Layout | None) -> bool:
    """Manage visibility and sharing, and hand the canvas over (``canvas.share_own``)."""
    return layout is not None and sharing.can_manage(KIND, user, layout["creator_id"])


def can_delete(user: User, layout: Layout | None) -> bool:
    """Delete the canvas."""
    return layout is not None and sharing.can_delete(KIND, user, layout["creator_id"], can_view(user, layout))


def shareable_roles() -> list[str]:
    """Roles a canvas may be shared with (administrators see every canvas anyway)."""
    return list(SHAREABLE_ROLES)


# Lists

LAYOUT_SELECT = (
    "SELECT l.id, l.slug, l.title, l.description, l.category_id, l.creator_id, l.created_at, l.updated_at, "
    "l.is_published, l.is_archived, l.visibility, l.version, COALESCE(u.username, '') AS creator_username "
    "FROM canvas__layouts l LEFT JOIN users u ON u.id = l.creator_id"
)


#: Canvases read at a time while looking for the ones a windowed list shows.
_LIST_BATCH = 500
#: Most recently changed first; ``idx_canvas_layouts_updated`` (schema version 16) serves it.
_LIST_ORDER = " ORDER BY l.updated_at DESC, l.id DESC"
_PUBLIC = "l.visibility = 'public' AND l.is_archived = 0"


def visible_layouts(user: User, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    """Every canvas *user* can open with its ``level``, most recently changed first.

    With *limit*, only that window of them: canvases are read in batches until it is filled.
    """
    if user is None:
        if not public_access_active():
            return []
        window = "" if limit is None else f" LIMIT {int(limit)} OFFSET {int(offset)}"
        rows = db.all(f"{LAYOUT_SELECT} WHERE {_PUBLIC}{_LIST_ORDER}{window}")
        return [dict(row, level="view") for row in rows]
    if limit is None:
        return list(iter_visible_layouts(user))
    return list(islice(iter_visible_layouts(user), offset, offset + limit))


def _batches(where: str | None = None) -> Iterator[list[dict[str, Any]]]:
    """Canvases in list order, ``_LIST_BATCH`` at a time.

    Each batch continues after the last canvas of the one before (keyset
    paging on ``updated_at, id``) and walks the index, so reading every batch
    costs one pass over the table, not a sort of the whole table per batch.
    """
    after: tuple[str, int] | None = None
    while True:
        conditions = [where] if where else []
        params: list[Any] = []
        if after is not None:
            conditions.append("(l.updated_at, l.id) < (?, ?)")
            params.extend(after)
        clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        layouts = db.all(f"{LAYOUT_SELECT}{clause}{_LIST_ORDER} LIMIT ?", [*params, _LIST_BATCH])
        if layouts:
            yield layouts
        if len(layouts) < _LIST_BATCH:
            return
        after = (layouts[-1]["updated_at"], layouts[-1]["id"])


def iter_visible_layouts(user: User) -> Iterator[dict[str, Any]]:
    """The canvases of :func:`visible_layouts`, read in batches, for a caller that may stop early."""
    if user is None:
        if public_access_active():
            for layouts in _batches(_PUBLIC):
                yield from (dict(layout, level="view") for layout in layouts)
        return
    if not can_use(user):
        return
    permissions = _rows_for(user)
    viewer = sharing.Viewer.of(KIND, user)
    for layouts in _batches():
        for layout in layouts:
            found = _level(viewer, layout, permissions.get(layout["id"], {}))
            if found:
                yield dict(layout, level=found)


_ORDER_POSITION = ("(SELECT MIN(o.sort_order) FROM canvas_user_layout_order o "
                   "WHERE o.layout_id = l.id AND o.user_id IS :owner)")


def ordered_ids(user: User, order_owner: str | None) -> list[int]:
    """Ids of every canvas *user* can open, in the order of the canvas list.

    Canvases in the saved order of *order_owner* (``None``: the shared one)
    come first, in that order, then the others most recently changed first,
    as :func:`service.apply_order` arranges them. Anonymous visitors get the
    public canvases, most recently changed first. Only the columns the access
    check needs are read, so the list can be paged cheaply.
    """
    if user is None:
        if not public_access_active():
            return []
        return db.column("SELECT l.id FROM canvas__layouts l WHERE l.visibility = 'public' AND l.is_archived = 0 "
                         "ORDER BY l.updated_at DESC, l.id DESC")
    if not can_use(user):
        return []
    permissions = _rows_for(user)
    viewer = sharing.Viewer.of(KIND, user)
    rows = db.all(f"SELECT l.id, l.creator_id, l.is_archived, l.visibility FROM canvas__layouts l "
                  f"ORDER BY {_ORDER_POSITION} IS NULL, {_ORDER_POSITION}, l.updated_at DESC, l.id DESC",
                  {"owner": order_owner})
    return [row["id"] for row in rows if _level(viewer, row, permissions.get(row["id"], {}))]


def layouts_in_order(user: User, layout_ids: list[int]) -> list[dict[str, Any]]:
    """The canvases *layout_ids* (from :func:`ordered_ids`) with their ``level``, in that order."""
    if not layout_ids:
        return []
    marks = ",".join("?" for _ in layout_ids)
    rows = {row["id"]: row for row in db.all(f"{LAYOUT_SELECT} WHERE l.id IN ({marks})", layout_ids)}
    if user is None:
        return [dict(rows[i], level="view") for i in layout_ids if i in rows]
    permissions = _rows_for(user, layout_ids)
    viewer = sharing.Viewer.of(KIND, user)
    found = []
    for layout_id in layout_ids:
        layout = rows.get(layout_id)
        level_found = _level(viewer, layout, permissions.get(layout_id, {})) if layout else None
        if level_found:
            found.append(dict(layout, level=level_found))
    return found
