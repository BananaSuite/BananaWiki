"""Builder documents: strict validation, safe HTML rendering and a Markdown twin.

A document is ``{"version": 2, "blocks": [...]}`` stored in
``pages.builder_json``. Version 1 (1.4 and early 1.6 documents) is read and
upgraded in memory: its text blocks and columns are plain text, so they get
``"format": "plain"``; everything else is unchanged, and its Markdown twin
stays byte-identical, so upgraded pages keep rendering from their document.

Validation is an allow-list: known block types, known keys, bounded text,
links limited to http(s) and local paths, images limited to files uploaded
to this wiki, layout options limited to fixed values. Every text is escaped
when rendered; Markdown text goes through :func:`markdown.render` (sanitised),
YouTube players are built by :func:`markdown.video_iframe` from the validated
video id, code by :func:`markdown.highlight_code`.

Blocks that show other wiki content (page lists, canvas and kanban embeds)
only store references. They are resolved for each reader when the page is
rendered, with that reader's permissions, so they never reveal a page the
reader may not open.

The page's ``content`` column receives :func:`to_markdown` of the document,
so search, exports, history diffs and the plain page view (feature switched
off) keep working.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from flask import g, has_request_context, render_template, url_for
from markupsafe import Markup, escape

from ... import markdown, registry
from ...db import db
from ...i18n import t

VERSION = 2
MAX_BYTES = 256 * 1024
MAX_BLOCKS = 120
MAX_TEXT = 20_000
MAX_PAGE_LIST = 24
MAX_LISTED_PAGES = 48  # entries all page lists of one document show together

V1_BLOCK_TYPES = ("heading", "text", "image", "youtube", "button", "callout", "list", "columns", "divider", "spacer")
# Palette groups, in the order the editor shows them.
PALETTE = (
    ("text", ("heading", "text", "list", "quote", "callout", "code", "table")),
    ("media", ("image", "gallery", "youtube")),
    ("layout", ("hero", "columns", "cards", "button", "faq", "divider", "spacer")),
    ("wiki", ("pages", "embed")),
)
BLOCK_TYPES = tuple(kind for _group, kinds in PALETTE for kind in kinds)

# Per-block layout options: the first value is the default (never stored).
LAYOUT = {
    "align": ("left", "center", "right"),
    "background": ("none", "muted", "primary", "info", "success", "warning"),
    "spacing": ("normal", "compact", "spacious"),
}
TEXT_FORMATS = ("markdown", "plain")
EMBED_KINDS = ("canvas", "kanban")

_UPLOAD_URL = re.compile(r"^/static/uploads/[a-f0-9]{32}\.(?:png|jpe?g|gif|webp|bmp)$", re.IGNORECASE)
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\\]")
_REF = re.compile(r"^[\w-]{1,200}$")
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+#.-]{0,30}$")
_ANCHOR_STRIP = re.compile(r"[^\w\s-]")
_ANCHOR_SPACES = re.compile(r"[\s_]+")
LEGACY_MARKER = '<section class="builder-page">'  # 1.4 stored compiled HTML starting with this


class DocumentError(ValueError):
    """The document does not match the schema; ``key`` is a translation key."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def empty() -> dict[str, Any]:
    return {"version": VERSION, "blocks": []}


# ── Field validators ──────────────────────────────────────────────────────────


def _text(value: Any, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DocumentError("page_builder.error.text_type")
    value = value.replace("\r\n", "\n").strip()
    if required and not value:
        raise DocumentError("page_builder.error.required")
    if len(value) > limit:
        raise DocumentError("page_builder.error.too_long")
    return value


def _link(value: Any, *, required: bool) -> str:
    url = _text(value, limit=2048, required=required)
    if not url:
        return ""
    if _CONTROL.search(url):
        raise DocumentError("page_builder.error.link")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise DocumentError("page_builder.error.link") from None
    if parts.scheme in ("http", "https") and parts.netloc:
        return url
    if not parts.scheme and not parts.netloc and url.startswith("/") and not url.startswith("//"):
        return url
    raise DocumentError("page_builder.error.link")


def _image(value: Any, *, required: bool) -> str:
    url = _text(value, limit=2048, required=required)
    if url and not _UPLOAD_URL.fullmatch(url):
        raise DocumentError("page_builder.error.image")
    return url


def _choice(value: Any, choices: tuple[Any, ...], error: str) -> Any:
    """*value* when it is one of *choices*, the first choice when missing, else an error."""
    if value is None:
        return choices[0]
    if isinstance(value, bool) or value not in choices:
        raise DocumentError(error)
    return value


def _count(value: Any, low: int, high: int, default: int, error: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DocumentError(error)
    return value


def _list(value: Any, limit: int, error: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise DocumentError(error)
    return value


def _objects(value: Any, limit: int, error: str) -> list[dict[str, Any]]:
    items = _list(value, limit, error)
    if not all(isinstance(item, dict) for item in items):
        raise DocumentError(error)
    return items


def _alt(alt: str, url: str, complete: bool) -> str:
    if complete and url and not alt:
        raise DocumentError("page_builder.error.alt_required")
    return alt


def youtube_url(value: Any, *, required: bool) -> str:
    """Canonical ``https://www.youtube.com/watch?v=<id>`` for a YouTube link."""
    url = _text(value, limit=2048, required=required)
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise DocumentError("page_builder.error.youtube") from None
    host = (parts.hostname or "").lower()
    video_id = ""
    if parts.scheme in ("http", "https") and host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if parts.path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif parts.path.startswith(("/shorts/", "/embed/")):
            video_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
    elif parts.scheme in ("http", "https") and host in ("youtu.be", "www.youtu.be"):
        video_id = parts.path.strip("/").split("/", 1)[0]
    if not _YOUTUBE_ID.fullmatch(video_id):
        raise DocumentError("page_builder.error.youtube")
    return f"https://www.youtube.com/watch?v={video_id}"


# ── Blocks ────────────────────────────────────────────────────────────────────


def _heading(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    level = _count(raw.get("level"), 1, 3, 2, "page_builder.error.heading_level")
    return {"level": level, "text": _text(raw.get("text"), limit=300, required=complete)}


def _format(raw: dict[str, Any], version: int) -> str:
    # Version 1 text was always plain; version 2 blocks say which one they use.
    if version == 1:
        return "plain"
    return _choice(raw.get("format"), TEXT_FORMATS, "page_builder.error.format")


def _text_block(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    return {"format": _format(raw, version), "text": _text(raw.get("text"))}


def _image_block(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    url = _image(raw.get("url"), required=complete)
    decorative = raw.get("decorative") is True
    alt = _text(raw.get("alt"), limit=300)
    return {"url": url, "alt": alt if decorative else _alt(alt, url, complete), "decorative": decorative,
            "caption": _text(raw.get("caption"), limit=500),
            "size": _choice(raw.get("size"), ("full", "medium", "small"), "page_builder.error.option")}


def _gallery(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    images = []
    for item in _objects(raw.get("images"), 24, "page_builder.error.gallery"):
        url = _image(item.get("url"), required=complete)
        images.append({"url": url, "alt": _alt(_text(item.get("alt"), limit=300), url, complete),
                       "caption": _text(item.get("caption"), limit=300)})
    if complete and not images:
        raise DocumentError("page_builder.error.required")
    return {"columns": _count(raw.get("columns"), 2, 4, 3, "page_builder.error.option"), "images": images}


def _youtube(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    return {"url": youtube_url(raw.get("url"), required=complete), "caption": _text(raw.get("caption"), limit=500)}


def _button(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    # Version 1 silently turned unknown styles into "primary"; keep reading such documents.
    style = raw.get("style") if raw.get("style") in ("primary", "outline") else "primary"
    return {"label": _text(raw.get("label"), limit=100, required=complete),
            "url": _link(raw.get("url"), required=complete), "style": style}


def _hero(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    label = _text(raw.get("button_label"), limit=100)
    url = _link(raw.get("button_url"), required=False)
    if complete and bool(label) != bool(url):
        raise DocumentError("page_builder.error.button_incomplete")
    image = _image(raw.get("image"), required=False)
    return {"title": _text(raw.get("title"), limit=200, required=complete), "text": _text(raw.get("text"), limit=1000),
            "button_label": label, "button_url": url, "image": image,
            "image_alt": _alt(_text(raw.get("image_alt"), limit=300), image, complete)}


def _cards(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    items = []
    for item in _objects(raw.get("items"), 12, "page_builder.error.cards"):
        image = _image(item.get("image"), required=False)
        items.append({"title": _text(item.get("title"), limit=200, required=complete),
                      "text": _text(item.get("text"), limit=1000), "url": _link(item.get("url"), required=False),
                      "image": image, "image_alt": _alt(_text(item.get("image_alt"), limit=300), image, complete)})
    if complete and not items:
        raise DocumentError("page_builder.error.required")
    return {"columns": _count(raw.get("columns"), 2, 4, 3, "page_builder.error.option"), "items": items}


def _callout(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    tone = raw.get("tone") if raw.get("tone") in ("info", "success", "warning", "danger") else "info"
    return {"title": _text(raw.get("title"), limit=200), "text": _text(raw.get("text"), limit=3000), "tone": tone}


def _quote(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    return {"text": _text(raw.get("text"), limit=2000, required=complete), "cite": _text(raw.get("cite"), limit=200)}


def _list_block(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    cleaned = [_text(item, limit=1000) for item in _list(raw.get("items"), 100, "page_builder.error.list")]
    if complete and not all(cleaned):
        raise DocumentError("page_builder.error.required")
    return {"ordered": bool(raw.get("ordered")), "items": [item for item in cleaned if item]}


def _faq(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    items = [{"question": _text(item.get("question"), limit=300, required=complete),
              "answer": _text(item.get("answer"), limit=5000)}
             for item in _objects(raw.get("items"), 50, "page_builder.error.faq")]
    if complete and not items:
        raise DocumentError("page_builder.error.required")
    return {"items": items}


def _table(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    rows = []
    for row in _list(raw.get("rows"), 50, "page_builder.error.table"):
        rows.append([_text(cell, limit=500) for cell in _list(row, 8, "page_builder.error.table")])
    width = max((len(row) for row in rows), default=0)
    rows = [row + [""] * (width - len(row)) for row in rows]
    if complete and not any(any(row) for row in rows):
        raise DocumentError("page_builder.error.required")
    return {"header": raw.get("header") is not False, "caption": _text(raw.get("caption"), limit=300), "rows": rows}


def _columns(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    columns = raw.get("columns", [])
    if not isinstance(columns, list) or len(columns) not in ((2, 3) if version == 1 else (2, 3, 4)):
        raise DocumentError("page_builder.error.columns")
    return {"format": _format(raw, version), "columns": [_text(column, limit=5000) for column in columns]}


def _code(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    language = _text(raw.get("language"), limit=30)
    if not _LANGUAGE.fullmatch(language):
        raise DocumentError("page_builder.error.language")
    return {"language": language, "code": _text(raw.get("code"), required=complete)}


def _pages(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    source = _choice(raw.get("source"), ("selected", "category", "recent"), "page_builder.error.option")
    slugs = [_text(slug, limit=180) for slug in _list(raw.get("slugs"), MAX_PAGE_LIST, "page_builder.error.pages")]
    if not all(_REF.fullmatch(slug) for slug in slugs if slug):
        raise DocumentError("page_builder.error.pages")
    category = raw.get("category_id")
    if category is not None and (isinstance(category, bool) or not isinstance(category, int) or category < 1):
        raise DocumentError("page_builder.error.pages")
    slugs = list(dict.fromkeys(slug for slug in slugs if slug))
    if complete and ((source == "selected" and not slugs) or (source == "category" and category is None)):
        raise DocumentError("page_builder.error.required")
    return {"source": source, "slugs": slugs, "category_id": category,
            "limit": _count(raw.get("limit"), 1, MAX_PAGE_LIST, 6, "page_builder.error.option"),
            "display": _choice(raw.get("display"), ("cards", "list"), "page_builder.error.option"),
            "excerpt": raw.get("excerpt") is not False}


def _embed(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    ref = _text(raw.get("ref"), limit=200, required=complete)
    if ref and not _REF.fullmatch(ref):
        raise DocumentError("page_builder.error.embed")
    return {"kind": _choice(raw.get("kind"), EMBED_KINDS, "page_builder.error.embed"), "ref": ref}


def _nothing(raw: dict[str, Any], complete: bool, version: int) -> dict[str, Any]:
    return {}


_BLOCKS: dict[str, Callable[[dict[str, Any], bool, int], dict[str, Any]]] = {
    "heading": _heading, "text": _text_block, "image": _image_block, "gallery": _gallery, "youtube": _youtube,
    "button": _button, "hero": _hero, "cards": _cards, "callout": _callout, "quote": _quote, "list": _list_block,
    "faq": _faq, "table": _table, "columns": _columns, "code": _code, "pages": _pages, "embed": _embed,
    "divider": _nothing, "spacer": _nothing,
}


def _layout(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DocumentError("page_builder.error.layout")
    layout = {}
    for key, choices in LAYOUT.items():
        value = _choice(raw.get(key), choices, "page_builder.error.layout")
        if value != choices[0]:
            layout[key] = value
    return layout


def _block(raw: Any, complete: bool, version: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DocumentError("page_builder.error.block")
    kind = raw.get("type")
    if kind not in (V1_BLOCK_TYPES if version == 1 else BLOCK_TYPES):
        raise DocumentError("page_builder.error.block_type")
    block: dict[str, Any] = {"type": kind, **_BLOCKS[kind](raw, complete, version)}
    layout = _layout(raw.get("layout")) if version > 1 else {}
    if layout:
        block["layout"] = layout
    return block


def validate(payload: Any, *, complete: bool = True) -> dict[str, Any]:
    """Normalised version-2 copy of *payload* (version 1 is upgraded).

    *complete* False accepts unfinished blocks (drafts, previews).
    """
    if not isinstance(payload, dict):
        raise DocumentError("page_builder.error.version")
    version = payload.get("version")
    if isinstance(version, bool) or version not in (1, VERSION):
        raise DocumentError("page_builder.error.version")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise DocumentError("page_builder.error.block")
    if len(blocks) > MAX_BLOCKS:
        raise DocumentError("page_builder.error.too_many_blocks")
    document = {"version": VERSION, "blocks": [_block(raw, complete, version) for raw in blocks]}
    if len(dump(document).encode("utf-8")) > MAX_BYTES:
        raise DocumentError("page_builder.error.too_large")
    return document


def dump(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=True, separators=(",", ":"))


def load(raw: str | None) -> dict[str, Any]:
    """Decode and validate a stored document (drafts may be unfinished)."""
    if not raw:
        return empty()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise DocumentError("page_builder.error.version") from None
    return validate(payload, complete=False)


def from_markdown(content: str) -> dict[str, Any]:
    """A document holding *content* as Markdown text blocks (split at paragraphs when long)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fenced = False
    for line in (content or "").replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        current.append(line)
        size += len(line) + 1
        if not fenced and not line.strip() and size > MAX_TEXT // 2:
            chunks.append("\n".join(current))
            current, size = [], 0
    chunks.append("\n".join(current))
    blocks = []
    for chunk in chunks:
        chunk = chunk.strip()
        while chunk:
            blocks.append({"type": "text", "format": "markdown", "text": chunk[:MAX_TEXT].strip()})
            chunk = chunk[MAX_TEXT:].strip()
    return {"version": VERSION, "blocks": blocks[:MAX_BLOCKS]}


# ── Output ────────────────────────────────────────────────────────────────────


def _lines(text: str) -> Markup:
    return Markup("<br>").join(escape(line) for line in text.split("\n"))


def _rich(text: str, text_format: str) -> Markup:
    """A text field as HTML: sanitised Markdown or escaped plain text."""
    if text_format == "markdown":
        return Markup(markdown.render(text))
    return _lines(text)


def _anchor(text: str, used: set[str]) -> str:
    base = _ANCHOR_SPACES.sub("-", _ANCHOR_STRIP.sub("", text.lower())).strip("-")[:80] or "section"
    anchor, n = base, 2
    while anchor in used:
        anchor, n = f"{base}-{n}", n + 1
    used.add(anchor)
    return anchor


def _excerpts(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Excerpts of the listed pages by id, worked out once per request and page revision.

    Only the start of each page is read: :func:`markdown.excerpt` renders no more.
    """
    cache: dict[tuple[int, Any], str] = g.setdefault("_builder_excerpts", {}) if has_request_context() else {}
    keys = {row["id"]: (row["id"], row.get("revision")) for row in rows}
    missing = [page_id for page_id, key in keys.items() if key not in cache]
    if missing:
        marks = ",".join("?" for _ in missing)
        # One character more than excerpt() renders tells it the page goes on.
        for found in db.all(f"SELECT id, substr(content, 1, ?) AS head FROM pages WHERE id IN ({marks})",
                            [markdown.EXCERPT_SOURCE_CHARS + 1, *missing]):
            cache[keys[found["id"]]] = markdown.excerpt(found["head"] or "", 160)
    return {page_id: cache.get(key, "") for page_id, key in keys.items()}


def _page_entries(block: dict[str, Any], current_page_id: int | None, room: int) -> list[dict[str, Any]]:
    """Pages a ``pages`` block lists for the current reader (their own permissions apply).

    *room* is how many more entries the document may show (:data:`MAX_LISTED_PAGES` in all).
    """
    from ..pages import service as pages

    if room <= 0:
        return []
    if block["source"] == "selected":
        rows = []
        for slug in block["slugs"]:
            page = pages.get_by_slug(slug, with_content=False)
            if page is not None and page["id"] != current_page_id and pages.can_view(page):
                rows.append(page)
                if len(rows) == room:
                    break
    else:
        category = block["category_id"] if block["source"] == "category" else "any"
        if category is None:
            return []
        limit = min(block["limit"], room)
        rows = [row for row in pages.list_visible(category_id=category, order="recent", limit=limit + 1)
                if row["id"] != current_page_id][:limit]
    excerpts = _excerpts(rows) if rows and block["excerpt"] else {}
    return [{"title": row["title"], "url": url_for("pages.view", slug=row["slug"]),
             "excerpt": excerpts.get(row["id"], "")}
            for row in rows]


def layout_classes(block: dict[str, Any]) -> str:
    layout = block.get("layout") or {}
    classes = [f"builder-section builder-section--{block['type']}"]
    for key, prefix in (("align", "is-align-"), ("background", "has-bg-"), ("spacing", "is-space-")):
        if key in layout:
            classes.append(prefix + layout[key])
    return " ".join(classes)


def render(document: dict[str, Any], *, page_id: int | None = None) -> Markup:
    """Sanitised HTML of a validated document for the current reader.

    *page_id* is the page being shown, left out of page lists.
    """
    items = []
    anchors: set[str] = set()
    room = MAX_LISTED_PAGES
    for block in document["blocks"]:
        if is_blank(block):
            continue
        kind = block["type"]
        extra: dict[str, Any] = {}
        if kind == "heading" and block["text"]:
            extra["anchor"] = _anchor(block["text"], anchors)
        elif kind == "hero" and block["title"]:
            extra["anchor"] = _anchor(block["title"], anchors)
        elif kind == "text" and block["text"]:
            extra["html"] = _rich(block["text"], block["format"])
        elif kind == "columns":
            extra["html"] = [_rich(column, block["format"]) for column in block["columns"]]
        elif kind == "faq":
            extra["html"] = [Markup(markdown.render(item["answer"])) for item in block["items"]]
        elif kind == "youtube" and block["url"]:
            player = markdown.video_iframe(block["url"])
            if player:
                extra["html"] = Markup(player)
        elif kind == "code" and block["code"]:
            extra["html"] = Markup(markdown.highlight_code(block["code"], block["language"]))
        elif kind == "pages":
            extra["pages"] = _page_entries(block, page_id, room)
            room -= len(extra["pages"])
        elif kind == "embed" and block["ref"]:
            extra["enabled"] = registry.is_enabled(block["kind"])
            extra["label"] = t(f"page_builder.embed_label.{block['kind']}", ref=block["ref"])
        items.append({"block": block, "classes": layout_classes(block), **extra})
    return Markup(render_template("page_builder/render.html", items=items, lines=_lines))


def audit(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Accessibility and structure hints: ``[{"block": index, "key": translation key}]``."""
    hints: list[dict[str, Any]] = []
    previous_level = 1  # the page title is the page's <h1>

    def hint(index: int, key: str) -> None:
        hints.append({"block": index, "key": f"page_builder.check.{key}"})

    for index, block in enumerate(document["blocks"]):
        kind = block["type"]
        level = block["level"] if kind == "heading" and block["text"] else 2 if kind == "hero" else None
        if level is not None:
            if level == 1:
                hint(index, "h1")
            elif level > previous_level + 1:
                hint(index, "heading_skip")
            previous_level = level
        images: list[tuple[str, str]] = []
        if kind == "image" and not block["decorative"]:
            images = [(block["url"], block["alt"])]
        elif kind == "gallery":
            images = [(image["url"], image["alt"]) for image in block["images"]]
        elif kind == "hero":
            images = [(block["image"], block["image_alt"])]
        elif kind == "cards":
            images = [(card["image"], card["image_alt"]) for card in block["items"]]
        if any(url and not alt for url, alt in images):
            hint(index, "alt")
        if kind == "table" and block["rows"] and not block["header"]:
            hint(index, "table_header")
        if kind == "embed" and block["ref"] and not registry.is_enabled(block["kind"]):
            hint(index, "embed_off")
        if is_blank(block):
            hint(index, "empty")
    return hints


def is_blank(block: dict[str, Any]) -> bool:
    """Whether *block* shows nothing on the published page."""
    kind = block["type"]
    if kind in ("divider", "spacer", "pages"):
        return False
    if kind == "columns":
        return not any(block["columns"])
    if kind == "table":
        return not any(any(row) for row in block["rows"])
    if kind == "callout":
        return not (block["title"] or block["text"])
    field = {"heading": "text", "text": "text", "image": "url", "gallery": "images", "youtube": "url",
             "button": "url", "hero": "title", "cards": "items", "quote": "text", "list": "items", "faq": "items",
             "code": "code", "embed": "ref"}[kind]
    return not block[field]


# ── Markdown twin ─────────────────────────────────────────────────────────────


def _cell(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|") or " "


def _fence(code: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


def to_markdown(document: dict[str, Any]) -> str:
    """Plain Markdown equivalent stored in ``pages.content``.

    The output for version-1 block types never changes: pages whose content
    matches it are recognised as builder pages (:func:`is_current`).
    """
    parts: list[str] = []
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "heading" and block["text"]:
            parts.append("#" * block["level"] + " " + block["text"].replace("\n", " "))
        elif kind == "text" and block["text"]:
            parts.append(block["text"])
        elif kind == "image" and block["url"]:
            parts.append(f"![{block['alt']}]({block['url']})" + (f"\n*{block['caption']}*" if block["caption"] else ""))
        elif kind == "gallery" and block["images"]:
            parts.extend(f"![{image['alt']}]({image['url']})" + (f"\n*{image['caption']}*" if image["caption"] else "")
                         for image in block["images"] if image["url"])
        elif kind == "youtube" and block["url"]:
            parts.append(block["url"] + (f"\n\n*{block['caption']}*" if block["caption"] else ""))
        elif kind == "button" and block["url"]:
            parts.append(f"[{block['label'] or block['url']}]({block['url']})")
        elif kind == "hero" and block["title"]:
            parts.append("## " + block["title"].replace("\n", " "))
            if block["text"]:
                parts.append(block["text"])
            if block["button_url"]:
                parts.append(f"[{block['button_label'] or block['button_url']}]({block['button_url']})")
        elif kind == "cards":
            for card in block["items"]:
                title = card["title"].replace("\n", " ")
                parts.append("### " + (f"[{title}]({card['url']})" if card["url"] and title else title)
                             + (f"\n\n{card['text']}" if card["text"] else ""))
        elif kind == "callout" and (block["title"] or block["text"]):
            lines = ([f"**{block['title']}**"] if block["title"] else []) + block["text"].split("\n")
            parts.append("\n".join("> " + line for line in lines))
        elif kind == "quote" and block["text"]:
            lines = block["text"].split("\n") + ([f"— {block['cite']}"] if block["cite"] else [])
            parts.append("\n".join("> " + line for line in lines))
        elif kind == "list" and block["items"]:
            parts.append("\n".join((f"{n}. " if block["ordered"] else "- ") + item
                                   for n, item in enumerate(block["items"], 1)))
        elif kind == "faq":
            parts.extend(f"**{item['question']}**" + (f"\n\n{item['answer']}" if item["answer"] else "")
                         for item in block["items"] if item["question"])
        elif kind == "table" and any(any(row) for row in block["rows"]):
            rows = block["rows"] if block["header"] else [[""] * len(block["rows"][0]), *block["rows"]]
            lines = ["| " + " | ".join(_cell(cell) for cell in rows[0]) + " |",
                     "|" + "|".join(" --- " for _ in rows[0]) + "|"]
            lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows[1:]]
            parts.append(("*" + block["caption"] + "*\n\n" if block["caption"] else "") + "\n".join(lines))
        elif kind == "columns":
            parts.extend(column for column in block["columns"] if column)
        elif kind == "code" and block["code"]:
            fence = _fence(block["code"])
            parts.append(f"{fence}{block['language']}\n{block['code']}\n{fence}")
        elif kind == "pages" and block["source"] == "selected" and block["slugs"]:
            parts.append("\n".join(f"- [{slug}](/page/{slug})" for slug in block["slugs"]))
        elif kind == "embed" and block["ref"]:
            parts.append(f'[[canvas slug="{block["ref"]}"]]' if block["kind"] == "canvas"
                         else f'[[kanban board="{block["ref"]}"]]')
        elif kind == "divider":
            parts.append("---")
    return "\n\n".join(parts)


def is_current(page: dict[str, Any], document: dict[str, Any]) -> bool:
    """Whether the page body still comes from its builder document.

    A page whose Markdown was changed elsewhere (the plain editor, the API,
    an import) shows that Markdown instead.
    """
    content = page.get("content") or ""
    return content == to_markdown(document) or content.startswith(LEGACY_MARKER)
