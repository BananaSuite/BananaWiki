"""Markdown rendering and video embed helpers."""

import re
from html import escape

import markdown
import bleach
from bleach.css_sanitizer import CSSSanitizer
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from ._constants import ALLOWED_TAGS, ALLOWED_ATTRS

# Allow only safe CSS properties for media spacing/sizing via style attributes.
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=[
    "margin-top", "margin-bottom", "margin-left", "margin-right", "padding",
    "--media-margin-top", "--media-margin-bottom",
    "--media-padding", "--media-max-width", "--media-aspect-ratio",
])

_MULTI_NEWLINE_RE = re.compile(r"(\n[ \t]*){3,}")


def _preserve_multi_newlines(text):
    """Replace 3+ consecutive blank lines with spacing ``<br>`` elements.

    Standard Markdown collapses multiple blank lines into a single paragraph
    break.  This pre-processor keeps the first two newlines (which produce the
    normal ``<p>`` break) and converts each additional blank line into a
    ``<br>`` so that the extra vertical space is preserved in the rendered
    output.

    Lines inside fenced code blocks are left untouched: inserting ``<br>``
    inside a ``<pre><code>`` block would display the raw ``<br>`` text.
    """

    lines = text.split("\n")
    out = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(line)
    # Rejoin and apply the regex replacement on the fence-aware text: lines
    # that were inside a fence are already passed through verbatim above, so
    # the regex never sees them.
    return _MULTI_NEWLINE_RE.sub(
        lambda m: "\n\n" + "<br>" * (m.group(0).count("\n") - 2) + "\n\n",
        "\n".join(out),
    )


_LIST_ITEM_RE = re.compile(r"^(\d+[.)]|[-*+])\s+\S")
_INDENTED_LIST_ITEM_RE = re.compile(r"^([ \t]*)(\d+[.)]|[-*+])\s")


_VIDEO_SHORTCODE_LINE_RE = re.compile(
    r"^[ \t]*\[\[video\s+[^\]]*?\]\][ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_EMBED_SHORTCODE_LINE_RE = re.compile(
    r"^[ \t]*\[\[(?:canvas|kanban)\s+[^\]]*?\]\][ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _isolate_video_shortcodes(text):
    """Ensure each ``[[video …]]`` shortcode sits on its own paragraph.

    The video-embed regex in :func:`_embed_videos_in_html` only matches
    shortcodes that are the sole content of a ``<p>`` element.  Because
    Markdown is rendered with the ``nl2br`` extension, a shortcode that
    sits directly under another line of text gets glued to it via
    ``<br>``, so the shortcode pattern never matches and the user sees
    raw ``[[video …]]`` text in the preview.  Wrap each line that is
    nothing but a video shortcode with surrounding blank lines so it
    always becomes its own paragraph.  Lines inside fenced code blocks
    are left alone.

    ``[[canvas …]]`` and ``[[kanban …]]`` shortcodes are handled
    identically.
    """

    lines = text.split("\n")
    out = []
    in_fence = False
    for idx, line in enumerate(lines):
        stripped_for_fence = line.lstrip()
        if stripped_for_fence.startswith("```") or stripped_for_fence.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if _VIDEO_SHORTCODE_LINE_RE.match(line) or _EMBED_SHORTCODE_LINE_RE.match(line):
            # Pad with blank lines if the previous / next non-empty line
            # is adjacent.  Avoid creating multiple consecutive blanks.
            if out and out[-1].strip() != "":
                out.append("")
            out.append(line.strip())
            # Look ahead: if the next line exists and is non-blank, insert
            # a blank.  We rely on the next iteration to append it normally.
            next_line = lines[idx + 1] if idx + 1 < len(lines) else None
            if next_line is not None and next_line.strip() != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def _normalize_list_indent(text):
    """Convert 2-space (CommonMark) nested-list indents to 4-space.

    Python-Markdown's default list parser requires 4-space indentation for
    sub-lists.  Most users, and most other Markdown editors (CommonMark,
    GitHub, VS Code, Notion, Obsidian): use 2-space indentation, which
    causes nested lists like ``- a\\n  - b`` to flatten into a single level.

    This pre-processor walks the input one list block at a time and, when
    the smallest non-zero indent inside the block is ``< 4`` columns, scales
    every indent in the block up so that 4 columns = one nesting level.
    Lists that already use 4-space (or tab) indentation are left untouched.
    Fenced code blocks are skipped entirely.
    """

    lines = text.split("\n")
    out = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        # Detect the start of a list block at column 0.
        if _LIST_ITEM_RE.match(stripped) and (line[:1] not in (" ", "\t")):
            # Walk forward over consecutive list lines (and indented continuations
            # / blank lines that visually belong to the same block).
            block_start = i
            j = i
            saw_blank = False
            while j < len(lines):
                cur = lines[j]
                cur_stripped = cur.lstrip()
                if cur_stripped.startswith("```") or cur_stripped.startswith("~~~"):
                    break
                if cur.strip() == "":
                    if saw_blank:
                        break
                    saw_blank = True
                    j += 1
                    continue
                if _INDENTED_LIST_ITEM_RE.match(cur):
                    saw_blank = False
                    j += 1
                    continue
                # Indented continuation line (non-list, e.g. wrapped paragraph
                # under a bullet), only keep extending the block if we haven't
                # just seen a blank line.
                if cur[:1] in (" ", "\t") and not saw_blank:
                    j += 1
                    continue
                break
            block = lines[block_start:j]
            normalized = _normalize_list_block(block)
            out.extend(normalized)
            i = j
            continue

        out.append(line)
        i += 1
    return "\n".join(out)


def _normalize_list_block(block):
    """Scale the indents of a single list block so 4 spaces == one level."""
    indents = []
    for ln in block:
        if ln.strip() == "":
            continue
        # Treat tabs as 4 spaces for the purpose of detecting the unit.
        expanded = ln.expandtabs(4)
        spaces = len(expanded) - len(expanded.lstrip(" "))
        if spaces > 0:
            indents.append(spaces)
    if not indents:
        return list(block)
    unit = min(indents)
    if unit >= 4:
        return list(block)  # already 4-space convention
    factor = 4 // unit if unit > 0 else 1
    out = []
    for ln in block:
        if ln.strip() == "":
            out.append(ln)
            continue
        expanded = ln.expandtabs(4)
        spaces = len(expanded) - len(expanded.lstrip(" "))
        rest = expanded[spaces:]
        out.append(" " * (spaces * factor) + rest)
    return out


def _ensure_list_has_blank_line(text):
    """Insert a blank line before a list that directly follows a paragraph.

    Markdown requires a blank line between a paragraph and the start of a
    list; without one, the list lines are merged into the preceding
    paragraph (and separated by ``<br>`` because ``nl2br`` is enabled),
    so a numbered list typed as

        Here is a list:
        1. something
        2. something
        3. something

    renders as plain text rather than ``<ol>``.  Most users do not realise
    a blank line is required, so this pre-processor adds the missing
    blank line before any list item that directly follows a non-blank
    paragraph line.  Lines inside fenced code blocks, indented
    continuations, and existing list items are left alone.
    """
    lines = text.split("\n")
    out = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if i > 0 and _LIST_ITEM_RE.match(line):
            prev = lines[i - 1]
            if (
                prev.strip()
                and not _LIST_ITEM_RE.match(prev)
                and not prev.startswith((" ", "\t"))
            ):
                out.append("")
        out.append(line)
    return "\n".join(out)


def render_markdown(text, embed_videos=False, auto_fix_lists=True):
    """Convert markdown to sanitised HTML.

    *text* may be ``None`` or an empty string; both are returned as ``""``
    without raising an error.

    When *embed_videos* is True, bare YouTube and Vimeo links are replaced
    with responsive iframe embeds after sanitisation.

    When *auto_fix_lists* is True (default), a blank line is inserted before
    list items that directly follow a paragraph or heading. This ensures
    the list is recognised by the Markdown parser even when the user forgot
    the blank line.  Set to False for the live preview pane so the output
    faithfully reflects what the user typed.
    """
    if not text:
        return ""
    if auto_fix_lists:
        text = _ensure_list_has_blank_line(text)
    text = _normalize_list_indent(text)
    text = _isolate_video_shortcodes(text)
    text = _preserve_multi_newlines(text)
    html = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "codehilite", "toc", "nl2br"],
        extension_configs={
            "codehilite": {
                "css_class": "codehilite",
                "guess_lang": False,
            }
        },
    )
    html = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=["http", "https", "ftp", "mailto"],
        strip=True,
        strip_comments=True,
        css_sanitizer=_CSS_SANITIZER,
    )
    if embed_videos:
        html = _embed_videos_in_html(html)
    html = _render_embed_shortcodes(html)
    html = _linkify_mentions(html)
    html = _harden_external_anchors(html)
    return html


def highlight_code_html(code, language=None):
    """Return sanitised HTML for a syntax-highlighted code block."""
    code = "" if code is None else str(code)
    language = (language or "").strip()
    try:
        if language:
            lexer = get_lexer_by_name(language, stripall=False)
        elif code.strip():
            lexer = guess_lexer(code)
        else:
            lexer = TextLexer(stripall=False)
    except ClassNotFound:
        lexer = TextLexer(stripall=False)

    html = highlight(
        code,
        lexer,
        HtmlFormatter(cssclass="codehilite", nowrap=False),
    )
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=["http", "https", "ftp", "mailto"],
        strip=True,
        strip_comments=True,
        css_sanitizer=_CSS_SANITIZER,
    )


_TARGET_BLANK_ANCHOR_RE = re.compile(
    r'<a\b([^>]*?)\btarget\s*=\s*"_blank"([^>]*)>',
    re.IGNORECASE,
)


def _harden_external_anchors(html):
    """Force ``rel="noopener noreferrer"`` on every ``target="_blank"`` anchor.

    Bleach allows ``target`` and ``rel`` attributes on ``<a>`` (see
    :data:`helpers._constants.ALLOWED_ATTRS`), so a wiki contributor can
    open a link in a new tab.  Without ``rel="noopener"`` the destination
    page can pivot back through ``window.opener`` (reverse tabnabbing).
    This post-processor injects the rel attribute, merging with any
    existing ``rel`` value rather than overwriting it.
    """

    def _replace(match):
        """Inject ``rel="noopener noreferrer"`` into a single ``<a>`` tag."""
        before, after = match.group(1), match.group(2)
        attrs = before + after
        # Already has rel?  Merge tokens into the existing value.
        rel_match = re.search(r'\brel\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
        if rel_match:
            existing = set(rel_match.group(1).lower().split())
            existing.update({"noopener", "noreferrer"})
            new_rel = " ".join(sorted(existing))
            attrs_new = (
                attrs[: rel_match.start()]
                + f'rel="{new_rel}"'
                + attrs[rel_match.end():]
            )
            return f'<a{attrs_new}>'
        # No rel attribute: append one before the closing >.
        return f'<a{before} target="_blank"{after} rel="noopener noreferrer">'

    return _TARGET_BLANK_ANCHOR_RE.sub(_replace, html)


_MENTION_RE = re.compile(r"(@[A-Za-z0-9_-]+(?:\s+deleted)?)")


def _linkify_mentions(html):
    """Wrap @username mentions in profile links, excluding matches inside <a> tags."""
    # We want to avoid matching inside <a> tags, <code>, <pre>, etc.
    # Instead, we split by tags and only process non-tag parts.
    parts = re.split(r"(<[^>]+>)", html)
    in_skip_tag = False
    skip_tags = {"a", "code", "pre"}

    for i in range(len(parts)):
        part = parts[i]
        if not part:
            continue
        if part.startswith("<"):
            tag_match = re.match(r"<(/?)([a-z0-9]+)", part, re.IGNORECASE)
            if tag_match:
                is_closing = bool(tag_match.group(1))
                tag_name = tag_match.group(2).lower()
                if tag_name in skip_tags:
                    if is_closing:
                        in_skip_tag = False
                    else:
                        in_skip_tag = True
        elif not in_skip_tag:
            def _replace_mention(m):
                """Replace an @mention with a profile link."""
                mention = m.group(1)
                if mention.lower() == "@account deleted":
                    return mention
                username = mention[1:]
                from flask import url_for
                try:
                    link = url_for('user_profile', username=username)
                    return f'<a class="xs-b5df2325" href="{link}">{mention}</a>'
                except Exception:
                    return mention

            parts[i] = _MENTION_RE.sub(_replace_mention, part)

    return "".join(parts)


_YT_WATCH_RE = re.compile(
    r'<a href="(https?://(?:www\.)?youtube\.com/watch\?[^"<]*)">\1</a>',
    re.IGNORECASE,
)
_YT_SHORT_RE = re.compile(
    r'<a href="(https?://youtu\.be/[^"<]*)">\1</a>',
    re.IGNORECASE,
)
_VIMEO_RE = re.compile(
    r'<a href="(https?://(?:www\.)?vimeo\.com/[^"<]*)">\1</a>',
    re.IGNORECASE,
)

# Bare URL patterns (markdown does not autolink plain URLs; they appear as text)
_YT_WATCH_BARE_RE = re.compile(
    r'<p>\s*(https?://(?:www\.)?youtube\.com/watch\?[^\s<>]*)\s*</p>',
    re.IGNORECASE,
)
_YT_SHORT_BARE_RE = re.compile(
    r'<p>\s*(https?://youtu\.be/[^\s<>]*)\s*</p>',
    re.IGNORECASE,
)
_VIMEO_BARE_RE = re.compile(
    r'<p>\s*(https?://(?:www\.)?vimeo\.com/(\d+)[^\s<>]*)\s*</p>',
    re.IGNORECASE,
)
_CUSTOM_VIDEO_RE = re.compile(
    r"""
    <p>\s*
    \[\[video
    \s+url="(?P<url>[^"]+)"
    (?:\s+width="(?P<width>\d{2,4})")?
    (?:\s+align="(?P<align>none|left|right|center)")?
    (?:\s+ratio="(?P<ratio>16:9|4:3|1:1)")?
    (?:\s+margin="(?P<margin>\d{1,3})")?
    (?:\s+autoplay="(?P<autoplay>true|false)")?
    (?:\s+loop="(?P<loop>true|false)")?
    (?:\s+controls="(?P<controls>true|false)")?
    (?:\s+preload="(?P<preload>none|metadata|auto)")?
    \s*\]\]
    \s*</p>
    """,
    re.IGNORECASE | re.VERBOSE,
)
_VIDEO_WIDTH_RE = re.compile(r"^\d{2,4}$")
_VIDEO_PADDING_BY_RATIO = {
    "16:9": "56.25%",
    "4:3": "75%",
    "1:1": "100%",
}

# ── Canvas & Kanban embed shortcodes
_CANVAS_EMBED_RE = re.compile(
    r"""
    <p>\s*
    \[\[canvas
    \s+slug="(?P<slug>[^"]+)"
    (?:\s+width="(?P<width>\d{2,4})")?
    (?:\s+height="(?P<height>\d{2,4})")?
    \s*\]\]
    \s*</p>
    """,
    re.IGNORECASE | re.VERBOSE,
)
_KANBAN_EMBED_RE = re.compile(
    r"""
    <p>\s*
    \[\[kanban
    \s+board="(?P<board>[^"]+)"
    (?:\s+width="(?P<width>\d{2,4})")?
    (?:\s+height="(?P<height>\d{2,4})")?
    \s*\]\]
    \s*</p>
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EMBED_DIM_RE = re.compile(r"^\d{2,4}$")


def _get_video_embed_src(url):
    """Return the canonical embed URL for a supported video URL, or None."""
    if not url:
        return None
    yt_watch = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url, re.IGNORECASE)
    if yt_watch:
        return f"https://www.youtube.com/embed/{yt_watch.group(1)}"
    yt_short = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url, re.IGNORECASE)
    if yt_short:
        return f"https://www.youtube.com/embed/{yt_short.group(1)}"
    vimeo = re.search(r"vimeo\.com/(\d+)", url, re.IGNORECASE)
    if vimeo:
        return f"https://player.vimeo.com/video/{vimeo.group(1)}"
    return None


def _parse_bool(value, default=False):
    """Parse a boolean value from a string or None."""
    if value is None:
        return default
    return str(value).strip().lower() == "true"


def _normalize_video_options(width=None, align=None, ratio=None,
                             margin=None, autoplay=None, loop=None,
                             controls=None, preload=None):
    """Return sanitised video sizing and playback options."""
    width = str(width).strip() if width is not None else ""
    if not _VIDEO_WIDTH_RE.match(width):
        width = ""
    align = (align or "center").strip().lower()
    if align not in {"none", "left", "right", "center"}:
        align = "center"
    ratio = (ratio or "16:9").strip()
    if ratio not in _VIDEO_PADDING_BY_RATIO:
        ratio = "16:9"
    # Margin (uniform vertical margin in px, 0-200)
    margin_str = str(margin).strip() if margin is not None else ""
    margin_val = ""
    if margin_str.isdigit():
        margin_int = int(margin_str)
        if 0 <= margin_int <= 200:
            margin_val = margin_str
    # Playback options
    autoplay_val = _parse_bool(autoplay, default=False)
    loop_val = _parse_bool(loop, default=False)
    # Controls defaults to True; only False when explicitly set to "false"
    if controls is not None and str(controls).strip().lower() == "false":
        controls_val = False
    else:
        controls_val = True
    preload_val = (preload or "").strip().lower()
    if preload_val not in {"none", "metadata", "auto"}:
        preload_val = ""
    return width, align, ratio, margin_val, autoplay_val, loop_val, controls_val, preload_val


def _make_video_iframe(embed_src, source_url=None, width=None, align="center",
                       ratio="16:9", margin=None, autoplay=None, loop=None,
                       controls=None, preload=None):
    """Return a responsive iframe HTML string for the given embed URL."""
    width, align, ratio, margin_val, autoplay_val, loop_val, controls_val, preload_val = (
        _normalize_video_options(
            width=width, align=align, ratio=ratio, margin=margin,
            autoplay=autoplay, loop=loop, controls=controls, preload=preload,
        )
    )
    # Use both legacy and new classes for backward compatibility
    classes = ["video-embed", f"video-embed-{align}", "media-video-container"]
    if align == "left":
        classes.append("media-left")
    elif align == "right":
        classes.append("media-right")
    elif align == "center":
        classes.append("media-center")
    styles = [
        "position:relative",
        f"padding-bottom:{_VIDEO_PADDING_BY_RATIO[ratio]}",
        "height:0",
        "overflow:hidden",
        "max-width:100%",
    ]
    if width:
        styles.append(f"width:min({width}px,100%)")
    elif align == "none":
        styles.append("width:100%")
    if margin_val:
        styles.append(f"--media-margin-top:{margin_val}px")
        styles.append(f"--media-margin-bottom:{margin_val}px")
        styles.append(f"margin-top:{margin_val}px")
        styles.append(f"margin-bottom:{margin_val}px")
    if align == "center":
        styles.append("margin-left:auto")
        styles.append("margin-right:auto")
    elif align == "left":
        styles.append("float:left")
        styles.append("clear:left")
        styles.append("margin-right:1.5rem")
    elif align == "right":
        styles.append("float:right")
        styles.append("clear:right")
        styles.append("margin-left:1.5rem")
    else:
        if not margin_val:
            styles.append("margin-top:1rem")
            styles.append("margin-bottom:1rem")
    attrs = [
        f'class="{" ".join(classes)}"',
        f'data-bw-source-url="{escape(source_url if source_url is not None else embed_src, quote=True)}"',
        f'data-bw-align="{align}"',
        f'data-bw-ratio="{ratio}"',
        f'style="{";".join(styles)}"',
    ]
    if width:
        attrs.append(f'data-bw-width="{width}"')
    if margin_val:
        attrs.append(f'data-bw-margin="{margin_val}"')
    if autoplay_val:
        attrs.append('data-bw-autoplay="true"')
    if loop_val:
        attrs.append('data-bw-loop="true"')
    if not controls_val:
        attrs.append('data-bw-controls="false"')
    if preload_val:
        attrs.append(f'data-bw-preload="{preload_val}"')
    # Build iframe src with playback params for YouTube/Vimeo
    iframe_params = []
    if autoplay_val:
        iframe_params.append("autoplay=1")
    if loop_val:
        iframe_params.append("loop=1")
    if not controls_val and embed_src.startswith("https://www.youtube.com/embed/"):
        iframe_params.append("controls=0")
    iframe_src = escape(embed_src, quote=True)
    if iframe_params:
        separator = "&amp;" if "?" in embed_src else "?"
        iframe_src += separator + "&amp;".join(iframe_params)
    return (
        f'<div {" ".join(attrs)}>'
        f'<iframe src="{iframe_src}" '
        'class="media-responsive-iframe" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0" '
        'allowfullscreen loading="lazy"></iframe>'
        '</div>'
    )


def _embed_videos_in_html(html):
    """Replace bare YouTube/Vimeo anchor links with responsive iframe embeds."""
    def _yt_watch_replace(m):
        """Replace a YouTube watch-URL match with an iframe embed."""
        source_url = m.group(1)
        embed_src = _get_video_embed_src(source_url)
        if not embed_src:
            return m.group(0)
        return _make_video_iframe(embed_src, source_url=source_url)

    def _yt_short_replace(m):
        """Replace a youtu.be short-URL match with an iframe embed."""
        source_url = m.group(1)
        embed_src = _get_video_embed_src(source_url)
        if not embed_src:
            return m.group(0)
        return _make_video_iframe(embed_src, source_url=source_url)

    def _vimeo_replace(m):
        """Replace a Vimeo URL match with an iframe embed."""
        source_url = m.group(1)
        embed_src = _get_video_embed_src(source_url)
        if not embed_src:
            return m.group(0)
        return _make_video_iframe(embed_src, source_url=source_url)

    def _custom_video_replace(m):
        """Replace a persisted video shortcode with a configurable iframe embed."""
        source_url = m.group("url")
        embed_src = _get_video_embed_src(source_url)
        if not embed_src:
            return m.group(0)
        return _make_video_iframe(
            embed_src,
            source_url=source_url,
            width=m.group("width"),
            align=m.group("align") or "center",
            ratio=m.group("ratio") or "16:9",
            margin=m.group("margin"),
            autoplay=m.group("autoplay"),
            loop=m.group("loop"),
            controls=m.group("controls"),
            preload=m.group("preload"),
        )

    html = _CUSTOM_VIDEO_RE.sub(_custom_video_replace, html)
    # Handle linked URLs (markdown angle-bracket or explicit link syntax)
    html = _YT_WATCH_RE.sub(_yt_watch_replace, html)
    html = _YT_SHORT_RE.sub(_yt_short_replace, html)
    html = _VIMEO_RE.sub(_vimeo_replace, html)
    # Handle bare URLs (plain text in <p> tags, no markdown linking)
    html = _YT_WATCH_BARE_RE.sub(_yt_watch_replace, html)
    html = _YT_SHORT_BARE_RE.sub(_yt_short_replace, html)
    html = _VIMEO_BARE_RE.sub(_vimeo_replace, html)
    return html


def _render_embed_shortcodes(html):
    """Replace ``[[canvas …]]`` and ``[[kanban …]]`` shortcodes with embed containers."""
    def _canvas_replace(m):
        """Render one canvas shortcode match as an embed placeholder."""
        slug = m.group("slug")
        width = m.group("width") or "100%"
        height = m.group("height") or "400"
        if _EMBED_DIM_RE.match(str(width)):
            width = f"{width}px"
        if _EMBED_DIM_RE.match(str(height)):
            height = f"{height}px"
        safe_slug = slug.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<div class="bw-embed bw-embed-canvas" '
            f'data-embed-type="canvas" data-embed-slug="{safe_slug}" '
            f'style="width:{escape(width, quote=True)};height:{escape(height, quote=True)};max-width:100%;position:relative;margin:1rem 0;border:1px solid rgba(255,255,255,.12);border-radius:8px;overflow:hidden;background:var(--bg)">'
            f'</div>'
        )

    def _kanban_replace(m):
        """Render one kanban shortcode match as an embed placeholder."""
        board = m.group("board")
        width = m.group("width") or "100%"
        height = m.group("height") or "400"
        if _EMBED_DIM_RE.match(str(width)):
            width = f"{width}px"
        if _EMBED_DIM_RE.match(str(height)):
            height = f"{height}px"
        safe_board = board.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<div class="bw-embed bw-embed-kanban" '
            f'data-embed-type="kanban" data-embed-board="{safe_board}" '
            f'style="width:{escape(width, quote=True)};height:{escape(height, quote=True)};max-width:100%;position:relative;margin:1rem 0;border:1px solid rgba(255,255,255,.12);border-radius:8px;overflow:hidden;background:var(--bg)">'
            f'</div>'
        )
        return (
            f'<div class="bw-embed bw-embed-kanban" '
            f'data-embed-type="kanban" data-embed-board="{escape(board, quote=True)}" '
            f'style="width:{escape(width, quote=True)};height:{escape(height, quote=True)};max-width:100%;position:relative;margin:1rem 0;border:1px solid rgba(255,255,255,.12);border-radius:8px;overflow:hidden;background:var(--bg)">'
            f'</div>'
        )

    html = _CANVAS_EMBED_RE.sub(_canvas_replace, html)
    html = _KANBAN_EMBED_RE.sub(_kanban_replace, html)
    return html
