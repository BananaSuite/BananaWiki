"""What a viewer receives for a canvas: nodes, server-rendered HTML and page previews.

The stored document never contains markup. Text nodes are rendered with the
wiki's Markdown renderer (sanitised), code nodes with its highlighter and
video nodes resolve to a supported player URL, all here on the server, so
the browser only ever inserts HTML that came from :mod:`...markdown`.

Wiki-page nodes are shown with the page's current title, slug and excerpt
only when the viewer may read that page; otherwise the title and slug are
removed from the node and it is marked ``restricted`` (1.4 showed stored
page titles to everyone who could open the canvas).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ... import markdown
from ..pages import service as pages
from . import model
from .service import _pages_by

_PAGE_COLUMNS = (
    "id, title, slug, category_id, pending_deletion, is_deindexed, builder_public, "
    "CASE WHEN builder_json != '' THEN 1 ELSE 0 END AS has_builder, substr(content, 1, 3000) AS content"
)


@lru_cache(maxsize=1024)
def _markdown_html(text: str) -> str:
    return markdown.render(text, embed_videos=False)


@lru_cache(maxsize=512)
def _code_html(code: str, language: str) -> str:
    return markdown.highlight_code(code, language or None)


def source_key(node: dict[str, Any]) -> str | None:
    """The input a rendering was made from; the browser compares it to the node."""
    if node["type"] == "text":
        return node.get("content") or ""
    if node["type"] == "code":
        return (node.get("language") or "") + "\n" + (node.get("content") or "")
    if node["type"] == "video":
        return node.get("url") or ""
    return None


def rendering(node: dict[str, Any]) -> dict[str, str] | None:
    key = source_key(node)
    if key is None:
        return None
    if node["type"] == "text":
        return {"type": "text", "for": key, "html": _markdown_html(key) if key.strip() else ""}
    if node["type"] == "code":
        content = node.get("content") or ""
        return {"type": "code", "for": key,
                "html": _code_html(content, node.get("language") or "") if content else ""}
    return {"type": "video", "for": key, "embed": markdown.video_embed_src(key) or ""}


def present_nodes(nodes: list[dict[str, Any]], user: dict[str, Any] | None) -> dict[str, Any]:
    """``{"nodes", "rendered", "pages"}`` for *nodes* as *user* may see them."""
    wiki = [node for node in nodes if node["type"] == "wiki_page"]
    ids = sorted({node["page_id"] for node in wiki if node.get("page_id")})
    slugs = sorted({node["page_slug"] for node in wiki if not node.get("page_id") and node.get("page_slug")})
    found = _pages_by(ids, slugs, _PAGE_COLUMNS)
    by_id = {page["id"]: page for page in found}
    by_slug = {page["slug"]: page for page in found}
    visible: dict[str, dict[str, str]] = {}
    out_nodes = []
    rendered: dict[str, dict[str, str]] = {}
    for node in nodes:
        node = dict(node)
        if node["type"] == "wiki_page":
            page = by_id.get(node.get("page_id")) if node.get("page_id") else by_slug.get(node.get("page_slug"))
            if page is not None and pages.can_view(page, user, anonymous=user is None):
                node.update(page_id=page["id"], page_slug=page["slug"], label=page["title"])
                node.pop("deleted", None)
                visible.setdefault(str(page["id"]), {
                    "title": page["title"], "slug": page["slug"],
                    "excerpt": markdown.excerpt(page["content"], 240),
                })
            else:
                node["label"] = ""
                node.pop("page_slug", None)
                if page is None and (node.get("page_id") or node.get("deleted")):
                    node["deleted"] = True
                else:
                    node.pop("deleted", None)
                    node["restricted"] = True
        else:
            entry = rendering(node)
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
    """The stored document with wiki-page nodes *user* may not read stripped of title and slug (exports)."""
    nodes = [{key: value for key, value in node.items() if key != "restricted"}
             for node in present_nodes(doc["nodes"], user)["nodes"]]
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


def _outline_text(node: dict[str, Any], page: dict[str, str] | None) -> str:
    if node["type"] == "text":
        return markdown.to_plain_text(node.get("content") or "")[:OUTLINE_TEXT_LIMIT]
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
    """
    shown = present_nodes(doc["nodes"], user)
    nodes = sorted(shown["nodes"], key=lambda node: (int(node["y"] // _ROW_HEIGHT), node["x"], node["y"]))
    groups: dict[str, int] = {}
    items: dict[str, dict[str, Any]] = {}
    for node in nodes:
        page = shown["pages"].get(str(node.get("page_id"))) if node["type"] == "wiki_page" else None
        text = _outline_text(node, page)
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
