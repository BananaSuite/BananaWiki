"""PDF export of a page with fpdf2.

The page Markdown is rendered by :func:`markdown.render` (the same sanitised
HTML the page view shows) and then reduced by :class:`_Simplifier` to the
small HTML subset fpdf2's ``write_html`` supports: headings, paragraphs,
emphasis, links, lists, quotes, code, rules and simple tables whose cells
hold plain text. Images are embedded only when they are files of this
wiki's ``uploads`` folder; nothing is ever fetched from the network.

Unicode needs a TrueType font: DejaVu Sans is used when the host has it
(``fonts-dejavu-core`` on Debian/Ubuntu). Without it the built-in Latin-1
fonts are used and other characters are replaced by ``?``.

Laying out a long page takes seconds, so finished PDFs are kept per process
by the hash of everything they are made from, up to :data:`CACHE_LIMIT`
bytes: exporting an unchanged page again costs nothing. The embedded upload
files are part of that (name, size and modification time of each): a PDF
kept from before an image was deleted or replaced is not served again.

The work of one PDF is bounded: each process lays out one PDF at a time
(others wait up to :data:`BUILD_WAIT_SECONDS`, then :class:`Busy`), a PDF
stops with a note after :data:`MAX_PAGES` pages or :data:`LAYOUT_SECONDS` of
layout (checked at each page break and at the end of each paragraph, so inside
one long code block or table too; a PDF cut by time is kept for
:data:`TIME_CUT_CACHE_SECONDS` only), and images are decoded one at a time (:func:`storage.image_slot`), within
:data:`MAX_DECODED_PIXELS` per PDF, and embedded as JPEG pictures of at most
:data:`IMAGE_BOX` pixels (enough for print), within :data:`MAX_IMAGE_BYTES`
per PDF. Images past either budget are shown by their description.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fpdf import FPDF
from fpdf.fonts import TextStyle

from bananawiki.core.numbers import ascii_int

from ... import markdown, storage

log = logging.getLogger("bananawiki.page_export")
# fpdf2 logs a warning for every glyph missing from the font; one page can have thousands.
logging.getLogger("fpdf").setLevel(logging.ERROR)

FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype",
    "/usr/local/share/fonts",
    "/usr/local/share/fonts/dejavu",
    "/opt/homebrew/share/fonts",
    "/Library/Fonts",
)
MAX_IMAGES = 50
#: Pixels of the uploads one PDF decodes (JPEGs are decoded at a reduced scale when they are large).
MAX_DECODED_PIXELS = 160_000_000
#: Bytes of (re-encoded) pictures one PDF embeds.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
#: The largest embedded picture, in pixels: about 200 dpi across an A4 page's text width.
IMAGE_BOX = (1400, 2000)
JPEG_QUALITY = 85
CACHE_LIMIT = 32 * 1024 * 1024
#: How long an export waits for this process's other PDF to be laid out before :class:`Busy`.
BUILD_WAIT_SECONDS = 10
#: A PDF ends with a note once its layout has this many pages or has run this long; a 1 M-character page of
#: text is 150 to 300 pages and takes a few seconds, while a page of tiny paragraphs could run to 10,000.
MAX_PAGES = 500
LAYOUT_SECONDS = 8.0
#: A PDF cut by :data:`LAYOUT_SECONDS` (which depends on how busy the CPU was) is kept this long only, then
#: laid out again; one cut by :data:`MAX_PAGES` is kept like a complete one.
TIME_CUT_CACHE_SECONDS = 300
# A4, in millimetres, and the margins of every page.
PAGE_WIDTH = 210
MARGIN = 18
# The widest image (points): the text width.
_IMAGE_WIDTH = (PAGE_WIDTH - 2 * MARGIN) * 72 / 25.4
IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_UPLOAD_PATH = re.compile(r"^/static/uploads/([0-9A-Za-z_.-]+)$")
_IMAGE_MARK = re.compile(r'<img src="bw-image:(\d+)" width="\d+">')
_ACCENT = (138, 101, 0)
_MUTED = (107, 105, 100)
_TEXT = (32, 33, 36)


def _find_font(name: str) -> str | None:
    for folder in FONT_DIRS:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def _unicode_font() -> str | None:
    return _find_font("DejaVuSans.ttf")


def _clean(text: str, unicode: bool) -> str:
    """*text* as the fonts can show it: without a Unicode font, characters outside Latin-1 become ``?``."""
    text = (text or "").replace("\r", "")
    return text if unicode else text.encode("latin-1", "replace").decode("latin-1")


def _register_fonts(pdf: FPDF) -> tuple[str, str, bool]:
    """Register DejaVu when available; return ``(sans, mono, unicode)`` family names."""
    regular = _unicode_font()
    if not regular:
        return "helvetica", "courier", False
    bold = _find_font("DejaVuSans-Bold.ttf") or regular
    italic = _find_font("DejaVuSans-Oblique.ttf") or regular
    bold_italic = _find_font("DejaVuSans-BoldOblique.ttf") or bold
    mono = _find_font("DejaVuSansMono.ttf") or regular
    pdf.add_font("DejaVu", "", regular)
    pdf.add_font("DejaVu", "B", bold)
    pdf.add_font("DejaVu", "I", italic)
    pdf.add_font("DejaVu", "BI", bold_italic)
    for style in ("", "B", "I", "BI"):
        pdf.add_font("DejaVuMono", style, mono)
    return "DejaVu", "DejaVuMono", True


class _Simplifier(HTMLParser):
    """Rewrite sanitised page HTML into markup fpdf2's ``write_html`` can lay out."""

    BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote", "table", "thead",
              "tbody", "tr"}
    INLINE = {"b": "b", "strong": "b", "mark": "b", "i": "i", "em": "i", "u": "u", "ins": "u",
              "code": "code", "kbd": "code"}
    AS_PARAGRAPH = {"dt", "dd", "summary", "figcaption"}
    DROPPED = {"script", "style", "iframe", "object", "svg", "button", "video", "audio"}

    def __init__(self, *, base_url: str, clean_text, image_width: float):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.clean_text = clean_text
        self.image_width = image_width
        self.out: list[str] = []
        self.stack: list[str] = []  # emitted inline/link tags, to close them in order
        self.in_pre = 0
        self.in_cell = False
        self.dropped = 0
        self.images = 0
        self.decoded = 0  # pixels the embedded images will cost to decode
        self.files: list[str] = []  # what each embedded upload was when it was read
        self.paths: list[Path] = []  # the uploads behind ``bw-image:<index>``
        self.alts: list[str] = []

    # Helpers

    def _link(self, href: str | None) -> str | None:
        if not href:
            return None
        absolute = urljoin(self.base_url, href)
        return absolute if urlsplit(absolute).scheme in ("http", "https", "mailto") else None

    def _image(self, attrs: dict[str, str | None]) -> str:
        alt = self.clean_text(attrs.get("alt") or "")
        match = _UPLOAD_PATH.match(attrs.get("src") or "")
        path = None
        if match and self.images < MAX_IMAGES and storage.extension(match.group(1)) in IMAGE_TYPES:
            path = storage.resolve("uploads", match.group(1))
        found = _pixel_size(path) if path else None
        if found:
            size, decoded = found
            if self.decoded + decoded > MAX_DECODED_PIXELS:
                found = None
        if not found:
            return _placeholder(alt)
        self.images += 1
        self.decoded += decoded
        self.files.append(_file_state(path))
        self.paths.append(path)
        self.alts.append(alt)
        width = min(self.image_width, size[0] * 0.75)
        # Replaced by the picture (or the description) when the PDF is laid out: see _embed_images.
        return f'<img src="bw-image:{len(self.paths) - 1}" width="{width:.0f}">'

    # Parser callbacks

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)
        if tag in self.DROPPED:
            self.dropped += 1
            return
        if self.dropped:
            return
        if self.in_cell:
            if tag == "br":
                self.out.append(" ")
            return
        if self.in_pre:
            return
        if tag in ("td", "th"):
            self.in_cell = True
            self.out.append(f"<{tag}>")
        elif tag == "pre":
            self.in_pre += 1
            self.out.append("<pre>")
        elif tag in self.BLOCKS:
            start = ascii_int(attrs.get("start"), 6) if tag == "ol" else None
            self.out.append(f'<ol start="{start}">' if start is not None else f"<{tag}>")
        elif tag in ("hr", "br"):
            self.out.append(f"<{tag}>")
        elif tag in self.AS_PARAGRAPH:
            self.out.append("<p>")
        elif tag == "img":
            self.out.append(self._image(attrs))
        elif tag == "a":
            href = self._link(attrs.get("href"))
            if href:
                self.out.append(f'<a href="{escape(href)}">')
                self.stack.append("a")
            else:
                self.stack.append("")
        elif tag in self.INLINE:
            mapped = self.INLINE[tag]
            self.out.append(f"<{mapped}>")
            self.stack.append(mapped)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.DROPPED:
            self.dropped = max(0, self.dropped - 1)
            return
        if self.dropped:
            return
        if self.in_cell:
            if tag in ("td", "th"):
                self.in_cell = False
                self.out.append(f"</{tag}>")
            return
        if tag == "pre" and self.in_pre:
            self.in_pre -= 1
            if not self.in_pre:
                self.out.append("</pre>")
            return
        if self.in_pre:
            return
        if tag in self.BLOCKS:
            self.out.append(f"</{tag}>")
        elif tag in self.AS_PARAGRAPH:
            self.out.append("</p>")
        elif (tag == "a" or tag in self.INLINE) and self.stack:
            emitted = self.stack.pop()
            if emitted:
                self.out.append(f"</{emitted}>")

    def handle_data(self, data: str) -> None:
        if self.dropped or not data:
            return
        text = self.clean_text(data)
        if self.in_cell:
            text = re.sub(r"\s+", " ", text)
        self.out.append(escape(text.expandtabs(4) if self.in_pre else text, quote=False))

    def result(self) -> str:
        return "".join(self.out)


def _file_state(path: Path) -> str:
    try:
        status = path.stat()
    except OSError:
        return f"{path.name}:gone"
    return f"{path.name}:{status.st_size}:{status.st_mtime_ns}"


def _placeholder(alt: str) -> str:
    return f"<i>[{escape(alt or 'image')}]</i>" if alt else ""


def _draft_scale(size: tuple[int, int]) -> int:
    """The reduction (1, 2, 4 or 8) a JPEG of *size* is decoded at to fill :data:`IMAGE_BOX`."""
    scale = 1
    while scale < 8 and size[0] // (scale * 2) >= IMAGE_BOX[0] and size[1] // (scale * 2) >= IMAGE_BOX[1]:
        scale *= 2
    return scale


def _pixel_size(path: Path) -> tuple[tuple[int, int], int] | None:
    """The image's size from its header, and the pixels decoding it for the PDF costs."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height > storage.MAX_IMAGE_PIXELS:
                return None
            scale = _draft_scale(image.size) if image.format == "JPEG" else 1
            return image.size, (width // scale) * (height // scale)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def _picture(path: Path) -> bytes | None:
    """The upload at *path* as a JPEG of at most :data:`IMAGE_BOX` pixels on white, decoded in this
    process's image slot; None when it cannot be read."""
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = storage.MAX_IMAGE_PIXELS
    try:
        with storage.image_slot(), Image.open(path) as image:
            if image.size[0] * image.size[1] > storage.MAX_IMAGE_PIXELS:
                return None
            if image.format == "JPEG":
                image.draft("RGB", (image.size[0] // _draft_scale(image.size),
                                    image.size[1] // _draft_scale(image.size)))
            picture = image.convert("RGBA")
            picture.thumbnail(IMAGE_BOX)
            flat = Image.new("RGB", picture.size, (255, 255, 255))
            flat.paste(picture, mask=picture.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=JPEG_QUALITY)
            return out.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, storage.UploadError):
        return None


def _embed_images(body: str, paths: list[Path], alts: list[str]) -> tuple[str, dict[str, bytes]]:
    """*body* with each ``bw-image:<index>`` picture prepared (see :func:`_picture`), or its
    description once :data:`MAX_IMAGE_BYTES` are used; and the pictures by ``src``."""
    pictures: dict[str, bytes] = {}
    used = 0

    def embed(match: re.Match[str]) -> str:
        nonlocal used
        index = int(match.group(1))
        data = _picture(paths[index]) if used < MAX_IMAGE_BYTES else None
        if data is None or used + len(data) > MAX_IMAGE_BYTES:
            used = MAX_IMAGE_BYTES if data is not None else used
            return _placeholder(alts[index])
        used += len(data)
        pictures[f"bw-image:{index}"] = data
        return match.group(0)

    return _IMAGE_MARK.sub(embed, body), pictures


class _Document(FPDF):
    """A4 document with a running header (from page 2) and a page-number footer."""

    def __init__(self, *, title: str, site_name: str, deadline: float | None = None):
        super().__init__(format="A4")
        self.doc_title = title
        self.site_name = site_name
        self.deadline = time.monotonic() + LAYOUT_SECONDS if deadline is None else deadline
        self.budgeted = True  # off once the layout is cut, for the note
        self.cut_by_time = False
        self.sans, self.mono, self.unicode = _register_fonts(self)
        self.set_auto_page_break(auto=True, margin=MARGIN)
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_title(self.clean(title))
        self.set_creator("BananaWiki")

    def clean(self, text: str) -> str:
        return _clean(text, self.unicode)

    def check_budget(self) -> None:
        """Raise :class:`_LayoutCut` once :data:`MAX_PAGES` pages are laid out or :data:`LAYOUT_SECONDS` passed."""
        if not self.budgeted:
            return
        if self.page_no() >= MAX_PAGES:
            raise _LayoutCut
        if time.monotonic() > self.deadline:
            self.cut_by_time = True
            raise _LayoutCut

    def _perform_page_break(self) -> None:
        """fpdf2's page break, checking the budget first: one block (a long code block, a table, a page the
        Markdown budget renders as plain text) is laid out in one go and can run to thousands of pages.

        The cut note then starts from the states fpdf2 left open: :func:`_render` closes them.
        """
        self.check_budget()
        super()._perform_page_break()

    def close_graphics_states(self) -> None:
        """Close the graphics states fpdf2 left open in this page's stream (a table opens one), as fpdf2
        does before each new page: a layout cut in the middle leaves them open, and every ``q`` in a page's
        stream needs its ``Q``."""
        while self._is_current_graphics_state_nested():
            self._pop_local_stack()
            self._end_local_context()

    def text_columns(self, *args, **kwargs):
        """fpdf2's text region, forgetting each paragraph once it is laid out, then checking the budget.

        ``write_html`` puts the whole page in one region and lays it out again at the end of every
        paragraph, rebuilding (empty) lines for all the paragraphs before it: 12,000 paragraphs took a
        minute. A laid-out paragraph has nothing left to lay out, so dropping it changes nothing else.
        """
        columns = super().text_columns(*args, **kwargs)
        render = columns.render

        def render_and_forget() -> None:
            render()
            paragraphs = getattr(columns, "_paragraphs", None)
            if isinstance(paragraphs, list):
                del paragraphs[:-1]  # the last one stays: fpdf2 may still write into it
            self.check_budget()

        columns.render = render_and_forget
        return columns

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(self.sans, "", 8)
        self.set_text_color(*_MUTED)
        width = (self.w - self.l_margin - self.r_margin) / 2
        self.cell(width, 6, self.clean(self.doc_title)[:90], align="L")
        self.cell(width, 6, self.clean(self.site_name)[:60], align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*_ACCENT)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(self.sans, "", 8)
        self.set_text_color(*_MUTED)
        width = (self.w - self.l_margin - self.r_margin) / 2
        self.cell(width, 6, self.clean(self.site_name)[:60], align="L")
        self.cell(width, 6, f"{self.page_no()} / {{nb}}", align="R")


def _cover(pdf: _Document, meta: list[str]) -> None:
    pdf.add_page()
    pdf.set_fill_color(*_ACCENT)
    pdf.rect(0, 0, pdf.w, 6, style="F")
    pdf.set_y(16)
    pdf.set_font(pdf.sans, "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 6, pdf.clean(pdf.site_name).upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(pdf.sans, "B", 22)
    pdf.set_text_color(*_TEXT)
    pdf.multi_cell(0, 10, pdf.clean(pdf.doc_title), new_x="LMARGIN", new_y="NEXT")
    if meta:
        pdf.set_font(pdf.sans, "", 9)
        pdf.set_text_color(*_MUTED)
        pdf.multi_cell(0, 5, pdf.clean(" · ".join(meta)), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*_ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.ln(6)


def _tag_styles(pdf: _Document) -> dict[str, TextStyle]:
    heading = {"h1": 18, "h2": 15, "h3": 13, "h4": 12, "h5": 11, "h6": 10}
    styles = {tag: TextStyle(font_family=pdf.sans, font_style="B", font_size_pt=size, color=_TEXT, t_margin=5,
                             b_margin=2) for tag, size in heading.items()}
    styles["code"] = TextStyle(font_family=pdf.mono, color=(160, 50, 90))
    styles["pre"] = TextStyle(font_family=pdf.mono, font_size_pt=9, color=(45, 50, 64), t_margin=3, b_margin=3)
    styles["blockquote"] = TextStyle(font_style="I", color=_MUTED, l_margin=8, t_margin=2, b_margin=2)
    styles["a"] = TextStyle(color=(40, 100, 180), font_style="U")
    return styles


def _write_body(pdf: _Document, html: str, pictures: dict[str, bytes]) -> None:
    pdf.set_font(pdf.sans, "", 10.5)
    pdf.set_text_color(*_TEXT)
    pdf.write_html(html, font_family=pdf.sans, tag_styles=_tag_styles(pdf), li_prefix_color=_ACCENT,
                   warn_on_tags_not_matching=False, image_map=lambda src: io.BytesIO(pictures[src]))


def _write_plain(pdf: _Document, source: str) -> None:
    """Fallback when the layout engine rejects the structure: the Markdown as plain text."""
    pdf.set_font(pdf.sans, "", 10)
    pdf.set_text_color(*_TEXT)
    for block in re.split(r"\n\s*\n", source.replace("\r", "")):
        text = pdf.clean(block.strip())
        if text:
            pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
            pdf.check_budget()


def _write_cut_note(pdf: _Document, note: str) -> None:
    pdf.budgeted = False
    pdf.ln(4)
    pdf.set_font(pdf.sans, "I", 10)
    pdf.set_text_color(*_MUTED)
    pdf.multi_cell(0, 5, pdf.clean(note), new_x="LMARGIN", new_y="NEXT")


_cache: OrderedDict[bytes, bytes] = OrderedDict()
_cache_size = 0
_cache_expiry: dict[bytes, float] = {}  # the PDFs cut by time: when they are laid out again
_cache_lock = threading.Lock()
_build_slot = threading.BoundedSemaphore(1)


class Busy(Exception):
    """This process is laying out another PDF (for longer than :data:`BUILD_WAIT_SECONDS`)."""


class _LayoutCut(Exception):
    """The layout used its budget (:meth:`_Document.check_budget`); the PDF ends with the cut note."""


#: The note a cut PDF ends with, when the caller gives none.
CUT_NOTE = "The PDF ends here: the rest of the page is too long to export. Read it in the wiki."


def _cache_key(*parts: str) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
    return digest.digest()


def _simplify(content: str, *, base_url: str, unicode: bool) -> _Simplifier:
    """The page HTML fpdf2 lays out (``result()``), the uploads it embeds and their state (``files``)."""
    simplifier = _Simplifier(base_url=base_url, clean_text=lambda text: _clean(text, unicode),
                             image_width=_IMAGE_WIDTH)
    simplifier.feed(markdown.render(content, embed_videos=False))
    simplifier.close()
    return simplifier


def _now() -> float:
    return time.monotonic()


def _cached(key: bytes) -> bytes | None:
    global _cache_size
    with _cache_lock:
        expiry = _cache_expiry.get(key)
        if expiry is not None and _now() >= expiry:
            del _cache_expiry[key]
            _cache_size -= len(_cache.pop(key, b""))
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
        return cached


def render(*, title: str, content: str, site_name: str, base_url: str, meta: list[str],
           cut_note: str = CUT_NOTE) -> bytes:
    """The PDF for one page (or history entry) as bytes, from the cache when it was made before.

    The key covers the embedded upload files as they are now, so the
    (cheap) simplification runs every time and the layout only when needed.
    Raises :class:`Busy` when this process's turn to lay out a PDF does not
    come within :data:`BUILD_WAIT_SECONDS`. A layout cut short (see
    :data:`MAX_PAGES`) ends with *cut_note*, and is kept like any other, or
    for :data:`TIME_CUT_CACHE_SECONDS` only when it was cut by time.
    """
    global _cache_size
    unicode = _unicode_font() is not None
    simplified = _simplify(content, base_url=base_url, unicode=unicode)
    key = _cache_key(title, content, site_name, base_url, cut_note, str(unicode), str(len(meta)), *meta,
                     *simplified.files)
    cached = _cached(key)
    if cached is not None:
        return cached
    if not _build_slot.acquire(timeout=BUILD_WAIT_SECONDS):
        raise Busy
    try:
        cached = _cached(key)  # made while this export waited
        if cached is not None:
            return cached
        body, pictures = _embed_images(simplified.result(), simplified.paths, simplified.alts)
        data, cut_by_time = _render(title=title, content=content, body=body, pictures=pictures,
                                    site_name=site_name, meta=meta, cut_note=cut_note)
    finally:
        _build_slot.release()
    if len(data) <= CACHE_LIMIT // 4:
        with _cache_lock:
            if key not in _cache:
                _cache[key] = data
                _cache_size += len(data)
                if cut_by_time:
                    _cache_expiry[key] = _now() + TIME_CUT_CACHE_SECONDS
            while _cache_size > CACHE_LIMIT and _cache:
                dropped_key, dropped = _cache.popitem(last=False)
                _cache_expiry.pop(dropped_key, None)
                _cache_size -= len(dropped)
    return data


def _render(*, title: str, content: str, body: str, pictures: dict[str, bytes], site_name: str,
            meta: list[str], cut_note: str = CUT_NOTE) -> tuple[bytes, bool]:
    """Lay out *body* (from :func:`_embed_images`); *content* is the fallback when fpdf2 rejects it.

    Returns the PDF and whether :data:`LAYOUT_SECONDS` cut it.
    """
    pdf = _Document(title=title, site_name=site_name)
    _cover(pdf, meta)
    try:
        try:
            _write_body(pdf, body, pictures)
        except _LayoutCut:
            raise
        except Exception:  # noqa: BLE001 - fpdf2 rejects some nestings; fall back to plain text
            log.warning("PDF layout failed for %r; exporting plain text", title, exc_info=True)
            pdf = _Document(title=title, site_name=site_name, deadline=pdf.deadline)  # the same budget
            _cover(pdf, meta)
            _write_plain(pdf, content)
    except _LayoutCut:
        # Stopped at the end of a paragraph or before a new page, maybe inside a graphics state fpdf2 opened.
        log.info("PDF of %r cut after %d pages", title, pdf.page_no())
        pdf.close_graphics_states()
        _write_cut_note(pdf, cut_note)
    return bytes(pdf.output()), pdf.cut_by_time
