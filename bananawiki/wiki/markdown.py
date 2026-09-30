"""Markdown to safe HTML.

The dialect is the one BananaWiki has always stored: Python-Markdown with
``tables``, ``fenced_code``, ``codehilite``, ``toc`` and ``nl2br``, plus

* forgiving list handling (2-space nesting, lists straight after a paragraph),
* extra blank lines preserved as vertical space,
* ``[[video url="…" …]]``, ``[[canvas slug="…"]]`` and ``[[kanban board="…"]]``
  shortcodes on a line of their own,
* bare YouTube/Vimeo links on their own line embedded as players,
* ``@username`` mentions linked to profiles.

Output is sanitised with nh3 (the ammonia HTML sanitiser) against a strict
allow-list: only known classes, ids only on headings, ``style`` limited to a
few spacing properties, links forced to ``rel="noopener noreferrer"``. Embeds
are generated *after* sanitising from validated parameters only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html import escape
from urllib.parse import quote

import markdown as _markdown
import nh3
from markdown.extensions import Extension
from markdown.inlinepatterns import SimpleTagInlineProcessor
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.token import STANDARD_TYPES
from pygments.util import ClassNotFound

MAX_SOURCE_CHARS = 1_000_000

_TAGS = {
    "a", "abbr", "acronym", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd",
    "li", "mark", "ol", "p", "pre", "s", "section", "aside", "small", "span", "strong", "sub", "summary",
    "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "style"},
    "abbr": {"title"},
    "acronym": {"title"},
    "figure": {"style"},
    "div": {"style"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan"},
    "ol": {"start"},
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"}, "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
}
_TAG_ATTRIBUTE_VALUES = {
    "a": {"target": {"_blank"}},
    "img": {"loading": {"lazy", "eager"}},
}
_STYLE_PROPERTIES = {
    "margin-top", "margin-bottom", "margin-left", "margin-right", "padding",
    "width", "max-width", "height",
    "--media-margin-top", "--media-margin-bottom", "--media-padding", "--media-max-width",
    "--media-aspect-ratio",
}
_PYGMENTS_CLASSES = {cls for cls in STANDARD_TYPES.values() if cls}
_CONTENT_CLASSES = {
    # Classes the editor toolbar inserts for media layout; styled in content.css.
    "media-left", "media-right", "media-center", "media-full", "media-inline",
    "media-image-container", "media-caption", "table-wrapper", "toc",
}
_ALLOWED_CLASSES = {
    "div": {"codehilite", "toc", *_CONTENT_CLASSES},
    "span": _PYGMENTS_CLASSES | {"hll", "linenos", "normal", "special"},
    "pre": {"codehilite", "code-block"},
    "code": set(),
    "img": _CONTENT_CLASSES,
    "figure": _CONTENT_CLASSES,
    "figcaption": _CONTENT_CLASSES,
    "table": {"table-wrapper"},
    "p": _CONTENT_CLASSES,
}
_URL_SCHEMES = {"http", "https", "mailto", "ftp"}
_HEADING_ID = re.compile(r"^[A-Za-z0-9_\-.:]{1,120}$")

_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^(\d+[.)]|[-*+])\s+\S")
_INDENTED_LIST_ITEM = re.compile(r"^([ \t]*)(\d+[.)]|[-*+])\s")
_SHORTCODE_LINE = re.compile(r"^[ \t]*\[\[(?:video|canvas|kanban)\s+[^\]]*?\]\][ \t]*$", re.IGNORECASE)
_BLANK_RUN = re.compile(r"(\n[ \t]*){3,}")


def _split_fences(text: str) -> list[tuple[bool, list[str]]]:
    """Split source lines into runs of (inside_fence, lines)."""
    runs: list[tuple[bool, list[str]]] = []
    inside = False
    current: list[str] = []
    for line in text.split("\n"):
        if _FENCE.match(line):
            if inside:
                current.append(line)
                runs.append((True, current))
                current = []
                inside = False
                continue
            if current:
                runs.append((False, current))
            current = [line]
            inside = True
            continue
        current.append(line)
    if current:
        runs.append((inside, current))
    return runs


def _map_prose(text: str, transform: Callable[[list[str]], list[str]]) -> str:
    out: list[str] = []
    for inside, lines in _split_fences(text):
        out.extend(lines if inside else transform(lines))
    return "\n".join(out)


def _blank_line_before_lists(lines: list[str]) -> list[str]:
    out: list[str] = []
    for index, line in enumerate(lines):
        if index and _LIST_ITEM.match(line):
            previous = lines[index - 1]
            if previous.strip() and not _LIST_ITEM.match(previous) and not previous.startswith((" ", "\t")):
                out.append("")
        out.append(line)
    return out


def _normalise_list_block(block: list[str]) -> list[str]:
    indents = []
    for line in block:
        if line.strip():
            expanded = line.expandtabs(4)
            spaces = len(expanded) - len(expanded.lstrip(" "))
            if spaces:
                indents.append(spaces)
    if not indents or min(indents) >= 4:
        return block
    factor = max(1, 4 // min(indents))
    result = []
    for line in block:
        if not line.strip():
            result.append(line)
            continue
        expanded = line.expandtabs(4)
        spaces = len(expanded) - len(expanded.lstrip(" "))
        result.append(" " * (spaces * factor) + expanded[spaces:])
    return result


def _normalise_list_indent(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _LIST_ITEM.match(line.lstrip()) and line[:1] not in (" ", "\t"):
            j = i
            saw_blank = False
            while j < len(lines):
                current = lines[j]
                if not current.strip():
                    if saw_blank:
                        break
                    saw_blank = True
                    j += 1
                    continue
                if _INDENTED_LIST_ITEM.match(current) or (current[:1] in (" ", "\t") and not saw_blank):
                    saw_blank = False
                    j += 1
                    continue
                break
            out.extend(_normalise_list_block(lines[i:j]))
            i = j
            continue
        out.append(line)
        i += 1
    return out


def _isolate_shortcodes(lines: list[str]) -> list[str]:
    out: list[str] = []
    for index, line in enumerate(lines):
        if _SHORTCODE_LINE.match(line):
            if out and out[-1].strip():
                out.append("")
            out.append(line.strip())
            if index + 1 < len(lines) and lines[index + 1].strip():
                out.append("")
            continue
        out.append(line)
    return out


def _preserve_blank_runs(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    text = _BLANK_RUN.sub(lambda m: "\n\n" + "<br>" * (m.group(0).count("\n") - 2) + "\n\n", text)
    return text.split("\n")


def _preprocess(text: str, fix_lists: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if fix_lists:
        text = _map_prose(text, _blank_line_before_lists)
    text = _map_prose(text, _normalise_list_indent)
    text = _map_prose(text, _isolate_shortcodes)
    return _map_prose(text, _preserve_blank_runs)


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "id":
        return value if _HEADING_ID.match(value) else None
    if tag == "img" and attribute in {"width", "height"}:
        return value if re.fullmatch(r"\d{1,4}%?", value) else None
    return value


def sanitize(html: str) -> str:
    """Sanitise HTML produced from user Markdown."""
    return nh3.clean(
        html,
        tags=_TAGS,
        clean_content_tags={"script", "style"},
        attributes=_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        tag_attribute_values=_TAG_ATTRIBUTE_VALUES,
        allowed_classes=_ALLOWED_CLASSES,
        filter_style_properties=_STYLE_PROPERTIES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


# ── Post-sanitisation embeds ──────────────────────────────────────────────────

_VIDEO_SHORTCODE = re.compile(r"<p>\s*\[\[video\s+(?P<attrs>[^\]]*?)\]\]\s*</p>", re.IGNORECASE)
_EMBED_SHORTCODE = re.compile(r"<p>\s*\[\[(?P<kind>canvas|kanban)\s+(?P<attrs>[^\]]*?)\]\]\s*</p>", re.IGNORECASE)
_SHORTCODE_ATTR = re.compile(r'([a-z]+)\s*=\s*(?:"|&quot;)([^"&]*)(?:"|&quot;)', re.IGNORECASE)
_BARE_VIDEO = re.compile(
    r'<p>\s*(?:<a href="(?P<href>https?://[^"]+)"[^>]*>(?P=href)</a>|(?P<bare>https?://[^\s<>"]+))\s*</p>',
    re.IGNORECASE,
)
_RATIOS = {"16:9": "ratio-16x9", "4:3": "ratio-4x3", "1:1": "ratio-1x1"}
_DIMENSION = re.compile(r"^\d{2,4}$")


def video_embed_src(url: str | None) -> str | None:
    """Canonical player URL for YouTube or Vimeo links, else None."""
    if not url:
        return None
    match = re.search(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if match and re.match(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/", url, re.IGNORECASE):
        return f"https://www.youtube-nocookie.com/embed/{match.group(1)}"
    match = re.match(r"https?://(?:www\.|player\.)?vimeo\.com/(?:video/)?(\d+)", url, re.IGNORECASE)
    if match:
        return f"https://player.vimeo.com/video/{match.group(1)}"
    return None


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() == "true"


def video_iframe(source_url: str, **options: str | None) -> str | None:
    """Build the player markup for a supported video URL (options validated here)."""
    src = video_embed_src(source_url)
    if not src:
        return None
    width = options.get("width") or ""
    width = width if _DIMENSION.match(width) else ""
    align = (options.get("align") or "center").lower()
    align = align if align in {"none", "left", "right", "center"} else "center"
    ratio = options.get("ratio") or "16:9"
    ratio = ratio if ratio in _RATIOS else "16:9"
    margin = options.get("margin") or ""
    margin = margin if margin.isdigit() and int(margin) <= 200 else ""
    params = []
    if _flag(options.get("autoplay"), False):
        params.append("autoplay=1")
    if _flag(options.get("loop"), False):
        params.append("loop=1")
    if not _flag(options.get("controls"), True) and "youtube" in src:
        params.append("controls=0")
    if params:
        src += "?" + "&".join(params)
    data = [
        f'data-source-url="{escape(source_url, quote=True)}"',
        f'data-align="{align}"',
        f'data-ratio="{ratio}"',
    ]
    if width:
        data.append(f'data-width="{width}"')
    if margin:
        data.append(f'data-margin="{margin}"')
    return (
        f'<div class="bw-video bw-video-{align} {_RATIOS[ratio]}" {" ".join(data)}>'
        f'<iframe src="{escape(src, quote=True)}" title="Video" loading="lazy" '
        'allow="fullscreen; picture-in-picture" allowfullscreen '
        'referrerpolicy="strict-origin-when-cross-origin"></iframe></div>'
    )


def _shortcode_attrs(raw: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _SHORTCODE_ATTR.findall(raw)}


def _embed_videos(html: str, bare_links: bool) -> str:
    def shortcode(match: re.Match[str]) -> str:
        attrs = _shortcode_attrs(match.group("attrs"))
        url = attrs.pop("url", "").replace("&amp;", "&")
        return video_iframe(url, **attrs) or match.group(0)

    html = _VIDEO_SHORTCODE.sub(shortcode, html)
    if bare_links:
        def bare(match: re.Match[str]) -> str:
            url = (match.group("href") or match.group("bare") or "").replace("&amp;", "&")
            return video_iframe(url) or match.group(0)

        html = _BARE_VIDEO.sub(bare, html)
    return html


def _embed_boards(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = match.group("kind").lower()
        attrs = _shortcode_attrs(match.group("attrs"))
        ref = attrs.get("slug" if kind == "canvas" else "board", "")
        if not ref or len(ref) > 200:
            return match.group(0)
        width = attrs.get("width", "")
        height = attrs.get("height", "")
        dims = ""
        if _DIMENSION.match(width):
            dims += f' data-width="{width}"'
        if _DIMENSION.match(height):
            dims += f' data-height="{height}"'
        return (
            f'<div class="bw-embed bw-embed-{kind}" data-embed-type="{kind}" '
            f'data-embed-ref="{escape(ref, quote=True)}"{dims}></div>'
        )

    return _EMBED_SHORTCODE.sub(replace, html)


_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_MENTION = re.compile(r"(?<![\w@/])@([A-Za-z0-9_-]{3,50})\b")
_SKIP_TAGS = {"a", "code", "pre"}


def _link_mentions(html: str, profile_url: Callable[[str], str]) -> str:
    parts = _TAG_SPLIT.split(html)
    depth = 0
    for index, part in enumerate(parts):
        if not part:
            continue
        if part.startswith("<"):
            match = re.match(r"<(/?)([a-z0-9]+)", part, re.IGNORECASE)
            if match and match.group(2).lower() in _SKIP_TAGS and not part.endswith("/>"):
                depth += -1 if match.group(1) else 1
                depth = max(depth, 0)
            continue
        if depth == 0 and "@" in part:
            parts[index] = _MENTION.sub(
                lambda m: f'<a class="mention" href="{escape(profile_url(m.group(1)), quote=True)}">@{m.group(1)}</a>',
                part,
            )
    return "".join(parts)


def _default_profile_url(username: str) -> str:
    return "/users/" + quote(username)


class _Strikethrough(Extension):
    """``~~text~~`` becomes ``<del>text</del>`` (the editor toolbar inserts it)."""

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API name
        md.inlinePatterns.register(SimpleTagInlineProcessor(r"(~~)(.+?)~~", "del"), "bw_del", 175)


_TASK_ITEM = re.compile(r"<li>(\s*<p>)?\s*\[( |x|X)\]\s+")


def _task_lists(html: str) -> str:
    """``- [ ] item`` / ``- [x] item`` become read-only check marks."""

    def replace(match: re.Match[str]) -> str:
        done = match.group(2) in "xX"
        mark = "☑" if done else "☐"
        state = "done" if done else "open"
        return (f'<li class="task-item task-{state}">{match.group(1) or ""}'
                f'<span class="task-box" aria-hidden="true">{mark}</span> ')

    return _TASK_ITEM.sub(replace, html)


def render(
    text: str | None,
    *,
    embed_videos: bool = True,
    fix_lists: bool = True,
    mentions: bool = True,
    profile_url: Callable[[str], str] = _default_profile_url,
) -> str:
    """Render stored Markdown to sanitised HTML."""
    if not text:
        return ""
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]
    html = _markdown.markdown(
        _preprocess(text, fix_lists),
        extensions=["tables", "fenced_code", "codehilite", "toc", "nl2br", _Strikethrough()],
        extension_configs={"codehilite": {"css_class": "codehilite", "guess_lang": False}},
        output_format="html",
    )
    html = _task_lists(sanitize(html))
    html = _embed_videos(html, bare_links=embed_videos)
    html = _embed_boards(html)
    if mentions:
        html = _link_mentions(html, profile_url)
    return html


def highlight_code(code: str | None, language: str | None = None) -> str:
    """Syntax-highlight a code snippet (used by code custom pages and the editor)."""
    code = "" if code is None else str(code)[:MAX_SOURCE_CHARS]
    language = (language or "").strip()
    try:
        if language:
            lexer = get_lexer_by_name(language, stripall=False)
        elif code.strip():
            lexer = guess_lexer(code)
        else:
            lexer = TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return sanitize(highlight(code, lexer, HtmlFormatter(cssclass="codehilite")))


_STRIP_TAGS = re.compile(r"<[^>]+>")


def to_plain_text(text: str | None) -> str:
    """Rendered text without markup (search snippets, TTS, previews)."""
    from html import unescape

    html = render(text, embed_videos=False, mentions=False)
    return re.sub(r"\s+", " ", unescape(_STRIP_TAGS.sub(" ", html))).strip()


def excerpt(text: str | None, length: int = 200) -> str:
    plain = to_plain_text(text)
    return plain if len(plain) <= length else plain[: length - 1].rstrip() + "…"
