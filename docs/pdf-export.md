# PDF Export

> **Plugin dependency:** requires the `page_history` plugin to be enabled (gates the
> `page.export_pdf` permission).

## Overview

BananaWiki can export wiki pages and individual history revisions as PDF files.
The feature uses the fpdf2 library, a pure-Python PDF generator with no
external system dependencies.

## Permission

The `page.export_pdf` permission controls PDF export.  By default this
permission is disabled for both editors and regular users.  An admin must
explicitly grant it through **Admin → User Permissions** or by assigning a
custom role that includes the permission.

Because the permission is gated by the `page_history` plugin, disabling that
plugin automatically revokes `page.export_pdf` for all users regardless of
their individual permission settings.

## Routes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/page/<slug>/export-pdf` | Export the current version of a page |
| `GET` | `/page/<slug>/history/<entry_id>/export-pdf` | Export a specific history revision |

Both routes are rate-limited and require the user to be logged in and to hold
the `page.export_pdf` permission.

## PDF Format

Each exported PDF contains:

| Section | Content |
|---------|---------|
| **Cover** | Site name kicker, page title, author/edited metadata, accent band |
| **Body** | Styled rendering of the page Markdown |
| **Header** *(pages 2+)* | Page title (left) and site name (right) with thin accent band |
| **Footer** | Site name (left) and `Page X / Y` (right), separated by a hairline |

History exports include an additional *(Historical revision)* label in the
cover metadata line.

### Styled Markdown Rendering

The exporter parses the page Markdown into a small block-level AST and emits
typographic drawing calls against `fpdf2`.  It gives the following constructs
dedicated styling:

- **Headings** (`#`, `######`): six-level hierarchy with size and colour
  cues.  H1 gets a thick accent underline; H2 gets a hairline underline.
- **Inline formatting**: `**bold**`, `*italic*`, `_italic_`, `` `code` ``
  and `[label](url)` clickable links.  Inline code is rendered in a
  monospace face with an accent colour.
- **Fenced code blocks** (` ``` ` / `~~~`): monospace text on a tinted
  background with a thin border.  Tabs are expanded to four spaces.
- **Blockquotes** (`>`): italic muted text indented behind a vertical
  accent bar.
- **Lists**: both bullet (`-`, `*`, `+`) and ordered (`1.`, `2)`) lists
  with accent-coloured markers.  Nested items are indented one step per
  nesting level.
- **Pipe tables** (`| header |`): coloured header row, hairline borders
  and column alignment driven by the separator row (`:---`, `:---:`,
  `---:`).
- **Horizontal rules** (`---`, `***`, `___`): short centred line.
- **Images** (`![alt](url)`): replaced with a `[Image: alt]` placeholder;
  remote bitmaps are never embedded.

When the host has DejaVu Sans installed (Linux distros bundle it as
`fonts-dejavu-core`), the body is rendered with full Unicode support and
DejaVu Sans Mono is used for code.  When the font file is missing the
exporter falls back to the built-in Helvetica + Courier faces (Latin-1
only).

> **Limitations:** nested italic-inside-bold and complex inline HTML are
> rendered as plain text; fenced code blocks have no syntax highlighting;
> images are not embedded.

## Configuration

No additional configuration is required.  The fpdf2 library is included in
`requirements.txt`.  The only prerequisite is enabling the `page_history`
plugin and granting the `page.export_pdf` permission to the desired users.
