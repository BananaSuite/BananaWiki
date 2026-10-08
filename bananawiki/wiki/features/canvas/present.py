"""What a viewer receives for a canvas: nodes, server-rendered HTML and page previews.

The stored document never contains markup. Text nodes are rendered with the
wiki's Markdown renderer (sanitised), code nodes with its highlighter and
video nodes resolve to a supported player URL, all here on the server, so
the browser only ever inserts HTML that came from :mod:`...markdown`.

Renderings (and page excerpts and outline text) are kept per process by the
hash of their source, up to :data:`CACHE_LIMIT` bytes, so reading a
canvas again renders only what changed. One answer renders at most
:data:`RENDER_LIMIT` characters of source, cached or not, in document order;
nodes past it are sent without HTML and the browser shows their text as it
is (code without a language is shown as plain text, never guessed).

Wiki-page nodes are shown with the page's current title, slug and excerpt
only when the viewer may read that page (1.4 showed stored page titles to
everyone who could open the canvas). Otherwise the node loses its page id,
slug and title and is marked ``restricted``, whether the page exists or not,
so a canvas cannot be used to find out which pages exist; only
administrators, who may read every page, see ``deleted`` instead.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ... import auth, markdown
from ...db import db
from ..pages import service as pages
from . import model
from .service import _pages_by

#: Characters of source one answer renders (Markdown, highlighting, excerpts, outline text): about 1.5 s of
#: work when nothing is cached, and room for the notes of an ordinary canvas many times over.
RENDER_LIMIT = 512 * 1024
#: Bytes of rendered HTML and text kept per process (as Python holds them: up to 4 per character).
CACHE_LIMIT = 32 * 1024 * 1024
_ENTRY_COST = 256
#: Highlighting with a lexer costs about twice as much per character as Markdown.
_HIGHLIGHT_WEIGHT = 2

_PAGE_COLUMNS = (
    "id, title, slug, category_id, pending_deletion, is_deindexed, builder_public, "
    "CASE WHEN builder_json != '' THEN 1 ELSE 0 END AS has_builder"
)
_PAGE_FIELDS = ("page_id", "page_slug", "label", "deleted", "restricted")


class RenderBudget:
    """What one answer may still render; once something does not fit, nothing more is rendered."""

    def __init__(self, limit: int | None = None) -> None:
        self.left = RENDER_LIMIT if limit is None else limit

    def take(self, cost: int) -> bool:
        if cost > self.left:
            self.left = 0
            return False
        self.left -= cost
        return True


class _Cache:
    """Renderings by kind and source hash, least recently used dropped first."""

    def __init__(self) -> None:
        self._items: OrderedDict[tuple[str, bytes], str] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[str, bytes]) -> str | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple[str, bytes], value: str) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= _footprint(old)
            self._items[key] = value
            self._size += _footprint(value)
            while self._size > CACHE_LIMIT and self._items:
                _key, dropped = self._items.popitem(last=False)
                self._size -= _footprint(dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


def _footprint(value: str) -> int:
    return sys.getsizeof(value) + _ENTRY_COST


_cache = _Cache()


def _cached(kind: str, source: str, make: Callable[[], str], budget: RenderBudget | None,
            weight: int = 1) -> str | None:
    """``make()``, the rendering of *source*, from the cache when possible; ``None`` once *budget* is spent."""
    if budget is not None and not budget.take(len(source) * weight):
        return None
    key = (kind, hashlib.sha256(source.encode("utf-8", "surrogatepass")).digest())
    value = _cache.get(key)
    if value is None:
        value = make()
        _cache.put(key, value)
    return value


def source_key(node: dict[str, Any]) -> str | None:
    """The input a rendering was made from; the browser compares it to the node."""
    if node["type"] == "text":
        return node.get("content") or ""
    if node["type"] == "code":
        return (node.get("language") or "") + "\n" + (node.get("content") or "")
    if node["type"] == "video":
        return node.get("url") or ""
    return None


def rendering(node: dict[str, Any], budget: RenderBudget | None = None) -> dict[str, str] | None:
    """The server rendering of a text, code or video node; ``None`` for other nodes or past *budget*."""
    key = source_key(node)
    if key is None:
        return None
    if node["type"] == "text":
        html = _cached("text", key, lambda: markdown.render(key, embed_videos=False), budget) if key.strip() else ""
        return None if html is None else {"type": "text", "for": key, "html": html}
    if node["type"] == "code":
        content, language = node.get("content") or "", node.get("language") or ""
        html = _cached("code", key, lambda: markdown.highlight_code(content, language or "text"), budget,
                       _HIGHLIGHT_WEIGHT if language else 1) if content else ""
        return None if html is None else {"type": "code", "for": key, "html": html}
    return {"type": "video", "for": key, "embed": markdown.video_embed_src(key) or ""}


def _excerpt(content: str, budget: RenderBudget) -> str:
    return _cached("excerpt", content, lambda: markdown.excerpt(content, 240), budget) or ""


def _readable_pages(nodes: list[dict[str, Any]], user: dict[str, Any] | None, *,
                    excerpts: bool) -> list[dict[str, Any]]:
    """The pages *nodes* link to that *user* may read (with ``content`` for *excerpts*)."""
    wiki = [node for node in nodes if node["type"] == "wiki_page"]
    ids = sorted({node["page_id"] for node in wiki if node.get("page_id")})
    slugs = sorted({node["page_slug"] for node in wiki if not node.get("page_id") and node.get("page_slug")})
    readable = [page for page in _pages_by(ids, slugs, _PAGE_COLUMNS)
                if pages.can_view(page, user, anonymous=user is None)]
    if excerpts and readable:
        marks = ",".join("?" for _ in readable)
        contents = {row["id"]: row["content"] for row in db.all(
            f"SELECT id, substr(content, 1, 3000) AS content FROM pages WHERE id IN ({marks})",
            [page["id"] for page in readable])}
        for page in readable:
            page["content"] = contents.get(page["id"]) or ""
    return readable


def present_nodes(nodes: list[dict[str, Any]], user: dict[str, Any] | None, *, render: bool = True,
                  excerpts: bool | None = None, budget: RenderBudget | None = None) -> dict[str, Any]:
    """``{"nodes", "rendered", "pages"}`` for *nodes* as *user* may see them.

    Without *render* (exports) nodes are not rendered, and pages carry an
    excerpt only with *excerpts* (which follows *render* unless given).
    Rendering stops once *budget* (a new :class:`RenderBudget` unless given) is
    spent: later nodes come without HTML and later pages without excerpt.
    """
    excerpts = render if excerpts is None else excerpts
    budget = RenderBudget() if budget is None else budget
    readable = _readable_pages(nodes, user, excerpts=excerpts)
    by_id = {page["id"]: page for page in readable}
    by_slug = {page["slug"]: page for page in readable}
    sees_every_page = user is not None and auth.is_admin(user)
    visible: dict[str, dict[str, str]] = {}
    out_nodes = []
    rendered: dict[str, dict[str, str]] = {}
    for node in nodes:
        node = dict(node)
        if node["type"] == "wiki_page":
            page = by_id.get(node.get("page_id")) if node.get("page_id") else by_slug.get(node.get("page_slug"))
            linked = any(node.get(key) for key in ("page_id", "page_slug", "deleted"))
            for key in _PAGE_FIELDS:
                node.pop(key, None)
            if page is not None:
                node.update(page_id=page["id"], page_slug=page["slug"], label=page["title"])
                if str(page["id"]) not in visible:  # each page's excerpt is made (and paid for) once
                    visible[str(page["id"])] = {
                        "title": page["title"], "slug": page["slug"],
                        "excerpt": _excerpt(page["content"], budget) if excerpts else "",
                    }
            elif linked:
                # Missing and unreadable pages look the same to anyone who cannot read every page.
                node["label"] = ""
                node["deleted" if sees_every_page else "restricted"] = True
        elif render:
            entry = rendering(node, budget)
            if entry is not None:
                rendered[node["id"]] = entry
        out_nodes.append(node)
    return {"nodes": out_nodes, "rendered": rendered, "pages": visible}


def present_document(doc: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    shown = present_nodes(doc["nodes"], user)
    return {
        "data": {"nodes": shown["nodes"], "edges": doc["edges"], "viewport": doc["viewport"]},
        "rendered": shown["rendered"],
        "pages": shown["pages"],
    }


def redacted_document(doc: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    """The document as *user* may see it, without renderings (exports and the REST API).

    Wiki-page nodes carry the title and slug of pages *user* may read and
    nothing about the others.
    """
    nodes = [{key: value for key, value in node.items() if key != "restricted"}
             for node in present_nodes(doc["nodes"], user, render=False)["nodes"]]
    return {**doc, "nodes": nodes}


def present_events(events: list[dict[str, Any]], user: dict[str, Any] | None) -> dict[str, Any]:
    """Apply viewer rules to the nodes carried by ``upsert_node`` events."""
    nodes = [event["payload"]["node"] for event in events
             if event["op_type"] == "upsert_node" and isinstance(event["payload"].get("node"), dict)]
    shown = present_nodes(nodes, user)
    replacements = iter(shown["nodes"])
    for event in events:
        if event["op_type"] == "upsert_node" and isinstance(event["payload"].get("node"), dict):
            event["payload"] = {"op": "upsert_node", "node": next(replacements)}
    return {"rendered": shown["rendered"], "pages": shown["pages"]}


# ── Text outline ──────────────────────────────────────────────────────────────

OUTLINE_TEXT_LIMIT = 4000
_ROW_HEIGHT = 80


def _outline_text(node: dict[str, Any], page: dict[str, str] | None, budget: RenderBudget) -> str:
    if node["type"] == "text":
        content = node.get("content") or ""
        plain = _cached("plain", content, lambda: markdown.to_plain_text(content), budget)
        return (content if plain is None else plain)[:OUTLINE_TEXT_LIMIT]
    if node["type"] == "code":
        return (node.get("content") or "")[:OUTLINE_TEXT_LIMIT]
    if node["type"] == "image":
        return node.get("alt") or ""
    if node["type"] == "wiki_page":
        return page["excerpt"] if page else ""
    return node.get("url") or ""


def outline(doc: dict[str, Any], user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The canvas as a list a screen reader (or a Markdown file) can follow.

    Elements come in reading order (top to bottom in rows, then left to
    right), each with its text and the connections leading out of and into
    it. Wiki-page nodes follow the same visibility rules as the canvas view.
    Text past the render budget is given as its Markdown source.
    """
    budget = RenderBudget()
    shown = present_nodes(doc["nodes"], user, render=False, excerpts=True, budget=budget)
    nodes = sorted(shown["nodes"], key=lambda node: (int(node["y"] // _ROW_HEIGHT), node["x"], node["y"]))
    groups: dict[str, int] = {}
    items: dict[str, dict[str, Any]] = {}
    for node in nodes:
        page = shown["pages"].get(str(node.get("page_id"))) if node["type"] == "wiki_page" else None
        text = _outline_text(node, page, budget)
        title = node.get("display_text") or (page["title"] if page else "")
        if not title and node["type"] in ("external_link", "image", "video"):
            title = node.get("label") or ""
        group = node.get("group")
        if group:
            groups.setdefault(group, len(groups) + 1)
        items[node["id"]] = {
            "id": node["id"], "type": node["type"], "title": title, "text": text,
            "name": title or " ".join(text.split())[:60] or "",
            "shape": node.get("shape") or "", "locked": bool(node.get("locked")),
            "group": groups.get(group) if group else None,
            "page": page, "page_state": "deleted" if node.get("deleted") else
            "restricted" if node.get("restricted") else "",
            "url": model.safe_url(node.get("url")) if node["type"] == "external_link" else "",
            "outgoing": [], "incoming": [],
        }
    for edge in doc["edges"]:
        start, end = items.get(edge["from"]), items.get(edge["to"])
        if start is None or end is None:
            continue
        both_ways = edge.get("arrow") in ("both", "none")
        start["outgoing"].append({"id": end["id"], "label": edge.get("label") or "", "both": both_ways})
        end["incoming"].append({"id": start["id"], "label": edge.get("label") or "", "both": both_ways})
    return list(items.values())
