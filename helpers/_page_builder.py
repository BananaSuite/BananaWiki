"""Validation and safe Markdown compilation for the core visual page builder."""

import json
import re
from html import escape
from urllib.parse import urlparse


MAX_BUILDER_BYTES = 256 * 1024
MAX_BUILDER_BLOCKS = 120
MAX_TEXT_LENGTH = 20_000
BUILDER_VERSION = 1

_UPLOAD_URL_RE = re.compile(
    r"^/static/uploads/[a-f0-9]{32}\.(?:png|jpe?g|gif|webp|bmp)$",
    re.IGNORECASE,
)
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class BuilderValidationError(ValueError):
    """Raised when untrusted builder data does not match the allowlisted schema."""


def _text(value, *, limit=MAX_TEXT_LENGTH, required=False):
    """Validate and trim a bounded text field."""
    if not isinstance(value, str):
        raise BuilderValidationError("Text fields must contain text.")
    value = value.strip()
    if required and not value:
        raise BuilderValidationError("A required text field is empty.")
    if len(value) > limit:
        raise BuilderValidationError("A builder text field is too long.")
    return value


def _safe_link(value, *, allow_empty=False):
    """Accept absolute HTTP(S) links and local paths."""
    value = _text(value, limit=2048)
    if not value and allow_empty:
        return ""
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    if not parsed.scheme and not parsed.netloc and value.startswith("/") and not value.startswith("//"):
        return value
    raise BuilderValidationError("Only safe HTTP(S) or internal links are allowed.")


def _youtube_url(value):
    """Canonicalize a supported YouTube URL after validating its video ID."""
    value = _safe_link(value)
    parsed = urlparse(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    video_id = ""
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if parsed.path == "/watch":
            from urllib.parse import parse_qs
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            video_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    elif host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    if not _YOUTUBE_ID_RE.fullmatch(video_id):
        raise BuilderValidationError("Enter a valid YouTube video URL.")
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_builder_payload(payload, *, allow_incomplete=False):
    """Return normalized builder data after strict schema and size validation."""
    if not isinstance(payload, dict) or payload.get("version") != BUILDER_VERSION:
        raise BuilderValidationError("Unsupported builder document version.")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise BuilderValidationError("Builder blocks must be a list.")
    if len(blocks) > MAX_BUILDER_BLOCKS:
        raise BuilderValidationError("This page has too many builder blocks.")

    normalized = []
    for raw in blocks:
        if not isinstance(raw, dict):
            raise BuilderValidationError("Every builder block must be an object.")
        kind = raw.get("type")
        block = {"type": kind}
        if kind == "heading":
            level = raw.get("level", 2)
            if level not in (1, 2, 3):
                raise BuilderValidationError("Heading level must be 1, 2, or 3.")
            block.update(level=level, text=_text(raw.get("text", ""), limit=300, required=True))
        elif kind == "text":
            block["text"] = _text(raw.get("text", ""))
        elif kind == "image":
            url = _text(raw.get("url", ""), limit=2048, required=not allow_incomplete)
            if url and not _UPLOAD_URL_RE.fullmatch(url):
                raise BuilderValidationError("Builder images must be uploaded to this wiki.")
            block.update(
                url=url,
                alt=_text(raw.get("alt", ""), limit=300),
                caption=_text(raw.get("caption", ""), limit=500),
            )
        elif kind == "youtube":
            raw_url = _text(raw.get("url", ""), limit=2048, required=not allow_incomplete)
            block.update(
                url=_youtube_url(raw_url) if raw_url else "",
                caption=_text(raw.get("caption", ""), limit=500),
            )
        elif kind == "button":
            style = raw.get("style", "primary")
            if style not in ("primary", "outline"):
                style = "primary"
            block.update(
                label=_text(raw.get("label", ""), limit=100, required=True),
                url=_safe_link(raw.get("url", "")),
                style=style,
            )
        elif kind == "callout":
            tone = raw.get("tone", "info")
            if tone not in ("info", "success", "warning"):
                tone = "info"
            block.update(
                title=_text(raw.get("title", ""), limit=200),
                text=_text(raw.get("text", ""), limit=3000),
                tone=tone,
            )
        elif kind == "list":
            items = raw.get("items", [])
            if not isinstance(items, list) or len(items) > 100:
                raise BuilderValidationError("A list must contain at most 100 items.")
            block.update(
                ordered=bool(raw.get("ordered")),
                items=[_text(item, limit=1000, required=True) for item in items],
            )
        elif kind == "columns":
            columns = raw.get("columns", [])
            if not isinstance(columns, list) or len(columns) not in (2, 3):
                raise BuilderValidationError("Columns must contain two or three text areas.")
            block["columns"] = [_text(column, limit=5000) for column in columns]
        elif kind in ("divider", "spacer"):
            pass
        else:
            raise BuilderValidationError("Unknown builder block type.")
        normalized.append(block)

    result = {"version": BUILDER_VERSION, "blocks": normalized}
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_BUILDER_BYTES:
        raise BuilderValidationError("This builder page is too large.")
    return result


def dump_builder_payload(payload, *, allow_incomplete=False):
    """Serialize a validated builder document to compact JSON."""
    normalized = validate_builder_payload(payload, allow_incomplete=allow_incomplete)
    return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))


def load_builder_payload(value):
    """Decode and validate a saved builder document."""
    if not value:
        return {"version": BUILDER_VERSION, "blocks": []}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise BuilderValidationError("The saved builder document is invalid.") from exc
    return validate_builder_payload(payload, allow_incomplete=True)


def _html_text(value):
    """Escape text before including it in generated page markup."""
    return escape(value).replace("\n", "<br>")


def compile_builder_markdown(payload, *, allow_incomplete=False):
    """Compile validated blocks into sanitizer-safe Markdown and limited HTML."""
    document = validate_builder_payload(payload, allow_incomplete=allow_incomplete)
    output = ['<section class="builder-page">']
    for block in document["blocks"]:
        kind = block["type"]
        if kind == "heading":
            level = block["level"]
            output.append(f"<h{level} class=\"builder-heading\">{escape(block['text'])}</h{level}>")
        elif kind == "text":
            output.append(f'<div class="builder-text">{_html_text(block["text"])}</div>')
        elif kind == "image":
            if not block["url"]:
                continue
            caption = (
                f'<figcaption>{escape(block["caption"])}</figcaption>'
                if block["caption"] else ""
            )
            output.append(
                '<figure class="builder-image">'
                f'<img src="{escape(block["url"], quote=True)}" '
                f'alt="{escape(block["alt"], quote=True)}" loading="lazy">{caption}</figure>'
            )
        elif kind == "youtube":
            if not block["url"]:
                continue
            output.extend(["", block["url"], ""])
            if block["caption"]:
                output.append(f'<p class="builder-caption">{escape(block["caption"])}</p>')
        elif kind == "button":
            output.append(
                '<p class="builder-button-row">'
                f'<a class="builder-button builder-button-{block["style"]}" '
                f'href="{escape(block["url"], quote=True)}" rel="noopener noreferrer">'
                f'{escape(block["label"])}</a></p>'
            )
        elif kind == "callout":
            title = f'<strong>{escape(block["title"])}</strong>' if block["title"] else ""
            output.append(
                f'<aside class="builder-callout builder-callout-{block["tone"]}">'
                f'{title}<div>{_html_text(block["text"])}</div></aside>'
            )
        elif kind == "list":
            tag = "ol" if block["ordered"] else "ul"
            items = "".join(f"<li>{escape(item)}</li>" for item in block["items"])
            output.append(f'<{tag} class="builder-list">{items}</{tag}>')
        elif kind == "columns":
            columns = "".join(
                f'<div class="builder-column">{_html_text(column)}</div>'
                for column in block["columns"]
            )
            output.append(f'<div class="builder-columns builder-columns-{len(block["columns"])}">{columns}</div>')
        elif kind == "divider":
            output.append('<hr class="builder-divider">')
        elif kind == "spacer":
            output.append('<div class="builder-spacer"><br></div>')
    output.append("</section>")
    return "\n\n".join(output)
