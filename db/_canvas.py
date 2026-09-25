"""Canvas layout CRUD operations and permission helpers."""

import json
import re
import sqlite3
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy
from helpers._text import slugify


def _unique_slug(title, exclude_id=None):
    """Return a slug derived from *title* that is unique in canvas__layouts."""
    base = slugify(title)
    # slugify falls back to 'page' for empty input; use 'canvas' instead
    if base == "page":
        base = "canvas"
    slug = base
    n = 1
    with get_db_context() as conn:
        while True:
            if exclude_id is not None:
                row = conn.execute(
                    "SELECT id FROM canvas__layouts WHERE slug = ? AND id != ?",
                    (slug, exclude_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM canvas__layouts WHERE slug = ?",
                    (slug,),
                ).fetchone()
            if not row:
                return slug
            n += 1
            slug = f"{base}-{n}"


@retry_on_busy
def get_user_layout_order(user_id):
    """Return a list of layout_ids in user's preferred order (or global when
    *user_id* is None).  Layouts that no longer exist are silently omitted."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT layout_id FROM canvas_user_layout_order "
            "WHERE user_id IS ? "
            "ORDER BY sort_order ASC",
            (user_id,),
        ).fetchall()
    return [r["layout_id"] for r in rows]


@retry_on_busy
def save_user_layout_order(user_id, layout_ids):
    """Replace the ordering for *user_id* (or global order when *user_id* is
    None) with the given list of layout_ids."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM canvas_user_layout_order WHERE user_id IS ?",
            (user_id,),
        )
        for i, lid in enumerate(layout_ids):
            conn.execute(
                "INSERT OR IGNORE INTO canvas_user_layout_order "
                "(user_id, layout_id, sort_order) VALUES (?, ?, ?)",
                (user_id, lid, i),
            )
        conn.commit()


_EMPTY_DATA = json.dumps({"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}})


def create_layout(title, creator_id, description="", category_id=None, data=None, visibility="private"):
    """Create a new canvas layout.  Returns the new layout id.

    Initial *data* (a dict or a JSON string) is filtered by
    :func:`sanitize_layout_data`, like every later save.
    """
    slug = _unique_slug(title)
    data = _EMPTY_DATA if data is None else _clean_layout_json(data)
    if visibility not in ("private", "shared", "public"):
        visibility = "private"
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO canvas__layouts "
            "(slug, title, description, category_id, creator_id, data, visibility) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, title, description, category_id, creator_id, data, visibility),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_layout(layout_id):
    """Return a single layout row or None."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT l.*, u.username AS creator_username "
            "FROM canvas__layouts l "
            "LEFT JOIN users u ON l.creator_id = u.id "
            "WHERE l.id = ?",
            (layout_id,),
        ).fetchone()


@retry_on_busy
def get_layout_by_slug(slug):
    """Return a single layout row by slug, or None."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT l.*, u.username AS creator_username "
            "FROM canvas__layouts l "
            "LEFT JOIN users u ON l.creator_id = u.id "
            "WHERE l.slug = ?",
            (slug,),
        ).fetchone()


@retry_on_busy
def list_layouts():
    """Return all layouts ordered by most-recently updated first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT l.*, u.username AS creator_username "
            "FROM canvas__layouts l "
            "LEFT JOIN users u ON l.creator_id = u.id "
            "ORDER BY l.updated_at DESC"
        ).fetchall()


@retry_on_busy
def list_layouts_for_user(user, open_access=False):
    """Return layouts visible to *user* (sqlite3.Row with id/role).

    When *open_access* is ``True`` all authenticated users see every
    non-archived layout (bypasses the usual visibility / permission
    filtering).
    """
    role = user["role"]
    uid = user["id"]
    if role in ("admin", "owner") or open_access:
        return list_layouts()

    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT l.*, u.username AS creator_username "
            "FROM canvas__layouts l "
            "LEFT JOIN users u ON l.creator_id = u.id "
            "WHERE ("
            "    l.creator_id = ? "
            "    OR (l.is_archived = 0 AND ("
            "      l.visibility = 'public' "
            "      OR (l.visibility = 'shared' AND ("
            "        EXISTS ("
            "          SELECT 1 FROM canvas__permissions cp "
            "          WHERE cp.layout_id = l.id AND cp.user_id = ? AND cp.permission != 'none'"
            "        ) OR EXISTS ("
            "          SELECT 1 FROM canvas__permissions cp "
            "          WHERE cp.layout_id = l.id AND cp.role = ? AND cp.permission != 'none'"
            "        )"
            "      )) "
            "      OR (l.visibility = 'private' AND EXISTS ("
            "        SELECT 1 FROM canvas__permissions cp "
            "        WHERE cp.layout_id = l.id AND cp.user_id = ? AND cp.permission != 'none'"
            "      ))"
            "    ))"
            "  ) "
            "ORDER BY l.updated_at DESC",
            (uid, uid, role, uid),
        ).fetchall()
    return rows


@retry_on_busy
def list_public_layouts():
    """Return canvases visible to anonymous public-mode visitors."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT l.*, u.username AS creator_username "
            "FROM canvas__layouts l "
            "LEFT JOIN users u ON l.creator_id = u.id "
            "WHERE l.is_archived = 0 AND l.visibility = 'public' "
            "ORDER BY l.updated_at DESC"
        ).fetchall()


def update_layout(layout_id, **kwargs):
    """Update layout fields.  Allowed: title, description, category_id,
    is_published, is_archived, data, creator_id, visibility."""
    allowed = {"title", "description", "category_id", "is_published", "is_archived", "data", "creator_id", "visibility"}
    for k in kwargs:
        if k not in allowed:
            raise ValueError(f"Invalid column: {k}")
    if "data" in kwargs:
        kwargs["data"] = _clean_layout_json(kwargs["data"])
    with get_db_context() as conn:
        set_parts = []
        vals = []
        for col in allowed:
            if col in kwargs:
                set_parts.append(f"{col} = ?")
                vals.append(kwargs[col])
        if "title" in kwargs:
            new_slug = _unique_slug(kwargs["title"], exclude_id=layout_id)
            set_parts.append("slug = ?")
            vals.append(new_slug)
        set_parts.append("updated_at = ?")
        vals.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        set_parts.append("version = version + 1")
        vals.append(layout_id)
        conn.execute(
            f"UPDATE canvas__layouts SET {', '.join(set_parts)} WHERE id = ?",
            vals,
        )
        conn.commit()


def delete_layout(layout_id):
    """Delete a layout and all associated permissions (cascade)."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM canvas__layouts WHERE id = ?", (layout_id,))
        conn.commit()


def save_layout_data(layout_id, data):
    """Update only the JSON data blob for a canvas layout.

    *data* (a dict or a JSON string) goes through :func:`sanitize_layout_data`
    first, so whole-document saves and imports store the same node fields
    as the incremental ``/ops`` path.
    """
    data = _clean_layout_json(data)
    with get_db_context() as conn:
        conn.execute(
            "UPDATE canvas__layouts SET data = ?, "
            "updated_at = ?, version = version + 1 WHERE id = ?",
            (data, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), layout_id),
        )
        conn.commit()


# Upload references inside the stored JSON.  Same pattern as the page scan in
# db._pages, plus the backslash, so a JSON escape right after a file name
# (for example an escaped quote in a text node) does not become part of it.
_UPLOAD_REF_RE = re.compile(r'/static/uploads/([^\s)"\'\\]+)')


@retry_on_busy
def referenced_upload_filenames():
    """Return the names of files in the upload folder that canvases use.

    Image nodes point at ``/static/uploads/<name>``, both for images added
    in the canvas editor and for images restored by an import.  The current
    data of every layout and every history snapshot is scanned, so the
    upload cleanup keeps an image as long as a canvas shows it or a revert
    could bring it back, as it does for page history.  Returns an empty set
    when the canvas tables do not exist.
    """
    filenames = set()
    with get_db_context() as conn:
        for table in ("canvas__layouts", "canvas__history"):
            try:
                rows = conn.execute(f"SELECT data FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                if row["data"]:
                    filenames.update(_UPLOAD_REF_RE.findall(row["data"]))
    return filenames


# Access to a layout is resolved from three sources in order: the creator,
# an explicit per-user row, then a per-role row; 'none' is a deny entry, not
# an absent one, so it can override a broader role grant.

def set_permission(layout_id, permission, user_id=None, role=None):
    """Set a permission entry for a layout.

    Exactly one of *user_id* or *role* must be provided.
    *permission* must be ``'view'``, ``'edit'``, or ``'none'``.
    """
    if permission not in ("view", "edit", "none"):
        raise ValueError(f"Invalid permission: {permission}")
    if user_id and role:
        raise ValueError("Specify user_id or role, not both")
    if not user_id and not role:
        raise ValueError("Specify user_id or role")

    with get_db_context() as conn:
        if user_id:
            existing = conn.execute(
                "SELECT id FROM canvas__permissions "
                "WHERE layout_id = ? AND user_id = ?",
                (layout_id, user_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE canvas__permissions SET permission = ? "
                    "WHERE layout_id = ? AND user_id = ?",
                    (permission, layout_id, user_id),
                )
            else:
                conn.execute(
                    "INSERT INTO canvas__permissions (layout_id, user_id, permission) "
                    "VALUES (?, ?, ?)",
                    (layout_id, user_id, permission),
                )
        else:
            existing = conn.execute(
                "SELECT id FROM canvas__permissions "
                "WHERE layout_id = ? AND role = ?",
                (layout_id, role),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE canvas__permissions SET permission = ? "
                    "WHERE layout_id = ? AND role = ?",
                    (permission, layout_id, role),
                )
            else:
                conn.execute(
                    "INSERT INTO canvas__permissions (layout_id, role, permission) "
                    "VALUES (?, ?, ?)",
                    (layout_id, role, permission),
                )
        conn.commit()


def remove_permission(layout_id, user_id=None, role=None):
    """Remove a permission entry for a layout."""
    with get_db_context() as conn:
        if user_id:
            conn.execute(
                "DELETE FROM canvas__permissions WHERE layout_id = ? AND user_id = ?",
                (layout_id, user_id),
            )
        elif role:
            conn.execute(
                "DELETE FROM canvas__permissions WHERE layout_id = ? AND role = ?",
                (layout_id, role),
            )
        conn.commit()


@retry_on_busy
def get_permissions(layout_id):
    """Return all permission rows for a layout."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT cp.*, u.username "
            "FROM canvas__permissions cp "
            "LEFT JOIN users u ON cp.user_id = u.id "
            "WHERE cp.layout_id = ?",
            (layout_id,),
        ).fetchall()


def clear_permissions(layout_id):
    """Remove all permissions for a layout."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM canvas__permissions WHERE layout_id = ?",
            (layout_id,),
        )
        conn.commit()


@retry_on_busy
def get_user_permission(layout_id, user, open_access=False):
    """Return the effective permission for *user* on *layout_id*.

    Returns ``'edit'``, ``'view'``, or ``'none'``.

    Resolution order:
    1. Admins always get ``'edit'``.
    2. Creator always gets ``'edit'``.
    3. When *open_access* is ``True``, all authenticated users get ``'edit'``.
    4. User-specific permission (highest precedence for non-admins).
    5. Role-based permission (fallback).
    6. Global visibility (public -> view).
    7. Default: ``'none'``.
    """
    role = user["role"]
    uid = user["id"]

    # Admins always edit
    if role in ("admin", "owner"):
        return "edit"

    layout = get_layout(layout_id)
    if not layout:
        return "none"

    # Creator always edit
    if layout["creator_id"] == uid:
        return "edit"

    # Open access: every authenticated user can edit every canvas
    if open_access:
        return "edit"

    with get_db_context() as conn:
        # User-specific check
        row = conn.execute(
            "SELECT permission FROM canvas__permissions "
            "WHERE layout_id = ? AND user_id = ?",
            (layout_id, uid),
        ).fetchone()
        if row:
            return row["permission"]

        # Role-based check
        row = conn.execute(
            "SELECT permission FROM canvas__permissions "
            "WHERE layout_id = ? AND role = ?",
            (layout_id, role),
        ).fetchone()
        if row:
            return row["permission"]

    # Fallback to global visibility if public
    if "visibility" in layout.keys() and layout["visibility"] == "public":
        return "view"

    return "none"


def user_can_view(layout_id, user, open_access=False):
    """Return True if *user* can view the layout.

    When *open_access* is ``True`` all authenticated users can view.
    """
    if not user:
        layout = get_layout(layout_id)
        return bool(layout and layout["is_archived"] == 0 and layout["visibility"] == "public")
    perm = get_user_permission(layout_id, user, open_access=open_access)
    return perm in ("view", "edit")


def user_can_edit(layout_id, user, open_access=False):
    """Return True if *user* can edit the layout.

    When *open_access* is ``True`` all authenticated users can edit.
    """
    perm = get_user_permission(layout_id, user, open_access=open_access)
    return perm == "edit"


@retry_on_busy
def user_has_any_permission(user_id):
    """Return True if *user_id* has at least one explicit user-specific canvas permission.

    Used to allow individually-shared users through the global canvas access gate,
    mirroring the kanban pattern where invited users bypass the global access check.
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM canvas__permissions "
            "WHERE user_id = ? AND permission != 'none' LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


def revoke_role_shares_for_restricted_roles(canvas_access):
    """Remove role-based canvas permissions for roles that no longer have global access.

    Called whenever the site-wide ``canvas_access`` setting is changed to ensure
    that existing canvas permissions are consistent with the new policy.
    """
    with get_db_context() as conn:
        if canvas_access == "admin":
            # Only admins have global access: remove editor and user role permissions
            conn.execute(
                "DELETE FROM canvas__permissions "
                "WHERE role IN ('editor', 'user')"
            )
        elif canvas_access == "editor":
            # Admins and editors have global access: remove user role permissions
            conn.execute(
                "DELETE FROM canvas__permissions "
                "WHERE role = 'user'"
            )
        # If "all", no role permissions need to be revoked
        conn.commit()


# Canvas nodes embed a wiki page's title and slug inside the layout JSON, so
# renaming, moving or deleting a page has to rewrite every blob referencing it.

def update_wiki_nodes_for_page(page_id, new_title=None, new_slug=None):
    """Update all canvas nodes that reference *page_id*.

    Called from the ``after_page_update`` hook.
    """
    # Use a LIKE pre-filter to avoid deserialising every canvas blob.
    # The precise check still happens in Python, so there are no false updates.
    like_pat = f'%"page_id":{page_id}%'
    like_pat_spaced = f'%"page_id": {page_id}%'
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT id, data FROM canvas__layouts "
            "WHERE data LIKE ? OR data LIKE ?",
            (like_pat, like_pat_spaced),
        ).fetchall()
        for row in rows:
            try:
                canvas_data = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            changed = False
            for node in canvas_data.get("nodes", []):
                if node.get("type") == "wiki_page" and node.get("page_id") == page_id:
                    if new_title is not None:
                        node["label"] = new_title
                    if new_slug is not None:
                        node["page_slug"] = new_slug
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE canvas__layouts SET data = ? WHERE id = ?",
                    (json.dumps(canvas_data), row["id"]),
                )
        conn.commit()


def mark_deleted_wiki_nodes(page_id):
    """Mark all canvas nodes that reference *page_id* as deleted.

    Called from the ``after_page_delete`` hook.
    """
    like_pat = f'%"page_id":{page_id}%'
    like_pat_spaced = f'%"page_id": {page_id}%'
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT id, data FROM canvas__layouts "
            "WHERE data LIKE ? OR data LIKE ?",
            (like_pat, like_pat_spaced),
        ).fetchall()
        for row in rows:
            try:
                canvas_data = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            changed = False
            for node in canvas_data.get("nodes", []):
                if node.get("type") == "wiki_page" and node.get("page_id") == page_id:
                    node["deleted"] = True
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE canvas__layouts SET data = ? WHERE id = ?",
                    (json.dumps(canvas_data), row["id"]),
                )
        conn.commit()


# Operational sync model for real-time collaboration.
# Each canvas keeps a monotonically increasing ``seq`` value on
# ``canvas__events``.  Clients send small operations (``upsert_node``,
# ``delete_node``, ``upsert_edge``, ``delete_edge``) and poll for events
# emitted by other sessions.  The single canonical state lives in
# ``canvas__layouts.data``; ops are applied in a transaction so two users
# editing different nodes never overwrite each other.

# Node fields a canvas stores.  Every write path (``/ops``, the
# whole-document ``/data`` save, imports and history restores) keeps only
# these.  Derived HTML such as a page preview or highlighted code is left
# out on purpose: the viewer renders those from server responses, and a
# stored copy would be markup that any collaborator could write and every
# viewer would receive.
_ALLOWED_NODE_KEYS = (
    "id", "type", "label", "display_text", "x", "y", "width", "height",
    "color", "border_color", "background_color", "text_color", "url",
    "page_id", "page_slug", "deleted", "text_size", "layer", "image_url",
    "metadata", "category", "rotation", "opacity", "shape", "icon",
    "embed_url", "video_id", "provider", "alt", "content", "language",
)


def _coerce_state(layout_id, conn):
    """Return the parsed canvas state for *layout_id* (must hold the conn)."""
    row = conn.execute(
        "SELECT data FROM canvas__layouts WHERE id = ?",
        (layout_id,),
    ).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["data"]) or {}
    except (json.JSONDecodeError, TypeError):
        state = {}
    state.setdefault("nodes", [])
    state.setdefault("edges", [])
    state.setdefault("viewport", {"x": 0, "y": 0, "zoom": 1})
    if not isinstance(state["nodes"], list):
        state["nodes"] = []
    if not isinstance(state["edges"], list):
        state["edges"] = []
    return state


def _next_seq(conn, layout_id):
    """Return the next event sequence number for *layout_id*."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM canvas__events WHERE layout_id = ?",
        (layout_id,),
    ).fetchone()
    return int(row["m"] or 0) + 1


def _sanitize_node(node):
    """Return a shallow-copy of *node* keeping only known canvas fields."""
    if not isinstance(node, dict):
        return None
    nid = node.get("id")
    if not isinstance(nid, (str, int)) or not str(nid):
        return None
    out = {}
    for key, value in node.items():
        if key in _ALLOWED_NODE_KEYS:
            out[key] = value
    out["id"] = str(nid)
    return out


def _sanitize_edge(edge):
    """Return a shallow-copy of *edge* with sane keys + ids."""
    if not isinstance(edge, dict):
        return None
    eid = edge.get("id")
    if not isinstance(eid, (str, int)) or not str(eid):
        return None
    return {k: v for k, v in edge.items() if isinstance(k, str)} | {"id": str(eid)}


def _sanitize_viewport(viewport):
    """Return a viewport dict with numeric ``x``, ``y`` and ``zoom``."""
    defaults = {"x": 0, "y": 0, "zoom": 1}
    if not isinstance(viewport, dict):
        return defaults
    out = {}
    for key, default in defaults.items():
        value = viewport.get(key, default)
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        out[key] = value if is_number else default
    return out


def sanitize_layout_data(data):
    """Return a clean copy of a whole canvas document.

    Nodes keep only ``_ALLOWED_NODE_KEYS`` and the viewport is reduced to
    numbers.  Edges are drawn as SVG paths with a text label, so any JSON
    object is kept as it is.  Other top-level keys and entries that are not
    objects are dropped.  Unlike ``/ops``, ids are not required or turned
    into strings, so an older export whose edges point at numeric node ids
    still lines up after the save.
    """
    if not isinstance(data, dict):
        data = {}
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    nodes = [
        {key: value for key, value in node.items() if key in _ALLOWED_NODE_KEYS}
        for node in (raw_nodes if isinstance(raw_nodes, list) else [])
        if isinstance(node, dict)
    ]
    edges = [
        edge for edge in (raw_edges if isinstance(raw_edges, list) else [])
        if isinstance(edge, dict)
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "viewport": _sanitize_viewport(data.get("viewport")),
    }


def _clean_layout_json(data):
    """Return *data* (a dict or a JSON string) as sanitised JSON text.

    Text that does not parse becomes an empty canvas.
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = None
    return json.dumps(sanitize_layout_data(data))


def apply_ops(layout_id, ops, by_user_id=None, by_session=""):
    """Apply a list of operational ``ops`` to *layout_id* atomically.

    Each op is one of:

    - ``{"op": "upsert_node",  "node": {...}}``: add or replace a node by id
    - ``{"op": "delete_node",  "id": "node_xyz"}``: remove a node by id
    - ``{"op": "upsert_edge",  "edge": {...}}``: add or replace an edge by id
    - ``{"op": "delete_edge",  "id": "edge_xyz"}``: remove an edge by id
    - ``{"op": "viewport_set", "viewport": {...}}``: replace the viewport
      (broadcast for parity but each client picks whether to follow)

    Returns ``(seq, applied)`` where ``seq`` is the highest event sequence
    written and ``applied`` is the list of fully-resolved op dicts that were
    actually persisted (after sanitisation and last-writer-wins de-dup).
    Returns ``(None, [])`` when the layout no longer exists.
    """
    if not ops:
        return None, []

    applied = []
    last_seq = None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    by_session = (by_session or "")[:64]

    with get_db_context() as conn:
        state = _coerce_state(layout_id, conn)
        if state is None:
            return None, []

        # Index by id for O(1) merges.
        nodes_by_id = {str(n.get("id")): n for n in state["nodes"] if isinstance(n, dict) and n.get("id")}
        edges_by_id = {str(e.get("id")): e for e in state["edges"] if isinstance(e, dict) and e.get("id")}

        for raw_op in ops:
            if not isinstance(raw_op, dict):
                continue
            op_type = raw_op.get("op") or raw_op.get("type")
            if op_type == "upsert_node":
                node = _sanitize_node(raw_op.get("node"))
                if not node:
                    continue
                nodes_by_id[node["id"]] = node
                applied.append({"op": "upsert_node", "node": node})
            elif op_type == "delete_node":
                nid = raw_op.get("id")
                if nid is None:
                    continue
                nid = str(nid)
                nodes_by_id.pop(nid, None)
                # Also drop edges that referenced the deleted node so the
                # stored state never contains dangling edges.
                edges_by_id = {
                    eid: e for eid, e in edges_by_id.items()
                    if str(e.get("source")) != nid and str(e.get("target")) != nid
                }
                applied.append({"op": "delete_node", "id": nid})
            elif op_type == "upsert_edge":
                edge = _sanitize_edge(raw_op.get("edge"))
                if not edge:
                    continue
                edges_by_id[edge["id"]] = edge
                applied.append({"op": "upsert_edge", "edge": edge})
            elif op_type == "delete_edge":
                eid = raw_op.get("id")
                if eid is None:
                    continue
                eid = str(eid)
                edges_by_id.pop(eid, None)
                applied.append({"op": "delete_edge", "id": eid})
            elif op_type == "viewport_set":
                vp = raw_op.get("viewport") or {}
                if isinstance(vp, dict):
                    state["viewport"] = {
                        "x": vp.get("x", 0),
                        "y": vp.get("y", 0),
                        "zoom": vp.get("zoom", 1),
                    }
                    applied.append({"op": "viewport_set", "viewport": state["viewport"]})

        if not applied:
            return None, []

        # Re-materialise ordered lists from the dicts (preserves last-seen order).
        state["nodes"] = list(nodes_by_id.values())
        state["edges"] = list(edges_by_id.values())

        conn.execute(
            "UPDATE canvas__layouts "
            "SET data = ?, updated_at = ?, version = version + 1 "
            "WHERE id = ?",
            (json.dumps(state), now, layout_id),
        )

        seq = _next_seq(conn, layout_id)
        for op in applied:
            conn.execute(
                "INSERT INTO canvas__events "
                "(layout_id, seq, op_type, payload, by_user_id, by_session) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (layout_id, seq, op["op"], json.dumps(op), by_user_id, by_session),
            )
            last_seq = seq
            seq += 1
        conn.commit()

    return last_seq, applied


def append_snapshot_event(layout_id, by_user_id=None, by_session=""):
    """Append a ``snapshot`` event signalling that the entire data blob was
    replaced (e.g. by a legacy bulk save / import).  Clients that observe
    this event re-fetch the full state to resync.
    """
    by_session = (by_session or "")[:64]
    with get_db_context() as conn:
        if not conn.execute(
            "SELECT 1 FROM canvas__layouts WHERE id = ?", (layout_id,),
        ).fetchone():
            return None
        seq = _next_seq(conn, layout_id)
        conn.execute(
            "INSERT INTO canvas__events "
            "(layout_id, seq, op_type, payload, by_user_id, by_session) "
            "VALUES (?, ?, 'snapshot', '{}', ?, ?)",
            (layout_id, seq, by_user_id, by_session),
        )
        conn.commit()
        return seq


@retry_on_busy
def latest_event_seq(layout_id):
    """Return the highest sequence number stored for *layout_id* (0 if none)."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM canvas__events WHERE layout_id = ?",
            (layout_id,),
        ).fetchone()
        return int(row["m"] or 0)


@retry_on_busy
def events_since(layout_id, since_seq, *, exclude_session=None, limit=500):
    """Return canvas events with ``seq > since_seq`` ordered ascending.

    *exclude_session* lets the calling client filter out its own echoes so
    polling does not replay locally-applied operations.
    """
    if since_seq is None:
        since_seq = 0
    params = [layout_id, int(since_seq)]
    sql = (
        "SELECT id, seq, op_type, payload, by_user_id, by_session, created_at "
        "FROM canvas__events "
        "WHERE layout_id = ? AND seq > ? "
    )
    if exclude_session:
        sql += "AND by_session != ? "
        params.append(str(exclude_session)[:64])
    sql += "ORDER BY seq ASC LIMIT ?"
    params.append(int(limit))
    with get_db_context() as conn:
        return conn.execute(sql, params).fetchall()


def prune_events(layout_id, keep=2000):
    """Trim the canvas event log to at most *keep* most-recent rows.

    Called best-effort after appends so the table cannot grow unbounded
    over the lifetime of a long-running canvas.
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM canvas__events WHERE layout_id = ?",
            (layout_id,),
        ).fetchone()
        if not row or (row["n"] or 0) <= keep:
            return 0
        threshold = conn.execute(
            "SELECT seq FROM canvas__events WHERE layout_id = ? "
            "ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (layout_id, int(keep)),
        ).fetchone()
        if not threshold:
            return 0
        cur = conn.execute(
            "DELETE FROM canvas__events WHERE layout_id = ? AND seq <= ?",
            (layout_id, int(threshold["seq"])),
        )
        conn.commit()
        return cur.rowcount or 0


# Layout-level history, for rollback.  Mirrors the wiki / kanban history mechanism: every meaningful save of a
# canvas layout captures the JSON ``data`` blob (nodes + edges + viewport)
# so editors can roll back to a previous revision.

def _latest_layout_snapshot(conn, layout_id):
    """Return the most recently stored ``data`` blob for *layout_id*."""
    row = conn.execute(
        "SELECT data FROM canvas__history "
        "WHERE layout_id = ? ORDER BY id DESC LIMIT 1",
        (layout_id,),
    ).fetchone()
    return row["data"] if row else None


def _normalize_canvas_data(data):
    """Coerce *data* to a deterministic JSON string for de-duplication."""
    if data is None:
        return ""
    if isinstance(data, (dict, list)):
        try:
            return json.dumps(data, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    if not isinstance(data, str):
        return str(data)
    try:
        parsed = json.loads(data)
        return json.dumps(parsed, sort_keys=True)
    except (TypeError, ValueError):
        return data


def record_layout_history(layout_id, edited_by, edit_message, is_revert=False):
    """Capture a snapshot of the layout state.

    The snapshot dedupes against the most recent stored entry to avoid
    redundant rows when nothing has changed.  Returns the new history id
    or ``None`` when deduped / the layout is missing.
    """
    with get_db_context() as conn:
        layout_row = conn.execute(
            "SELECT title, description, data FROM canvas__layouts WHERE id = ?",
            (layout_id,),
        ).fetchone()
        if not layout_row:
            return None
        normalized = _normalize_canvas_data(layout_row["data"])
        prev = _latest_layout_snapshot(conn, layout_id)
        prev_norm = _normalize_canvas_data(prev) if prev is not None else None
        if prev_norm == normalized and not is_revert:
            return None
        cur = conn.execute(
            "INSERT INTO canvas__history "
            "(layout_id, title, description, data, edited_by, "
            " edit_message, is_revert, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                layout_id,
                layout_row["title"] or "",
                layout_row["description"] or "",
                normalized,
                edited_by,
                edit_message or "",
                1 if is_revert else 0,
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_layout_history(layout_id):
    """Return all history entries for a layout, newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT h.*, u.username "
            "FROM canvas__history h "
            "LEFT JOIN users u ON h.edited_by = u.id "
            "WHERE h.layout_id = ? "
            "ORDER BY h.id DESC",
            (layout_id,),
        ).fetchall()
    return rows


@retry_on_busy
def get_layout_history_entry(entry_id):
    """Return a single layout-history row with the editor's username."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT h.*, u.username "
            "FROM canvas__history h "
            "LEFT JOIN users u ON h.edited_by = u.id "
            "WHERE h.id = ?",
            (entry_id,),
        ).fetchone()
    return row


def delete_layout_history_entry(entry_id):
    """Delete a single layout-history row."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM canvas__history WHERE id = ?",
            (entry_id,),
        )
        conn.commit()


def clear_layout_history(layout_id):
    """Delete every history entry attached to a layout."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM canvas__history WHERE layout_id = ?",
            (layout_id,),
        )
        conn.commit()


def restore_layout_from_snapshot(layout_id, snapshot_data, title=None, description=None):
    """Overwrite a layout's ``data`` blob with *snapshot_data*.

    *title* and *description* are restored from the snapshot when
    provided so a revert can also undo metadata changes.  Returns
    True if the layout was updated, False if it no longer exists.

    The snapshot goes through :func:`sanitize_layout_data`, because
    history rows written before whole-document saves were sanitised can
    still hold fields that the viewer must not receive.
    """
    data_str = _clean_layout_json(snapshot_data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_context() as conn:
        existing = conn.execute(
            "SELECT id FROM canvas__layouts WHERE id = ?",
            (layout_id,),
        ).fetchone()
        if not existing:
            return False
        set_parts = ["data = ?", "updated_at = ?", "version = version + 1"]
        vals = [data_str, now]
        if title:
            set_parts.append("title = ?")
            vals.append(title)
        if description is not None:
            set_parts.append("description = ?")
            vals.append(description)
        vals.append(layout_id)
        conn.execute(
            f"UPDATE canvas__layouts SET {', '.join(set_parts)} WHERE id = ?",
            vals,
        )
        # Clear the realtime event log so other clients reload the
        # restored snapshot on their next poll.
        conn.execute(
            "DELETE FROM canvas__events WHERE layout_id = ?",
            (layout_id,),
        )
        conn.commit()
    return True
