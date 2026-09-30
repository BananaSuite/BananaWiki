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
"""

from __future__ import annotations

import logging
import os
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fpdf import FPDF
from fpdf.fonts import TextStyle

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
IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_UPLOAD_PATH = re.compile(r"^/static/uploads/([0-9A-Za-z_.-]+)$")
_ACCENT = (74, 116, 190)
_MUTED = (110, 116, 130)
_TEXT = (33, 37, 46)


def _find_font(name: str) -> str | None:
    for folder in FONT_DIRS:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def _register_fonts(pdf: FPDF) -> tuple[str, str, bool]:
    """Register DejaVu when available; return ``(sans, mono, unicode)`` family names."""
    regular = _find_font("DejaVuSans.ttf")
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

    # Helpers ------------------------------------------------------------------

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
        size = _pixel_size(path) if path else None
        if not size:
            return f"<i>[{escape(alt or 'image')}]</i>" if alt else ""
        self.images += 1
        width = min(self.image_width, size[0] * 0.75)
        return f'<img src="{escape(str(path))}" width="{width:.0f}">'

    # Parser callbacks -----------------------------------------------------------

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
            start = attrs.get("start") if tag == "ol" else None
            self.out.append(f'<ol start="{int(start)}">' if start and start.isdigit() else f"<{tag}>")
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


def _pixel_size(path: Path) -> tuple[int, int] | None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


class _Document(FPDF):
    """A4 document with a running header (from page 2) and a page-number footer."""

    def __init__(self, *, title: str, site_name: str):
        super().__init__(format="A4")
        self.doc_title = title
        self.site_name = site_name
        self.sans, self.mono, self.unicode = _register_fonts(self)
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)
        self.set_title(self.clean(title))
        self.set_creator("BananaWiki")

    def clean(self, text: str) -> str:
        text = (text or "").replace("\r", "")
        return text if self.unicode else text.encode("latin-1", "replace").decode("latin-1")

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


def _write_body(pdf: _Document, html: str) -> None:
    pdf.set_font(pdf.sans, "", 10.5)
    pdf.set_text_color(*_TEXT)
    pdf.write_html(html, font_family=pdf.sans, tag_styles=_tag_styles(pdf), li_prefix_color=_ACCENT,
                   warn_on_tags_not_matching=False)


def _write_plain(pdf: _Document, source: str) -> None:
    """Fallback when the layout engine rejects the structure: the Markdown as plain text."""
    pdf.set_font(pdf.sans, "", 10)
    pdf.set_text_color(*_TEXT)
    for block in re.split(r"\n\s*\n", source.replace("\r", "")):
        text = pdf.clean(block.strip())
        if text:
            pdf.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)


def render(*, title: str, content: str, site_name: str, base_url: str, meta: list[str]) -> bytes:
    """The PDF for one page (or history entry) as bytes."""
    pdf = _Document(title=title, site_name=site_name)
    _cover(pdf, meta)
    simplifier = _Simplifier(base_url=base_url, clean_text=pdf.clean,
                             image_width=(pdf.w - pdf.l_margin - pdf.r_margin) * pdf.k)
    simplifier.feed(markdown.render(content, embed_videos=False))
    simplifier.close()
    try:
        _write_body(pdf, simplifier.result())
    except Exception:  # noqa: BLE001 - fpdf2 rejects some nestings; fall back to plain text
        log.warning("PDF layout failed for %r; exporting plain text", title, exc_info=True)
        pdf = _Document(title=title, site_name=site_name)
        _cover(pdf, meta)
        _write_plain(pdf, content)
    return bytes(pdf.output())
