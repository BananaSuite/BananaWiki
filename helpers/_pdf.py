"""PDF generation helpers for wiki page export.

The renderer parses wiki page Markdown into a small block-level AST and emits
styled drawing calls against ``fpdf2``.  It deliberately covers the subset of
Markdown that wiki pages typically use: headings, paragraphs, emphasis,
inline code, links, fenced code blocks, blockquotes, lists, horizontal rules
and simple pipe tables, and falls back to a plain-text rendering for any
construct it does not recognise.
"""

import io
import os
import re

from fpdf import FPDF



_C_TITLE = (32, 40, 56)
_C_TEXT = (40, 44, 56)
_C_MUTED = (122, 128, 142)
_C_HR = (215, 219, 226)
_C_HEADING = (40, 60, 100)
_C_HEADING_ACCENT = (74, 144, 217)
_C_QUOTE_BAR = (74, 144, 217)
_C_QUOTE_TEXT = (90, 95, 110)
_C_CODE_BG = (243, 245, 250)
_C_CODE_BORDER = (215, 219, 226)
_C_CODE_TEXT = (50, 55, 70)
_C_INLINE_CODE_TEXT = (170, 50, 80)
_C_LINK = (52, 130, 197)
_C_TABLE_HEADER_BG = (236, 240, 245)
_C_TABLE_BORDER = (215, 219, 226)
_C_BAND = (74, 144, 217)



_FONT_DIRS = [
    # Linux (Debian/Ubuntu)
    "/usr/share/fonts/truetype/dejavu",
    # Linux (Fedora/RHEL)
    "/usr/share/fonts/dejavu",
    # macOS (Homebrew)
    "/usr/local/share/fonts/dejavu",
    # macOS (MacPorts)
    "/opt/local/share/fonts/dejavu",
    # Windows (choco/scoop)
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),
]


def _find_font(filename):
    """Return the absolute path of *filename* in any known font directory."""
    for d in _FONT_DIRS:
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p
    return None


def _find_dejavu():
    """Return ``(regular_path, bold_path)`` for DejaVu Sans, or ``(None, None)``.

    Kept for backwards compatibility with callers that import this private
    helper directly.  New code should rely on :func:`_add_unicode_fonts`.
    """
    regular = _find_font("DejaVuSans.ttf")
    if not regular:
        return None, None
    bold = _find_font("DejaVuSans-Bold.ttf") or regular
    return regular, bold


def _add_unicode_fonts(pdf):
    """Register the DejaVu Sans family + monospace variant if available.

    Returns ``True`` when the regular DejaVu Sans face was registered.  The
    caller falls back to the built-in Helvetica / Courier fonts (Latin-1
    only) when the font file is missing on the host.
    """
    regular = _find_font("DejaVuSans.ttf")
    if not regular:
        return False
    bold = _find_font("DejaVuSans-Bold.ttf") or regular
    italic = _find_font("DejaVuSans-Oblique.ttf") or regular
    bold_italic = _find_font("DejaVuSans-BoldOblique.ttf") or bold
    mono = _find_font("DejaVuSansMono.ttf") or regular
    mono_bold = _find_font("DejaVuSansMono-Bold.ttf") or mono

    pdf.add_font("DejaVu", "", regular)
    pdf.add_font("DejaVu", "B", bold)
    pdf.add_font("DejaVu", "I", italic)
    pdf.add_font("DejaVu", "BI", bold_italic)
    pdf.add_font("DejaVuMono", "", mono)
    pdf.add_font("DejaVuMono", "B", mono_bold)
    return True


def _font_family(pdf):
    """Return the best available Unicode-capable sans-serif family name."""
    return "DejaVu" if getattr(pdf, "_has_unicode", False) else "Helvetica"


def _mono_family(pdf):
    """Return the best available monospace family name."""
    return "DejaVuMono" if getattr(pdf, "_has_unicode", False) else "Courier"


# Older callers only want a flattened plain-text version of a page, so the
# stripper below stays alongside the real parser used for PDF output.

def _strip_markdown(text):
    """Convert Markdown to a simplified plain-text representation.

    This is intentionally simple: it preserves paragraph structure, headings,
    and list items while stripping formatting characters that would render
    poorly in a basic PDF layout.  The styled renderer used by
    :func:`generate_page_pdf` parses the Markdown directly and does not call
    this function.
    """
    lines = []
    for line in text.splitlines():
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            lines.append("")
            lines.append(m.group(2).strip())
            lines.append("")
            continue
        if re.match(r'^[\-\*_]{3,}\s*$', line):
            lines.append("---")
            continue
        line = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', line)
        line = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', line)
        line = re.sub(r'`(.+?)`', r'\1', line)
        line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', line)
        line = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[Image: \1]', line)
        lines.append(line)
    return "\n".join(lines)



def _parse_inline(text):
    """Parse a Markdown inline string into a list of styled run dicts.

    Each run dict has the keys ``text``, ``bold``, ``italic``, ``code`` and
    ``link``.  The parser handles bold (``**``/``__``), italic (``*``/``_``),
    inline code, links and image placeholders (``![alt](url)`` becomes
    ``[Image: alt]``).  Anything else is emitted verbatim.
    """
    runs = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Image: replace with a literal placeholder; we never embed remote
        # bitmaps in the PDF.
        if ch == "!" and i + 1 < n and text[i + 1] == "[":
            m = re.match(r'!\[([^\]]*)\]\(([^)]+)\)', text[i:])
            if m:
                runs.append({"text": f"[Image: {m.group(1)}]"})
                i += m.end()
                continue
        # Link
        if ch == "[":
            m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', text[i:])
            if m:
                inner = _parse_inline(m.group(1))
                url = m.group(2)
                for r in inner:
                    r["link"] = url
                    runs.append(r)
                i += m.end()
                continue
        # Inline code (backtick-delimited; supports ``literal `tick` text``)
        if ch == "`":
            j = i + 1
            while j < n and text[j] == "`":
                j += 1
            tick = "`" * (j - i)
            close = text.find(tick, j)
            if close != -1:
                runs.append({"text": text[j:close], "code": True})
                i = close + len(tick)
                continue
        # Bold (**...** or __...__)
        if (ch in "*_") and (i + 1 < n) and text[i + 1] == ch:
            marker = ch * 2
            close = text.find(marker, i + 2)
            if close != -1:
                inner = _parse_inline(text[i + 2:close])
                for r in inner:
                    r["bold"] = True
                    runs.append(r)
                i = close + 2
                continue
        # Italic (*...* or _..._): single delimiter only; the bold case
        # was handled above.
        if ch in "*_":
            close = text.find(ch, i + 1)
            if close != -1 and close > i + 1:
                inner = _parse_inline(text[i + 1:close])
                for r in inner:
                    r["italic"] = True
                    runs.append(r)
                i = close + 1
                continue
        # Plain text: accumulate until the next markdown sigil
        j = i + 1
        while j < n and text[j] not in "*_`[!":
            j += 1
        runs.append({"text": text[i:j]})
        i = j

    for r in runs:
        r.setdefault("bold", False)
        r.setdefault("italic", False)
        r.setdefault("code", False)
        r.setdefault("link", None)
    return runs


# Second parsing stage: lines are grouped into block dicts here, and each
# block's text is handed to _parse_inline above only at render time.

_FENCE_RE = re.compile(r'^(```+|~~~+)\s*([^\s`~]*)\s*$')
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_HR_RE = re.compile(r'^\s*((?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})\s*$')
_LIST_RE = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+(.*)$')
_TABLE_SEP_RE = re.compile(
    r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$'
)


def _parse_blocks(text):
    """Parse Markdown source into a flat list of block dicts.

    Block types:
      * ``heading``: ``{level, text}``
      * ``paragraph``: ``{text}``
      * ``code``: ``{lang, lines}``
      * ``quote``: ``{lines}``
      * ``list``: ``{kind, items}`` where each item is
        ``{indent, marker, text, kind}``
      * ``table``: ``{header, aligns, rows}``
      * ``hr``: ``{}``
      * ``blank``: ``{}``
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        m = _FENCE_RE.match(stripped)
        if m:
            fence_char = m.group(1)[0]
            code_lines = []
            i += 1
            while i < n:
                cur = lines[i]
                if cur.strip().startswith(fence_char * 3):
                    i += 1
                    break
                code_lines.append(cur)
                i += 1
            blocks.append({"type": "code", "lang": m.group(2), "lines": code_lines})
            continue

        # ATX heading
        m = _HEADING_RE.match(stripped)
        if m:
            blocks.append({
                "type": "heading",
                "level": len(m.group(1)),
                "text": m.group(2),
            })
            i += 1
            continue

        # Horizontal rule
        if _HR_RE.match(stripped):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            blocks.append({"type": "quote", "lines": quote_lines})
            continue

        # List
        if _LIST_RE.match(line):
            list_block, i = _parse_list(lines, i)
            blocks.append(list_block)
            continue

        # Pipe table: header line followed by a separator row
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            table_block, i = _parse_table(lines, i)
            blocks.append(table_block)
            continue

        # Blank line
        if not stripped:
            blocks.append({"type": "blank"})
            i += 1
            continue

        # Paragraph: accumulate continuation lines until the next block break
        para_lines = [stripped]
        i += 1
        while i < n:
            ln = lines[i]
            sl = ln.strip()
            if not sl:
                break
            if (
                _HEADING_RE.match(sl)
                or _FENCE_RE.match(sl)
                or _HR_RE.match(sl)
                or sl.startswith(">")
                or _LIST_RE.match(ln)
                or ("|" in ln and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]))
            ):
                break
            para_lines.append(sl)
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})

    return blocks


def _parse_list(lines, start):
    """Parse a list starting at ``start`` and return ``(block, next_index)``.

    Nested lists are flattened: each item carries an ``indent`` level
    (in nesting steps) so the renderer can adjust indentation accordingly.
    """
    items = []
    n = len(lines)
    list_kind = None
    i = start
    while i < n:
        line = lines[i]
        if not line.strip():
            break
        m = _LIST_RE.match(line)
        if not m:
            # Continuation of the previous item if it is indented further
            if items and (line.startswith(" ") or line.startswith("\t")):
                items[-1]["text"] += " " + line.strip()
                i += 1
                continue
            break
        indent_str = m.group(1).expandtabs(4)
        marker = m.group(2)
        content = m.group(3)
        kind = "ordered" if re.match(r'^\d+[.)]$', marker) else "unordered"
        if list_kind is None:
            list_kind = kind
        items.append({
            "indent": len(indent_str) // 2,
            "marker": marker,
            "text": content,
            "kind": kind,
        })
        i += 1
    return {"type": "list", "kind": list_kind or "unordered", "items": items}, i


def _parse_table(lines, start):
    """Parse a pipe table starting at ``start`` and return ``(block, next_index)``."""
    header = [c.strip() for c in _split_table_row(lines[start])]
    aligns = []
    for c in _split_table_row(lines[start + 1]):
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            aligns.append("center")
        elif c.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")
    rows = []
    i = start + 2
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or "|" not in ln:
            break
        rows.append([c.strip() for c in _split_table_row(ln)])
        i += 1
    return {"type": "table", "header": header, "aligns": aligns, "rows": rows}, i


def _split_table_row(line):
    """Split a pipe-separated row, ignoring the optional outer pipes."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return line.split("|")


def _strip_inline_markers(text):
    """Best-effort strip of inline Markdown markers: used for headings."""
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'[Image: \1]', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', text)
    return text



class _WikiPDF(FPDF):
    """Styled FPDF subclass with cover, running header and footer."""

    def __init__(self, title, site_name, meta_line=""):
        """Configure margins, fonts and document metadata for a styled PDF."""
        super().__init__()
        self._doc_title = title
        self._site_name = site_name
        self._meta_line = meta_line
        self._has_unicode = _add_unicode_fonts(self)
        self.set_margins(20, 22, 20)
        self.set_auto_page_break(auto=True, margin=22)

    # header / footer

    def header(self):
        """Render the running header (suppressed on the cover page)."""
        if self.page_no() <= 1:
            return
        ff = _font_family(self)
        # Slim accent band at the top
        self.set_fill_color(*_C_BAND)
        self.rect(0, 0, self.w, 4, "F")
        self.set_y(10)
        self.set_font(ff, "B", 9)
        self.set_text_color(*_C_TITLE)
        title_width = self.w / 2 - self.l_margin
        self.cell(title_width, 5, self._truncate(self._doc_title, 60))
        self.set_font(ff, "", 9)
        self.set_text_color(*_C_MUTED)
        self.cell(title_width, 5, self._site_name, align="R")
        self.ln(6)
        self.set_draw_color(*_C_HR)
        self.set_line_width(0.2)
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(6)
        self.set_text_color(*_C_TEXT)

    def footer(self):
        """Render the page footer with site name and page numbers."""
        ff = _font_family(self)
        self.set_y(-15)
        self.set_draw_color(*_C_HR)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_y(-12)
        self.set_font(ff, "", 8)
        self.set_text_color(*_C_MUTED)
        half = self.w / 2 - self.l_margin
        self.cell(half, 5, self._site_name)
        self.cell(half, 5, f"Page {self.page_no()} / {{nb}}", align="R")
        self.set_text_color(0, 0, 0)

    # cover

    def render_cover(self):
        """Render the styled title section on the first page."""
        ff = _font_family(self)
        self.set_fill_color(*_C_BAND)
        self.rect(0, 0, self.w, 6, "F")
        self.set_y(22)
        self.set_x(self.l_margin)
        # Site name kicker
        self.set_font(ff, "B", 9)
        self.set_text_color(*_C_HEADING_ACCENT)
        kicker = (self._site_name or "").upper()
        self.cell(0, 5, kicker, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        # Title
        self.set_text_color(*_C_TITLE)
        self.set_font(ff, "B", 22)
        self.multi_cell(0, 9, self._doc_title)
        # Meta line
        if self._meta_line:
            self.ln(1)
            self.set_font(ff, "", 9)
            self.set_text_color(*_C_MUTED)
            self.multi_cell(0, 5, self._meta_line)
        # Separator
        self.ln(2)
        self.set_draw_color(*_C_HR)
        self.set_line_width(0.3)
        y = self.get_y() + 1
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(7)
        self.set_text_color(*_C_TEXT)

    # helpers

    @staticmethod
    def _truncate(text, length):
        """Return *text* truncated to *length* characters with a trailing ellipsis."""
        if len(text) <= length:
            return text
        return text[: length - 1].rstrip() + "\u2026"



class _MarkdownRenderer:
    """Render a list of parsed blocks onto a :class:`_WikiPDF` instance."""

    PARA_LINE_HEIGHT = 5.5
    PARA_AFTER = 2.5
    HEADING_SIZES = {1: 18, 2: 14.5, 3: 12.5, 4: 11.5, 5: 11, 6: 10.5}
    HEADING_BEFORE = {1: 4, 2: 4, 3: 3, 4: 2.5, 5: 2, 6: 2}
    HEADING_AFTER = {1: 2.5, 2: 2, 3: 1.5, 4: 1.2, 5: 1, 6: 1}

    def __init__(self, pdf):
        """Bind the renderer to *pdf* and cache its font families."""
        self.pdf = pdf
        self.ff = _font_family(pdf)
        self.mf = _mono_family(pdf)
        self.has_unicode = getattr(pdf, "_has_unicode", False)

    # public API

    def render(self, blocks):
        """Render the parsed Markdown ``blocks`` onto the PDF."""
        for block in blocks:
            t = block["type"]
            if t == "heading":
                self._render_heading(block)
            elif t == "paragraph":
                self._render_paragraph(block)
            elif t == "code":
                self._render_code(block)
            elif t == "quote":
                self._render_quote(block)
            elif t == "list":
                self._render_list(block)
            elif t == "table":
                self._render_table(block)
            elif t == "hr":
                self._render_hr()
            elif t == "blank":
                self.pdf.ln(1.5)

    # block renderers

    def _render_heading(self, block):
        """Render a Markdown heading block with level-aware styling."""
        pdf = self.pdf
        level = block["level"]
        size = self.HEADING_SIZES.get(level, 11)
        pdf.ln(self.HEADING_BEFORE.get(level, 2))
        if level <= 2:
            pdf.set_text_color(*_C_HEADING)
        else:
            pdf.set_text_color(*_C_TITLE)
        pdf.set_font(self.ff, "B", size)
        text = _strip_inline_markers(block["text"]).strip()
        pdf.multi_cell(0, size * 0.55, text)
        if level == 1:
            pdf.set_draw_color(*_C_HEADING_ACCENT)
            pdf.set_line_width(0.5)
            y = pdf.get_y() + 0.8
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(2.5)
        elif level == 2:
            pdf.set_draw_color(*_C_HR)
            pdf.set_line_width(0.2)
            y = pdf.get_y() + 0.8
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(2)
        else:
            pdf.ln(self.HEADING_AFTER.get(level, 1))
        pdf.set_text_color(*_C_TEXT)
        pdf.set_font(self.ff, "", 11)

    def _render_paragraph(self, block):
        """Render a Markdown paragraph block with inline formatting."""
        runs = _parse_inline(block["text"])
        self._draw_runs(runs)
        self.pdf.ln(self.PARA_AFTER)

    def _render_code(self, block):
        """Render a fenced or indented code block in a bordered, shaded box."""
        pdf = self.pdf
        pdf.ln(1.5)
        line_h = 5
        pdf.set_fill_color(*_C_CODE_BG)
        pdf.set_draw_color(*_C_CODE_BORDER)
        pdf.set_line_width(0.2)
        pdf.set_text_color(*_C_CODE_TEXT)
        pdf.set_font(self.mf, "", 9)
        # Top padding
        pdf.set_x(pdf.l_margin)
        pdf.cell(0, 2, "", border="LRT", fill=True, new_x="LMARGIN", new_y="NEXT")
        for raw in block["lines"]:
            # Substitute tabs with four spaces and strip trailing whitespace
            line = raw.replace("\t", "    ").rstrip()
            pdf.set_x(pdf.l_margin)
            # multi_cell handles wrapping for long lines
            pdf.multi_cell(
                0,
                line_h,
                line if line else " ",
                border="LR",
                fill=True,
                new_x="LMARGIN",
                new_y="NEXT",
            )
        # Bottom padding
        pdf.set_x(pdf.l_margin)
        pdf.cell(0, 2, "", border="LRB", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_C_TEXT)
        pdf.set_font(self.ff, "", 11)
        pdf.ln(2)

    def _render_quote(self, block):
        """Render a blockquote with an accent bar in the margin."""
        pdf = self.pdf
        pdf.ln(1)
        text = " ".join(line.strip() for line in block["lines"] if line.strip())
        if not text:
            return
        old_l = pdf.l_margin
        bar_x = pdf.l_margin
        pdf.set_left_margin(old_l + 6)
        pdf.set_x(old_l + 6)
        y_start = pdf.get_y()
        pdf.set_text_color(*_C_QUOTE_TEXT)
        pdf.set_font(self.ff, "I", 11)
        runs = _parse_inline(text)
        self._draw_runs(runs, italic_default=True, color=_C_QUOTE_TEXT)
        y_end = pdf.get_y()
        # Draw the bar last so it spans the whole rendered height
        pdf.set_fill_color(*_C_QUOTE_BAR)
        pdf.rect(bar_x, y_start, 1.5, max(y_end - y_start, 4), "F")
        pdf.set_left_margin(old_l)
        pdf.set_x(old_l)
        pdf.set_text_color(*_C_TEXT)
        pdf.set_font(self.ff, "", 11)
        pdf.ln(self.PARA_AFTER)

    def _render_list(self, block):
        """Render an ordered or unordered list with hanging-indent markers."""
        pdf = self.pdf
        pdf.ln(1)
        bullet = "\u2022" if self.has_unicode else "-"
        for item in block["items"]:
            indent = item.get("indent", 0)
            indent_amount = indent * 6
            x_marker = pdf.l_margin + indent_amount
            old_l = pdf.l_margin
            content_x = x_marker + 6
            pdf.set_left_margin(content_x)
            pdf.set_x(x_marker)
            pdf.set_text_color(*_C_HEADING_ACCENT)
            pdf.set_font(self.ff, "B", 11)
            if item["kind"] == "ordered":
                marker_text = item["marker"].rstrip(".)") + "."
                pdf.cell(6, self.PARA_LINE_HEIGHT, marker_text)
            else:
                pdf.cell(6, self.PARA_LINE_HEIGHT, bullet)
            pdf.set_text_color(*_C_TEXT)
            pdf.set_font(self.ff, "", 11)
            runs = _parse_inline(item["text"])
            self._draw_runs(runs)
            pdf.set_left_margin(old_l)
            pdf.set_x(old_l)
        pdf.ln(self.PARA_AFTER)

    def _render_table(self, block):
        """Render a Markdown table with shaded header and column alignment."""
        pdf = self.pdf
        pdf.ln(1)
        cols = max(len(block["header"]), max((len(r) for r in block["rows"]), default=0))
        if cols == 0:
            return
        avail = pdf.w - pdf.l_margin - pdf.r_margin
        col_w = avail / cols
        # Header
        pdf.set_fill_color(*_C_TABLE_HEADER_BG)
        pdf.set_draw_color(*_C_TABLE_BORDER)
        pdf.set_line_width(0.2)
        pdf.set_text_color(*_C_TITLE)
        pdf.set_font(self.ff, "B", 10)
        pdf.set_x(pdf.l_margin)
        for i in range(cols):
            cell = block["header"][i] if i < len(block["header"]) else ""
            cell = _strip_inline_markers(cell)
            align = self._table_align(block["aligns"], i, default="C")
            pdf.cell(col_w, 7, cell, border=1, align=align, fill=True)
        pdf.ln(7)
        # Body
        pdf.set_text_color(*_C_TEXT)
        pdf.set_font(self.ff, "", 10)
        for row in block["rows"]:
            pdf.set_x(pdf.l_margin)
            for i in range(cols):
                cell = row[i] if i < len(row) else ""
                cell = _strip_inline_markers(cell)
                align = self._table_align(block["aligns"], i, default="L")
                pdf.cell(col_w, 6.5, cell, border=1, align=align)
            pdf.ln(6.5)
        pdf.ln(self.PARA_AFTER)

    def _render_hr(self):
        """Render a horizontal-rule block as a centred thin line."""
        pdf = self.pdf
        pdf.ln(2)
        pdf.set_draw_color(*_C_HR)
        pdf.set_line_width(0.3)
        y = pdf.get_y() + 1.5
        pdf.line(pdf.l_margin + 30, y, pdf.w - pdf.r_margin - 30, y)
        pdf.ln(4)

    @staticmethod
    def _table_align(aligns, i, default="L"):
        """Translate the parsed alignment list into an FPDF ``align`` token."""
        if i >= len(aligns):
            return default
        return {"left": "L", "right": "R", "center": "C"}.get(aligns[i], default)

    # inline run rendering

    def _draw_runs(self, runs, italic_default=False, color=None):
        """Emit a sequence of styled runs onto the current line.

        The callers reset font / colour state afterwards. This method only
        cares about the runs themselves.
        """
        pdf = self.pdf
        body_color = color if color is not None else _C_TEXT
        line_h = self.PARA_LINE_HEIGHT
        for run in runs:
            text = run["text"]
            if not text:
                continue
            bold = run["bold"]
            italic = run["italic"] or italic_default
            code = run["code"]
            link = run["link"]
            if code:
                pdf.set_text_color(*_C_INLINE_CODE_TEXT)
                pdf.set_font(self.mf, "B" if bold else "", 9.5)
                pdf.write(line_h, text, link=link or "")
            elif link:
                pdf.set_text_color(*_C_LINK)
                style = ("B" if bold else "") + ("I" if italic else "")
                pdf.set_font(self.ff, style, 11)
                pdf.write(line_h, text, link=link)
            else:
                pdf.set_text_color(*body_color)
                style = ("B" if bold else "") + ("I" if italic else "")
                pdf.set_font(self.ff, style, 11)
                pdf.write(line_h, text)
        pdf.ln(line_h)
        pdf.set_text_color(*body_color)
        pdf.set_font(self.ff, "", 11)



def generate_page_pdf(title, markdown_content, site_name,
                      author=None, edited_at=None, is_history=False):
    """Generate a styled PDF from a wiki page's Markdown content.

    Args:
        title: Page or revision title.
        markdown_content: Raw Markdown source of the page.
        site_name: Name of the wiki site (used as the cover kicker and
            footer label).
        author: Optional editor / author username.
        edited_at: Optional human-readable edit timestamp.
        is_history: Whether this is a historical revision.

    Returns:
        A :class:`io.BytesIO` buffer containing the PDF data, seeked to 0.
    """
    meta_parts = []
    if author:
        meta_parts.append(f"Author: {author}")
    if edited_at:
        label = "Revision date" if is_history else "Last edited"
        meta_parts.append(f"{label}: {edited_at}")
    if is_history:
        meta_parts.append("(Historical revision)")
    meta_line = "  \u00b7  ".join(meta_parts) if meta_parts else ""

    pdf = _WikiPDF(title, site_name, meta_line)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.render_cover()

    blocks = _parse_blocks(markdown_content or "")
    _MarkdownRenderer(pdf).render(blocks)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf
