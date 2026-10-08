"""Canvas storage: layouts, sharing, list order, the op log, history and page links.

Concurrency
-----------
``canvas__layouts.version`` grows by one on every change. Editors send small
operations (``/ops``) that are applied inside one transaction to the stored
document, so two people editing different nodes never overwrite each other;
each applied operation is appended to ``canvas__events`` with a per-canvas
sequence number, and open editors poll ``/sync`` for the operations of other
sessions. A whole-document save (``POST /data``) carries the version the
client started from and is refused with :class:`VersionConflict` when the
canvas changed in between; it and history restores append a ``snapshot``
event that tells open editors to reload.

Wiki-page nodes store only the id of their page (:func:`link_pages`); its
title and slug are read when the canvas is shown, as the viewer may see them
(:mod:`.present`). Changes made by the server to older nodes that still carry
a slug (a linked page was renamed or deleted) go through the same path as
edits: the version is bumped and ``upsert_node`` events are appended, so open
editors pick them up instead of overwriting them.

Retention
---------
The op log keeps the newest :data:`EVENTS_KEEP` operations per canvas and
drops operations older than a day (the newest one always stays, so sequence
numbers never restart). History snapshots hold the whole document, so their
number and size are bounded: edits, saves and title changes by one person
within :data:`HISTORY_COALESCE_MINUTES`, and changes by anyone within
:data:`HISTORY_MIN_SECONDS` of the latest entry (people editing together),
update that entry instead of adding one; each canvas keeps its newest
:data:`HISTORY_KEEP` entries and, of those, only as many as fit in
:data:`HISTORY_MAX_BYTES` (the newest always stays).

Events
------
After a change is committed the registry events ``canvas.created``,
``canvas.updated`` (once per request: an operation batch, a save, a restore,
a change of title, visibility, sharing or owner, or a server-side page-link
update) and ``canvas.deleted`` are emitted with ``canvas`` (the layout row)
and ``actor_id``. Functions without an explicit actor use the signed-in user
of the current request, so the REST API emits the same events as the web
interface.

Locks
-----
Operations and whole-document saves that change or remove a locked node are
refused (:func:`.model.lock_violation`); history restores are not, since they
put back a state someone chose deliberately.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from flask import g, has_app_context

from ....core.timeutil import now_sql, sql_in
from ... import accounts
from ...db import db
from ...registry import emit
from ..pages import service as pages
from . import model
from .access import LAYOUT_SELECT

MAX_TITLE = 200
MAX_DESCRIPTION = 2000
HISTORY_KEEP = 200
HISTORY_MAX_BYTES = 32 * 1024 * 1024
HISTORY_COALESCE_MINUTES = 15
HISTORY_MIN_SECONDS = 60
#: Entries later changes may be merged into (not creations, imports or restores).
_COALESCED_MESSAGES = frozenset({"edited", "saved", "info"})
#: Bytes of a stored snapshot; octet_length (SQLite 3.43) does not read the snapshot itself.
_SNAPSHOT_BYTES = "octet_length(data)" if sqlite3.sqlite_version_info >= (3, 43) else "length(CAST(data AS BLOB))"
EVENTS_KEEP = 500
EVENTS_MAX_AGE_HOURS = 24
SYNC_BATCH = 500
RESERVED_SLUGS = frozenset({"create", "import", "api", "static", "settings"})
#: Only wiki-page nodes saved by older versions store a slug (and the page title).
_STORED_SLUG_MARKER = '"page_slug"'


class CanvasError(ValueError):
    """A refused change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


class VersionConflict(CanvasError):
    def __init__(self, current_version: int):
        super().__init__("canvas.error.conflict")
        self.current_version = current_version


# ── Events ────────────────────────────────────────────────────────────────────


def _actor(actor_id: str | None) -> str | None:
    """*actor_id*, or the signed-in user of the current request."""
    if actor_id is not None or not has_app_context():
        return actor_id
    user = g.get("user")
    return user["id"] if user else None


def _emit(event: str, layout_id: int, actor_id: str | None, row: dict[str, Any] | None = None) -> None:
    """Emit *event* for a committed change (``row`` is given for a canvas that no longer exists)."""
    row = row if row is not None else get(layout_id)
    if row is not None:
        emit(event, canvas=row, actor_id=_actor(actor_id))


def _locked_error(ids: list[str]) -> CanvasError:
    return CanvasError("canvas.error.locked", count=len(ids), ids=", ".join(ids[:10]))


# ── Layouts ───────────────────────────────────────────────────────────────────


def get_by_slug(slug: str | None) -> dict[str, Any] | None:
    if not slug:
        return None
    return db.one(f"{LAYOUT_SELECT} WHERE l.slug = ?", (slug,))


def get(layout_id: int) -> dict[str, Any] | None:
    return db.one(f"{LAYOUT_SELECT} WHERE l.id = ?", (layout_id,))


def unique_slug(title: str) -> str:
    base = pages.slugify(title)
    if base == "page" and "page" not in title.lower():
        base = "canvas"
    if base in RESERVED_SLUGS:
        base = f"{base}-canvas"
    candidate, number = base, 2
    while db.scalar("SELECT 1 FROM canvas__layouts WHERE slug = ?", (candidate,)):
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def clean_info(title: str | None, description: str | None) -> tuple[str, str]:
    title = " ".join((title or "").split())
    description = (description or "").strip()
    if not title:
        raise CanvasError("canvas.error.title_required")
    if len(title) > MAX_TITLE:
        raise CanvasError("canvas.error.title_too_long", limit=MAX_TITLE)
    if len(description) > MAX_DESCRIPTION:
        raise CanvasError("canvas.error.description_too_long", limit=MAX_DESCRIPTION)
    return title, description


def document(layout_id: int) -> dict[str, Any]:
    """The stored document of a canvas, cleaned."""
    return model.clean_document(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (layout_id,)))


def create(title: str, description: str, creator_id: str, *, data: Any = None,
           message: str = "created") -> dict[str, Any]:
    title, description = clean_info(title, description)
    doc = model.clean_document(data if data is not None else model.EMPTY_DOCUMENT, strict=True)
    link_pages(doc["nodes"], creator_id)
    text = model.serialize(doc)
    now = now_sql()
    with db.transaction():
        layout_id = db.insert("canvas__layouts", {
            "slug": unique_slug(title), "title": title, "description": description, "creator_id": creator_id,
            "data": text, "visibility": "private", "created_at": now, "updated_at": now, "version": 1,
        })
        record_history(layout_id, creator_id, message)
    layout = get(layout_id)
    _emit("canvas.created", layout_id, creator_id, layout)
    return layout  # type: ignore[return-value]


def update_info(layout: dict[str, Any], title: str | None, description: str | None, *, user_id: str) -> None:
    """Change title and description. The slug stays, so links and embeds keep working."""
    title, description = clean_info(title, description)
    with db.transaction():
        db.execute(
            "UPDATE canvas__layouts SET title = ?, description = ?, updated_at = ? WHERE id = ?",
            (title, description, now_sql(), layout["id"]),
        )
        record_history(layout["id"], user_id, "info", coalesce=True)
    _emit("canvas.updated", layout["id"], user_id)


def delete(layout: dict[str, Any], *, actor_id: str | None = None) -> None:
    row = get(layout["id"])
    if db.execute("DELETE FROM canvas__layouts WHERE id = ?", (layout["id"],)).rowcount:
        _emit("canvas.deleted", layout["id"], actor_id, row)


def set_visibility(layout: dict[str, Any], visibility: str, *, actor_id: str | None = None) -> None:
    from .access import VISIBILITIES

    if visibility not in VISIBILITIES:
        raise CanvasError("canvas.error.bad_visibility")
    db.execute("UPDATE canvas__layouts SET visibility = ? WHERE id = ?", (visibility, layout["id"]))
    _emit("canvas.updated", layout["id"], actor_id)


def transfer(layout: dict[str, Any], new_owner_id: str, *, actor_id: str | None = None) -> None:
    with db.transaction():
        db.execute("UPDATE canvas__layouts SET creator_id = ? WHERE id = ?", (new_owner_id, layout["id"]))
        db.execute("DELETE FROM canvas__permissions WHERE layout_id = ? AND user_id = ?",
                   (layout["id"], new_owner_id))
    _emit("canvas.updated", layout["id"], actor_id)


# ── Sharing ───────────────────────────────────────────────────────────────────


def permissions(layout_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT p.id, p.user_id, p.role, p.permission, u.username FROM canvas__permissions p "
        "LEFT JOIN users u ON u.id = p.user_id WHERE p.layout_id = ? "
        "ORDER BY p.role IS NULL, p.role, u.username COLLATE NOCASE",
        (layout_id,),
    )


def set_permission(layout_id: int, permission: str, *, user_id: str | None = None,
                   role: str | None = None, actor_id: str | None = None) -> None:
    from .access import PERMISSIONS

    if permission not in PERMISSIONS or bool(user_id) == bool(role):
        raise CanvasError("canvas.error.bad_request")
    column, value = ("user_id", user_id) if user_id else ("role", role)
    with db.transaction():
        updated = db.execute(
            f"UPDATE canvas__permissions SET permission = ? WHERE layout_id = ? AND {column} = ?",
            (permission, layout_id, value),
        ).rowcount
        if not updated:
            db.insert("canvas__permissions", {"layout_id": layout_id, column: value, "permission": permission,
                                              "created_at": now_sql()})
    _emit("canvas.updated", layout_id, actor_id)


def remove_permission(layout_id: int, *, user_id: str | None = None, role: str | None = None,
                      actor_id: str | None = None) -> None:
    removed = 0
    if user_id:
        removed = db.execute("DELETE FROM canvas__permissions WHERE layout_id = ? AND user_id = ?",
                             (layout_id, user_id)).rowcount
    elif role:
        removed = db.execute("DELETE FROM canvas__permissions WHERE layout_id = ? AND role = ?",
                             (layout_id, role)).rowcount
    if removed:
        _emit("canvas.updated", layout_id, actor_id)


# ── List order ────────────────────────────────────────────────────────────────


def order_ids(owner_id: str | None) -> list[int]:
    """Saved order for one user, or the shared order (``None``) under open access."""
    return db.column(
        "SELECT layout_id FROM canvas_user_layout_order WHERE user_id IS ? ORDER BY sort_order, id", (owner_id,)
    )


def apply_order(layouts: list[dict[str, Any]], ordered_ids: Iterable[int]) -> list[dict[str, Any]]:
    by_id = {layout["id"]: layout for layout in layouts}
    ordered = [by_id.pop(layout_id) for layout_id in ordered_ids if layout_id in by_id]
    return ordered + [layout for layout in layouts if layout["id"] in by_id]


def save_order(owner_id: str | None, layout_ids: list[int]) -> None:
    with db.transaction():
        db.execute("DELETE FROM canvas_user_layout_order WHERE user_id IS ?", (owner_id,))
        db.executemany(
            "INSERT OR IGNORE INTO canvas_user_layout_order (user_id, layout_id, sort_order) VALUES (?, ?, ?)",
            [(owner_id, layout_id, position) for position, layout_id in enumerate(layout_ids)],
        )


def order_version() -> int:
    """Changes whenever the shared order is saved (rows are re-inserted with new ids).

    Canvas keeps its own counter instead of the ``list_order_version`` site
    setting that 1.4 shared with kanban.
    """
    return int(db.scalar(
        "SELECT COALESCE(MAX(id), 0) + COUNT(*) FROM canvas_user_layout_order WHERE user_id IS NULL", default=0
    ))


# ── Wiki page links ───────────────────────────────────────────────────────────


def link_pages(nodes: Iterable[dict[str, Any]], actor_id: str | None) -> None:
    """Point wiki-page nodes at their page by id only, as *actor_id* may.

    Titles and slugs are never stored: they are read from the page when the
    canvas is shown (:mod:`.present`), so a canvas, its history and its exports
    hold nothing about a page beyond its id. Nodes from older versions that
    only carry a slug gain the page id when the actor may read that page (so
    later renames and deletions can follow them) and otherwise keep the slug
    they were given.
    """
    wiki_nodes = [node for node in nodes if node["type"] == "wiki_page"]
    if not wiki_nodes:
        return
    slugs = sorted({node["page_slug"] for node in wiki_nodes if not node.get("page_id") and node.get("page_slug")})
    actor = _acting_user(actor_id) if slugs else None
    by_slug = {page["slug"]: page for page in _readable_pages_by_slug(slugs, actor)} if actor else {}
    for node in wiki_nodes:
        page = None if node.get("page_id") else by_slug.get(node.get("page_slug"))
        if page is not None:
            node["page_id"] = page["id"]
        model.drop_page_details(node)


def _acting_user(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    user = g.get("user") if has_app_context() else None
    if user and user["id"] == user_id:
        return user
    return accounts.by_id(user_id)


def _readable_pages_by_slug(slugs: list[str], user: dict[str, Any]) -> list[dict[str, Any]]:
    if not slugs:
        return []
    where, params = pages.visible_filter(user)
    return db.all(f"SELECT p.id, p.slug FROM pages p WHERE p.slug IN ({','.join('?' for _ in slugs)}) AND {where}",
                  [*slugs, *params])


def _pages_by(ids: list[int], slugs: list[str], columns: str) -> list[dict[str, Any]]:
    clauses, params = [], []
    if ids:
        clauses.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if slugs:
        clauses.append(f"slug IN ({','.join('?' for _ in slugs)})")
        params.extend(slugs)
    if not clauses:
        return []
    return db.all(f"SELECT {columns} FROM pages WHERE {' OR '.join(clauses)}", params)


def _rewrite_page_nodes(matches: Callable[[dict[str, Any]], bool], change: Callable[[dict[str, Any]], None]) -> int:
    """Apply *change* to matching wiki-page nodes in every canvas; return canvases changed.

    Only nodes saved by older versions still carry a slug, so canvases whose
    nodes hold just page ids are left as they are.
    """
    candidates = db.column(
        "SELECT id FROM canvas__layouts WHERE instr(data, ?) > 0", (_STORED_SLUG_MARKER,)
    )
    changed_layouts = 0
    for layout_id in candidates:
        with db.transaction():
            row = db.one("SELECT data FROM canvas__layouts WHERE id = ?", (layout_id,))
            if row is None:
                continue
            doc = model.clean_document(row["data"])
            changed = []
            for node in doc["nodes"]:
                if node["type"] == "wiki_page" and matches(node):
                    before = dict(node)
                    change(node)
                    if node != before:
                        changed.append(node)
            if not changed:
                continue
            _store(layout_id, doc)
            for node in changed:
                _append_event(layout_id, "upsert_node", {"op": "upsert_node", "node": node}, None, "server")
            changed_layouts += 1
        _emit("canvas.updated", layout_id, None)
    return changed_layouts


def _same_page(page: dict[str, Any], *slugs: str | None) -> Callable[[dict[str, Any]], bool]:
    names = {slug for slug in slugs if slug}
    return lambda node: node.get("page_id") == page["id"] or (
        not node.get("page_id") and node.get("page_slug") in names
    )


def _point_at(page: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def change(node: dict[str, Any]) -> None:
        node["page_id"] = page["id"]
        model.drop_page_details(node)

    return change


def on_page_renamed(page: dict[str, Any], old_slug: str, **_: Any) -> None:
    _rewrite_page_nodes(_same_page(page, old_slug, page["slug"]), _point_at(page))


def on_page_updated(page: dict[str, Any], previous: dict[str, Any] | None = None, **_: Any) -> None:
    if previous and previous.get("title") == page["title"] and previous.get("slug") == page["slug"]:
        return
    old_slug = previous.get("slug") if previous else None
    _rewrite_page_nodes(_same_page(page, old_slug, page["slug"]), _point_at(page))


def on_page_restored(page: dict[str, Any], **_: Any) -> None:
    _rewrite_page_nodes(_same_page(page, page["slug"]), _point_at(page))


def on_page_deleted(page: dict[str, Any], **_: Any) -> None:
    # Nodes keep the id, so restoring the page brings the link back.
    _rewrite_page_nodes(_same_page(page, page.get("slug")), _point_at(page))


# ── Saving ────────────────────────────────────────────────────────────────────


def _store(layout_id: int, doc: dict[str, Any]) -> int:
    """Write *doc* and bump the version; return the new version (inside a transaction)."""
    db.execute(
        "UPDATE canvas__layouts SET data = ?, updated_at = ?, version = version + 1 WHERE id = ?",
        (model.serialize(doc), now_sql(), layout_id),
    )
    return int(db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout_id,)))


def apply(layout: dict[str, Any], ops: Any, *, user_id: str, session_id: str,
          skip_locked: bool = False) -> dict[str, Any]:
    """Apply editor operations atomically; return ``version``, ``seq``, ``applied`` and ``rejected``.

    An operation that breaks a lock refuses the whole batch with
    ``canvas.error.locked``, or with *skip_locked* (the editor) is left out
    and listed in ``rejected`` while the rest is applied.
    """
    rejected: list[dict[str, Any]] = []
    with db.transaction():
        row = db.one("SELECT data, version FROM canvas__layouts WHERE id = ?", (layout["id"],))
        if row is None:
            raise CanvasError("canvas.error.not_found")
        doc = model.clean_document(row["data"])
        applied = model.apply_ops(doc, ops, rejected)
        if rejected and not skip_locked:
            raise _locked_error([item["id"] for item in rejected])
        if not applied:
            return {"version": row["version"], "seq": head_seq(layout["id"]), "applied": [], "rejected": rejected}
        link_pages((op["node"] for op in applied if op["op"] == "upsert_node"), user_id)
        version = _store(layout["id"], doc)
        seq = 0
        for op in applied:
            if op["op"] != "viewport_set":
                seq = _append_event(layout["id"], op["op"], op, user_id, session_id)
        _trim_events(layout["id"])
        record_history(layout["id"], user_id, "edited", coalesce=True)
    _emit("canvas.updated", layout["id"], user_id)
    return {"version": version, "seq": seq or head_seq(layout["id"]), "applied": applied, "rejected": rejected}


def save_document(layout: dict[str, Any], data: Any, *, expected_version: int | None, user_id: str,
                  session_id: str) -> dict[str, Any]:
    """Replace the whole document unless someone saved since *expected_version*.

    Refused with ``canvas.error.locked`` when it changes or drops a locked node.
    Wiki-page nodes sent without their page (the sender could not see it),
    and locked ones whatever page they name, keep the stored link.
    """
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(
            data.get("edges"), list):
        raise CanvasError("canvas.error.bad_document")
    doc = model.clean_document(data, strict=True)
    with db.transaction():
        current = db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout["id"],))
        if current is None:
            raise CanvasError("canvas.error.not_found")
        if expected_version is not None and int(current) != int(expected_version):
            raise VersionConflict(int(current))
        stored = document(layout["id"])
        model.keep_page_links(stored, doc)
        locked = model.locked_changes(stored, doc)
        if locked:
            raise _locked_error(locked)
        link_pages(doc["nodes"], user_id)
        version = _store(layout["id"], doc)
        seq = _append_event(layout["id"], "snapshot", {}, user_id, session_id)
        _trim_events(layout["id"])
        record_history(layout["id"], user_id, "saved", coalesce=True)
    _emit("canvas.updated", layout["id"], user_id)
    return {"version": version, "seq": seq}


# ── Op log ────────────────────────────────────────────────────────────────────


def head_seq(layout_id: int) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(seq), 0) FROM canvas__events WHERE layout_id = ?",
                         (layout_id,), default=0))


def _append_event(layout_id: int, op_type: str, payload: dict[str, Any], user_id: str | None,
                  session_id: str) -> int:
    seq = head_seq(layout_id) + 1
    db.insert("canvas__events", {
        "layout_id": layout_id, "seq": seq, "op_type": op_type,
        "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "by_user_id": user_id, "by_session": (session_id or "")[:64], "created_at": now_sql(),
    })
    return seq


def _trim_events(layout_id: int) -> None:
    db.execute("DELETE FROM canvas__events WHERE layout_id = ? AND seq <= ?",
               (layout_id, head_seq(layout_id) - EVENTS_KEEP))


def events_since(layout_id: int, since: int, *, exclude_session: str = "") -> dict[str, Any]:
    """Operations after *since* by other sessions.

    ``seq`` is where the client resumes. ``reset`` tells a client whose
    position is no longer in the log (it fell too far behind) to reload.
    """
    head = head_seq(layout_id)
    oldest = int(db.scalar("SELECT COALESCE(MIN(seq), 0) FROM canvas__events WHERE layout_id = ?",
                           (layout_id,), default=0))
    if since > head or (since and oldest and since < oldest - 1):
        return {"events": [], "seq": head, "reset": True}
    rows = db.all(
        "SELECT seq, op_type, payload, by_user_id, created_at FROM canvas__events "
        "WHERE layout_id = ? AND seq > ? AND by_session != ? ORDER BY seq LIMIT ?",
        (layout_id, since, exclude_session[:64], SYNC_BATCH + 1),
    )
    cursor = head
    if len(rows) > SYNC_BATCH:
        rows = rows[:SYNC_BATCH]
        cursor = rows[-1]["seq"]
    events = []
    for row in rows:
        payload = _event_payload(row["op_type"], row["payload"])
        if payload is None:
            continue
        events.append({"seq": row["seq"], "op_type": row["op_type"], "payload": payload,
                       "by_user_id": row["by_user_id"], "created_at": row["created_at"]})
    return {"events": events, "seq": cursor, "reset": False}


def _event_payload(op_type: str, raw: str | None) -> dict[str, Any] | None:
    """The payload of a logged operation, cleaned like stored data (rows may predate 1.6)."""
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if op_type == "upsert_node":
        node = model.clean_node(payload.get("node"))
        return {"op": op_type, "node": node} if node else None
    if op_type == "upsert_edge":
        edge = model.clean_edge(payload.get("edge"))
        return {"op": op_type, "edge": edge} if edge else None
    if op_type in ("delete_node", "delete_edge"):
        item_id = model.clean_id(payload.get("id"))
        return {"op": op_type, "id": item_id} if item_id else None
    return {} if op_type == "snapshot" else None


# ── History ───────────────────────────────────────────────────────────────────


def record_history(layout_id: int, user_id: str | None, message: str, *, is_revert: bool = False,
                   coalesce: bool = False) -> int | None:
    """Snapshot the canvas; with *coalesce*, update the latest entry when it is the same session of work."""
    row = db.one("SELECT title, description, data FROM canvas__layouts WHERE id = ?", (layout_id,))
    if row is None:
        return None
    latest = db.one(
        "SELECT id, title, description, data, edited_by, edit_message, is_revert, created_at "
        "FROM canvas__history WHERE layout_id = ? ORDER BY id DESC LIMIT 1",
        (layout_id,),
    )
    state = (row["title"], row["description"], row["data"])
    if latest and not is_revert and (latest["title"], latest["description"], latest["data"]) == state:
        return None
    if coalesce and latest and _same_session(latest, user_id):
        db.execute("UPDATE canvas__history SET title = ?, description = ?, data = ? WHERE id = ?",
                   (*state, latest["id"]))
        return latest["id"]
    entry_id = db.insert("canvas__history", {
        "layout_id": layout_id, "title": row["title"], "description": row["description"], "data": row["data"],
        "edited_by": user_id, "edit_message": message, "is_revert": 1 if is_revert else 0,
        "created_at": now_sql(),
    })
    _trim_history("WHERE layout_id = ?", (layout_id,))
    return entry_id


def _same_session(latest: dict[str, Any], user_id: str | None) -> bool:
    """Whether a change by *user_id* belongs in the *latest* history entry.

    The entry keeps its author and message; any edit, save or title change
    joins it, so alternating between them cannot add an entry per request.
    """
    if latest["is_revert"] or latest["edit_message"] not in _COALESCED_MESSAGES:
        return False
    if latest["edited_by"] == user_id and latest["created_at"] >= sql_in(minutes=-HISTORY_COALESCE_MINUTES):
        return True
    return latest["created_at"] >= sql_in(seconds=-HISTORY_MIN_SECONDS)


def _trim_history(where: str, params: tuple[Any, ...]) -> int:
    """Drop entries past the newest :data:`HISTORY_KEEP` or :data:`HISTORY_MAX_BYTES` of each canvas."""
    newest = "(PARTITION BY layout_id ORDER BY id DESC)"
    return db.execute(
        f"DELETE FROM canvas__history WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER {newest} "
        f"AS position, SUM({_SNAPSHOT_BYTES}) OVER {newest} AS kept FROM canvas__history {where}) "
        f"WHERE position > 1 AND (position > ? OR kept > ?))",
        (*params, HISTORY_KEEP, HISTORY_MAX_BYTES),
    ).rowcount


def history(layout_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT h.id, h.title, h.edited_by, h.edit_message, h.is_revert, h.created_at, u.username, "
        "length(h.data) AS size FROM canvas__history h LEFT JOIN users u ON u.id = h.edited_by "
        "WHERE h.layout_id = ? ORDER BY h.id DESC",
        (layout_id,),
    )


def history_entry(layout: dict[str, Any], entry_id: int) -> dict[str, Any] | None:
    """One entry of *layout*'s history (never another canvas's)."""
    return db.one(
        "SELECT h.*, u.username FROM canvas__history h LEFT JOIN users u ON u.id = h.edited_by "
        "WHERE h.id = ? AND h.layout_id = ?",
        (entry_id, layout["id"]),
    )


def revert(layout: dict[str, Any], entry: dict[str, Any], *, user_id: str, session_id: str) -> int:
    doc = model.clean_document(entry["data"])
    link_pages(doc["nodes"], user_id)
    title = (entry["title"] or layout["title"])[:MAX_TITLE]
    with db.transaction():
        db.execute("UPDATE canvas__layouts SET title = ?, description = ? WHERE id = ?",
                   (title, (entry["description"] or "")[:MAX_DESCRIPTION], layout["id"]))
        version = _store(layout["id"], doc)
        _append_event(layout["id"], "snapshot", {}, user_id, session_id)
        _trim_events(layout["id"])
        record_history(layout["id"], user_id, f"reverted:{entry['id']}", is_revert=True)
    _emit("canvas.updated", layout["id"], user_id)
    return version


def delete_history_entry(layout: dict[str, Any], entry_id: int) -> bool:
    return db.execute("DELETE FROM canvas__history WHERE id = ? AND layout_id = ?",
                      (entry_id, layout["id"])).rowcount > 0


def clear_history(layout: dict[str, Any]) -> None:
    db.execute("DELETE FROM canvas__history WHERE layout_id = ?", (layout["id"],))


# ── Maintenance ───────────────────────────────────────────────────────────────


def prune() -> dict[str, int]:
    """Background job: drop old operations and history beyond the per-canvas caps."""
    events = db.execute(
        "DELETE FROM canvas__events WHERE created_at < ? AND seq < "
        "(SELECT MAX(e.seq) FROM canvas__events e WHERE e.layout_id = canvas__events.layout_id)",
        (sql_in(hours=-EVENTS_MAX_AGE_HOURS),),
    ).rowcount
    return {"events": events, "history": _trim_history("", ())}


def referenced_upload_names() -> set[str]:
    """Upload-folder files that a canvas or one of its saved revisions shows.

    For the upload cleanup, which must keep these even while the canvas
    feature is switched off.
    """
    names: set[str] = set()
    for table in ("canvas__layouts", "canvas__history"):
        for raw in db.column(f"SELECT data FROM {table} WHERE instr(data, '/static/uploads/') > 0"):
            names |= model.upload_names(model.clean_document(raw))
    return names
