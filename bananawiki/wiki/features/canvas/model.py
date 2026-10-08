"""The canvas document: nodes, edges and the viewport, and how they are cleaned.

A layout stores one JSON document in ``canvas__layouts.data``::

    {"nodes": [...], "edges": [...], "viewport": {"x": 0, "y": 0, "zoom": 1}}

Every write path (whole-document save, operations, import, history restore)
and every read path runs through :func:`clean_document`, so a collaborator
can never store markup or unexpected fields, and rows written by older
versions reach viewers in the same clean shape. The field names are the 1.4
ones, so existing canvases and exports keep working.

Node types: ``text`` (Markdown note), ``code``, ``image``, ``video``,
``wiki_page`` and ``external_link``. Text nodes can be drawn as shapes. Nodes
may belong to a ``group`` (an id shared by the members, which then move
together) and be ``locked`` so the editor does not move, resize or delete
them by accident (any editor can unlock them again). Edges join
two nodes (``from`` / ``to``) and carry an optional label, arrowheads, a line
style, a ``route`` (curved, straight or elbow) and the side of each node they
attach to (``from_side`` / ``to_side``: top, right, bottom or left; absent
means "auto", the side facing the other node).

Locks are enforced here too, not only in the editor: an operation that
moves, resizes, edits or deletes a locked node is refused (see
:func:`lock_violation`). Unlocking, restacking (``layer``) and grouping stay
allowed, so a batch that first unlocks a node may then change it.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_NODES = 2000
MAX_EDGES = 5000
MAX_OPS = 500

NODE_TYPES = ("text", "code", "image", "video", "wiki_page", "external_link")
_LEGACY_TYPES = {"title": "text", "note": "text", "link": "external_link", "page": "wiki_page"}
SHAPES = ("rectangle", "rounded", "pill", "ellipse", "diamond", "note", "parallelogram", "hexagon")
ARROWS = ("end", "start", "both", "none")
LINE_STYLES = ("solid", "dashed", "dotted")
ROUTES = ("curved", "straight", "elbow")
SIDES = ("top", "right", "bottom", "left")
#: Node fields a locked node may still change: the lock itself, its stacking and its group.
LOCK_FREE_FIELDS = frozenset({"locked", "layer", "group"})
#: Wiki-page fields the server derives from the linked page (and hides from some readers).
_DERIVED_PAGE_FIELDS = frozenset({"label", "page_slug", "deleted"})

DEFAULT_TEXT_SIZE = 13
DEFAULT_EDGE_TEXT_SIZE = 14
_LEGACY_TEXT_SIZES = {"small": 12, "normal": 13, "large": 16, "xlarge": 19}
DEFAULT_SIZES = {
    "text": (240, 120), "code": (320, 200), "image": (320, 240), "video": (360, 280),
    "wiki_page": (280, 200), "external_link": (240, 100),
}
EMPTY_DOCUMENT: dict[str, Any] = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+#.\-]{1,32}$")
_ID = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_LOCAL_UPLOAD = re.compile(r"^/static/uploads/[A-Za-z0-9_\-.]+$")
_STRING_LIMITS = {
    "label": 200, "display_text": 200, "content": 20_000, "alt": 300, "video_id": 64, "provider": 32,
    "icon": 32, "category": 64,
}
_URL_FIELDS = ("url", "image_url", "embed_url")
_COLOR_FIELDS = ("color", "border_color", "background_color", "text_color")


class DocumentError(ValueError):
    """The document or an operation cannot be stored; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Scalars ───────────────────────────────────────────────────────────────────


def _number(value: Any, low: float, high: float, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return default
        else:
            return default
    if not math.isfinite(value):
        return default
    value = float(min(max(float(value), low), high))
    return int(value) if value.is_integer() else round(value, 2)


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value[:limit] if isinstance(value, str) else None


def text_size(value: Any, default: int = DEFAULT_TEXT_SIZE) -> int:
    if isinstance(value, str) and value in _LEGACY_TEXT_SIZES:
        return _LEGACY_TEXT_SIZES[value]
    size = _number(value, 8, 96)
    return default if size is None else int(round(size))


def clean_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if _ID.match(text) else None


def safe_url(value: Any, *, kind: str = "link") -> str:
    """A URL a viewer may follow or load, or ``""``.

    Links accept http(s), mailto and site-relative paths; images accept
    http(s) and this wiki's uploads; embeds accept http(s) only. Characters
    browsers ignore inside a scheme are removed before it is checked.
    """
    if not isinstance(value, str):
        return ""
    url = value.strip()[:2000]
    if not url:
        return ""
    compact = _CONTROL.sub("", url)
    if kind == "image" and _LOCAL_UPLOAD.match(compact):
        return compact
    if compact.startswith("/") and not compact.startswith("//"):
        return url if kind == "link" else ""
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", compact)
    if not scheme:
        return ""
    allowed = {"http", "https", "mailto"} if kind == "link" else {"http", "https"}
    return url if scheme.group(1).lower() in allowed else ""


# ── Nodes and edges ───────────────────────────────────────────────────────────


def node_type(value: Any) -> str:
    kind = str(value or "text").strip().lower()
    kind = _LEGACY_TYPES.get(kind, kind)
    return kind if kind in NODE_TYPES else "text"


def clean_node(raw: Any) -> dict[str, Any] | None:
    """Return a node holding only known, well-typed fields, or ``None``."""
    if not isinstance(raw, dict):
        return None
    node_id = clean_id(raw.get("id"))
    if node_id is None:
        return None
    kind = node_type(raw.get("type"))
    width, height = DEFAULT_SIZES[kind]
    node: dict[str, Any] = {
        "id": node_id,
        "type": kind,
        "x": _number(raw.get("x"), -1e6, 1e6, 0),
        "y": _number(raw.get("y"), -1e6, 1e6, 0),
        "width": _number(raw.get("width"), 40, 4000, width),
        "height": _number(raw.get("height"), 30, 4000, height),
        "layer": int(_number(raw.get("layer"), 0, 1e6, 0) or 0),
        "text_size": text_size(raw.get("text_size")),
    }
    for key, limit in _STRING_LIMITS.items():
        value = _text(raw.get(key), limit)
        if value is not None:
            node[key] = value
    language = raw.get("language")
    if isinstance(language, str) and _LANGUAGE.match(language.strip()):
        node["language"] = language.strip().lower()
    for key in _COLOR_FIELDS:
        value = raw.get(key)
        if isinstance(value, str) and _COLOR.match(value):
            node[key] = value.lower()
    url_kind = {"image": "image", "video": "embed"}.get(kind, "link")
    for key in _URL_FIELDS:
        value = safe_url(raw.get(key), kind="image" if key == "image_url" else url_kind)
        if value:
            node[key] = value
    page_id = raw.get("page_id")
    if isinstance(page_id, (int, str)) and not isinstance(page_id, bool) and str(page_id).isdigit():
        node["page_id"] = int(page_id)
    page_slug = _text(raw.get("page_slug"), 200)
    if page_slug:
        node["page_slug"] = page_slug
    if raw.get("deleted") is True:
        node["deleted"] = True
    group = clean_id(raw.get("group"))
    if group is not None:
        node["group"] = group
    if raw.get("locked") is True:
        node["locked"] = True
    rotation = _number(raw.get("rotation"), -360, 360)
    if rotation:
        node["rotation"] = rotation
    opacity = _number(raw.get("opacity"), 0.1, 1)
    if opacity is not None and opacity != 1:
        node["opacity"] = opacity
    if raw.get("shape") in SHAPES:
        node["shape"] = raw["shape"]
    metadata = raw.get("metadata")
    if isinstance(metadata, dict) and len(json.dumps(metadata, default=str)) <= 2000:
        node["metadata"] = json.loads(json.dumps(metadata, default=str))
    return node


def clean_edge(raw: Any) -> dict[str, Any] | None:
    """Return an edge with known fields; 1.4 ``source``/``target`` become ``from``/``to``."""
    if not isinstance(raw, dict):
        return None
    edge_id = clean_id(raw.get("id"))
    start = clean_id(raw.get("from", raw.get("source")))
    end = clean_id(raw.get("to", raw.get("target")))
    if edge_id is None or start is None or end is None or start == end:
        return None
    edge: dict[str, Any] = {"id": edge_id, "from": start, "to": end,
                            "label": _text(raw.get("label"), 200) or "",
                            "text_size": text_size(raw.get("text_size"), DEFAULT_EDGE_TEXT_SIZE)}
    color = raw.get("color")
    if isinstance(color, str) and _COLOR.match(color):
        edge["color"] = color.lower()
    if raw.get("arrow") in ARROWS:
        edge["arrow"] = raw["arrow"]
    if raw.get("style") in LINE_STYLES:
        edge["style"] = raw["style"]
    if raw.get("route") in ROUTES and raw["route"] != "curved":
        edge["route"] = raw["route"]
    for key in ("from_side", "to_side"):
        if raw.get(key) in SIDES:
            edge[key] = raw[key]
    return edge


def clean_viewport(raw: Any) -> dict[str, float]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "x": _number(raw.get("x"), -1e7, 1e7, 0),
        "y": _number(raw.get("y"), -1e7, 1e7, 0),
        "zoom": _number(raw.get("zoom"), 0.1, 4, 1),
    }


def clean_document(raw: Any, *, strict: bool = False) -> dict[str, Any]:
    """A clean copy of a whole document; anything unreadable becomes empty.

    Duplicate ids keep their last occurrence, and edges whose ends are not
    nodes of the document are dropped. With *strict* (documents sent by
    clients and imports) a document listing more nodes or edges than a canvas
    may hold is refused before any of them is cleaned.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    raw = raw if isinstance(raw, dict) else {}
    raw_nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    raw_edges = raw.get("edges") if isinstance(raw.get("edges"), list) else []
    if strict:
        check_counts(len(raw_nodes), len(raw_edges))
    nodes: dict[str, dict[str, Any]] = {}
    for item in raw_nodes:
        node = clean_node(item)
        if node is not None:
            nodes.pop(node["id"], None)
            nodes[node["id"]] = node
    edges: dict[str, dict[str, Any]] = {}
    for item in raw_edges:
        edge = clean_edge(item)
        if edge is not None and edge["from"] in nodes and edge["to"] in nodes:
            edges.pop(edge["id"], None)
            edges[edge["id"]] = edge
    return {"nodes": list(nodes.values()), "edges": list(edges.values()),
            "viewport": clean_viewport(raw.get("viewport"))}


def check_counts(nodes: int, edges: int) -> None:
    if nodes > MAX_NODES:
        raise DocumentError("canvas.error.too_many_nodes", limit=MAX_NODES)
    if edges > MAX_EDGES:
        raise DocumentError("canvas.error.too_many_edges", limit=MAX_EDGES)


def serialize(document: dict[str, Any]) -> str:
    """Stored form of a clean document; refuses documents over the limits."""
    check_counts(len(document["nodes"]), len(document["edges"]))
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise DocumentError("canvas.error.too_large", limit_mb=MAX_DOCUMENT_BYTES // (1024 * 1024))
    return text


# ── Operations ────────────────────────────────────────────────────────────────


def _protected(node: dict[str, Any]) -> dict[str, Any]:
    skip = LOCK_FREE_FIELDS | (_DERIVED_PAGE_FIELDS if node["type"] == "wiki_page" else frozenset())
    return {key: value for key, value in node.items() if key not in skip}


def lock_violation(current: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """Whether replacing node *current* with *new* (``None``: deleting it) breaks its lock.

    A locked node may only be unlocked, restacked or regrouped; anything else
    (position, size, content, style, deletion) needs an earlier operation that
    unlocks it. Unlocking and editing in the same operation counts as editing,
    so a stale client that never saw the lock cannot override it.
    """
    if current is None or not current.get("locked"):
        return False
    return new is None or _protected(current) != _protected(new)


def apply_ops(document: dict[str, Any], ops: Any,
              rejected: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Apply *ops* to *document* in place and return the operations that took effect.

    Operations (``op`` or, as in 1.4, ``type``)::

        {"op": "upsert_node", "node": {...}}      add or replace a node
        {"op": "delete_node", "id": "..."}        remove a node and its edges
        {"op": "upsert_edge", "edge": {...}}      add or replace an edge
        {"op": "delete_edge", "id": "..."}
        {"op": "viewport_set", "viewport": {...}} change the stored default view

    Edges may only join nodes that exist once the preceding operations ran.
    Operations that break a lock (:func:`lock_violation`) are skipped and, when
    a *rejected* list is given, recorded there as ``{"op", "id", "reason"}``.
    """
    if not isinstance(ops, list):
        raise DocumentError("canvas.error.bad_request")
    if len(ops) > MAX_OPS:
        raise DocumentError("canvas.error.too_many_ops", limit=MAX_OPS)
    nodes = {node["id"]: node for node in document["nodes"]}
    edges = {edge["id"]: edge for edge in document["edges"]}
    applied: list[dict[str, Any]] = []
    for raw in ops:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("op") or raw.get("type")
        if kind == "upsert_node":
            node = clean_node(raw.get("node"))
            if node is not None:
                keep_page_link(nodes.get(node["id"]), node)
            if node is not None and lock_violation(nodes.get(node["id"]), node):
                _reject(rejected, kind, node["id"])
            elif node is not None:
                nodes[node["id"]] = node
                applied.append({"op": kind, "node": node})
        elif kind == "delete_node":
            node_id = clean_id(raw.get("id"))
            if node_id is not None and lock_violation(nodes.get(node_id), None):
                _reject(rejected, kind, node_id)
            elif node_id is not None and nodes.pop(node_id, None) is not None:
                edges = {key: e for key, e in edges.items() if node_id not in (e["from"], e["to"])}
                applied.append({"op": kind, "id": node_id})
        elif kind == "upsert_edge":
            edge = clean_edge(raw.get("edge"))
            if edge is not None and edge["from"] in nodes and edge["to"] in nodes:
                edges[edge["id"]] = edge
                applied.append({"op": kind, "edge": edge})
        elif kind == "delete_edge":
            edge_id = clean_id(raw.get("id"))
            if edge_id is not None and edges.pop(edge_id, None) is not None:
                applied.append({"op": kind, "id": edge_id})
        elif kind == "viewport_set":
            document["viewport"] = clean_viewport(raw.get("viewport"))
            applied.append({"op": kind, "viewport": document["viewport"]})
    document["nodes"] = list(nodes.values())
    document["edges"] = list(edges.values())
    return applied


def _reject(rejected: list[dict[str, Any]] | None, kind: str, node_id: str) -> None:
    if rejected is not None:
        rejected.append({"op": kind, "id": node_id, "reason": "locked"})


def locked_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Ids of locked nodes of *before* that the whole document *after* changes or drops."""
    new = {node["id"]: node for node in after["nodes"]}
    return [node["id"] for node in before["nodes"] if lock_violation(node, new.get(node["id"]))]


def drop_page_details(node: dict[str, Any]) -> None:
    """Remove what a wiki-page node shows of its page; only a slug without a page id stays."""
    node.pop("label", None)
    node.pop("deleted", None)
    if node.get("page_id"):
        node.pop("page_slug", None)


def keep_page_link(current: dict[str, Any] | None, new: dict[str, Any]) -> None:
    """Give wiki-page node *new* the page of *current* when it arrives without one.

    Readers who may not see a linked page receive its node without the page
    (see :mod:`.present`), so what they save carries the stored link forward.
    A locked node always keeps its stored page, whatever page *new* names:
    otherwise the lock check would tell a reader who guesses page ids which
    one the node links to.
    """
    if current is None or new["type"] != "wiki_page" or current["type"] != "wiki_page":
        return
    if not current.get("locked") and (new.get("page_id") or new.get("page_slug")):
        return
    for key in ("page_id", "page_slug"):
        if current.get(key):
            new[key] = current[key]
        else:
            new.pop(key, None)


def keep_page_links(before: dict[str, Any], after: dict[str, Any]) -> None:
    """:func:`keep_page_link` for every node of the whole document *after*."""
    old = {node["id"]: node for node in before["nodes"]}
    for node in after["nodes"]:
        keep_page_link(old.get(node["id"]), node)


# ── Helpers used by exports, imports and upload cleanup ──────────────────────

_UPLOAD_NAME = re.compile(r"^/static/uploads/([A-Za-z0-9_\-.]+)$")


def upload_names(document: dict[str, Any]) -> set[str]:
    """Names of files in this wiki's upload folder that the document shows."""
    names = set()
    for node in document.get("nodes", []):
        for key in ("url", "image_url"):
            match = _UPLOAD_NAME.match(str(node.get(key) or ""))
            if match:
                names.add(match.group(1))
    return names


def rename_uploads(document: dict[str, Any], mapping: dict[str, str]) -> None:
    for node in document.get("nodes", []):
        for key in ("url", "image_url"):
            match = _UPLOAD_NAME.match(str(node.get(key) or ""))
            if match and match.group(1) in mapping:
                node[key] = "/static/uploads/" + mapping[match.group(1)]
