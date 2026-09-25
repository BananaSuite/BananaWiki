"""Bulk markdown import: single .md, multiple .md, or .zip uploads.

The importer accepts a collection of ``(relative_path, content)`` pairs and
creates wiki pages.  Directory segments of the relative path become a
hierarchy of categories: existing categories are reused (case-insensitive
match within the same parent) and new ones are created when needed.

This module performs *no* I/O on its own. Callers are expected to read
files / unpack archives and feed pre-validated bytes/text into
:func:`import_markdown_bundle`.

Security notes
--------------
* Paths are aggressively sanitised: components ``..``, ``.``, absolute
  segments, and any non-printable characters are rejected.  Only ``.md``
  files are imported; everything else in a ZIP is silently ignored.
* Decompressed-size and member-count limits are enforced by the caller
  (see :mod:`config` MAX_IMPORT_* constants). This helper only handles
  already-decoded text.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field

import db
from helpers._text import slugify
from helpers._tts import suppress_tts_auto_generation


_MAX_MD_BYTES = 1_000_000  # 1 MB per page, matches _MAX_PAGE_CONTENT_LENGTH
_MAX_CATEGORY_NAME_LENGTH = 100
_MAX_PAGE_TITLE_LENGTH = 200
_MAX_CATEGORY_DEPTH = 10
_ALLOWED_EXTS = (".md", ".markdown")

# Strip leading order prefixes such as "01 - " or "02_" from
# category / page names: they are common in exported documentation
# trees and aren't meaningful as part of the visible label.
_ORDER_PREFIX_RE = re.compile(r"^\s*\d+\s*[-_.)\s]+")
# Allow letters, digits, spaces, common punctuation; collapse the rest.
_PRINTABLE_RE = re.compile(r"[^\w\s\-.&()',]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
# Frontmatter (YAML between two `---` lines) at the top of a file.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n.*?\n---\s*\n", re.DOTALL
)
# Pull title from the first non-empty ATX heading line.
_TITLE_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class BulkImportResult:
    """Outcome summary of a bulk import."""

    created_pages: list[str] = field(default_factory=list)
    skipped_pages: list[tuple[str, str]] = field(default_factory=list)
    created_categories: list[str] = field(default_factory=list)
    reused_categories: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_created(self) -> int:
        """Number of pages successfully imported in this run."""
        return len(self.created_pages)

    @property
    def total_skipped(self) -> int:
        """Number of pages skipped (e.g. duplicate slugs, validation errors)."""
        return len(self.skipped_pages)


def _sanitize_segment(segment: str) -> str:
    """Return a human-readable category name from a raw path segment."""
    if not segment:
        return ""
    s = _ORDER_PREFIX_RE.sub("", segment)
    s = s.replace("_", " ").replace("-", " ")
    s = _PRINTABLE_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s[:_MAX_CATEGORY_NAME_LENGTH]


def _normalise_relpath(raw_path: str) -> list[str] | None:
    """Split *raw_path* into safe POSIX-style segments, or return ``None`` if unsafe."""
    if not raw_path:
        return []
    # Normalise separators and drop empty parts.
    raw = raw_path.replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or p.startswith("/") or "\x00" in p:
            return None
    return parts


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block, if present."""
    return _FRONTMATTER_RE.sub("", text, count=1)


def _extract_title(body: str, fallback: str) -> str:
    """Return the first H1 heading from *body*, or a sanitised *fallback*."""
    match = _TITLE_RE.search(body)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate[:_MAX_PAGE_TITLE_LENGTH]
    cleaned = _sanitize_segment(fallback) or "Untitled"
    return cleaned[:_MAX_PAGE_TITLE_LENGTH]


def _strip_first_h1(body: str, title: str) -> str:
    """Remove the H1 heading that matches *title* (if it sits at the top of *body*)."""
    match = _TITLE_RE.search(body)
    if not match:
        return body
    heading_title = match.group(1).strip()
    if heading_title[:_MAX_PAGE_TITLE_LENGTH] != title:
        return body
    # Only strip when the heading is at the very top (after at most blank lines).
    prefix = body[: match.start()]
    if prefix.strip():
        return body
    return body[match.end():].lstrip("\n")


def _find_or_create_category(
    name: str,
    parent_id: int | None,
    cache: dict[tuple[int | None, str], int],
    result: BulkImportResult,
) -> int | None:
    """Return the id of the category named *name* under *parent_id*.

    Looks up an existing match case-insensitively, otherwise creates a new
    row and records the action on *result*.
    """
    key = (parent_id, name.casefold())
    if key in cache:
        return cache[key]
    for cat in db.list_categories():
        if cat["parent_id"] == parent_id and cat["name"].casefold() == name.casefold():
            cache[key] = cat["id"]
            result.reused_categories.append(cat["name"])
            return cat["id"]
    new_id = db.create_category(name, parent_id)
    cache[key] = new_id
    result.created_categories.append(name)
    return new_id


def _resolve_category_chain(
    segments: list[str],
    cache: dict[tuple[int | None, str], int],
    result: BulkImportResult,
) -> int | None:
    """Walk *segments*, creating/finding categories until the deepest one is returned."""
    parent_id: int | None = None
    for seg in segments[:_MAX_CATEGORY_DEPTH]:
        name = _sanitize_segment(seg)
        if not name:
            continue
        parent_id = _find_or_create_category(name, parent_id, cache, result)
    return parent_id


def _unique_slug(title: str) -> str | None:
    """Return a slug not yet present in the pages table, or ``None`` if exhausted."""
    base = slugify(title) or "page"
    candidate = base
    counter = 1
    while db.get_page_by_slug(candidate):
        counter += 1
        if counter > 1000:
            return None
        candidate = f"{base}-{counter}"
    return candidate


def import_markdown_bundle(
    files: list[tuple[str, str]],
    user_id: str,
    *,
    skip_existing: bool = True,
) -> BulkImportResult:
    """Import a bundle of ``(rel_path, markdown_text)`` pairs as wiki pages.

    Parameters
    ----------
    files:
        List of ``(relative_path, content)`` tuples.  ``relative_path`` may
        use either ``/`` or ``\\`` separators; the filename (without
        extension) is used as a fallback title when the markdown body has
        no H1 heading.
    user_id:
        The user id recorded as the creator of every imported page, or
        ``db.SYSTEM_USER_ID`` to attribute the import to the system.
    skip_existing:
        If ``True`` (default), pages with a slug that matches an existing
        page are skipped.  When ``False`` the slug is disambiguated with
        a numeric suffix.

    Returns
    -------
    BulkImportResult
        Summary of pages created, skipped, and categories touched.
    """
    result = BulkImportResult()
    category_cache: dict[tuple[int | None, str], int] = {}
    seen_paths: set[tuple[int | None, str]] = set()

    with suppress_tts_auto_generation():
        for raw_path, raw_body in files:
            path_str = (raw_path or "").strip()
            if not path_str:
                continue
            norm = _normalise_relpath(path_str)
            if norm is None:
                result.errors.append(f"Unsafe path skipped: {raw_path}")
                continue
            if not norm:
                continue
            filename = norm[-1]
            stem, ext = os.path.splitext(filename)
            if ext.lower() not in _ALLOWED_EXTS:
                # Silently ignore non-markdown entries (images, READMEs etc.)
                continue
            body = raw_body or ""
            if len(body.encode("utf-8")) > _MAX_MD_BYTES:
                result.errors.append(
                    f"{raw_path}: file exceeds the 1 MB page-content limit"
                )
                continue

            cleaned_body = _strip_frontmatter(body).lstrip("\n")
            title = _extract_title(cleaned_body, stem)
            if not title:
                result.errors.append(f"{raw_path}: could not determine a title")
                continue
            cleaned_body = _strip_first_h1(cleaned_body, title)

            directory = norm[:-1]
            category_id = _resolve_category_chain(directory, category_cache, result)

            cat_key = (category_id, title.casefold())
            if cat_key in seen_paths:
                result.skipped_pages.append((raw_path, "duplicate within upload"))
                continue
            seen_paths.add(cat_key)

            base_slug = slugify(title) or "page"
            existing_slug = db.get_page_by_slug(base_slug)
            if existing_slug and skip_existing:
                result.skipped_pages.append((raw_path, f"page '{title}' already exists"))
                continue

            slug = _unique_slug(title)
            if not slug:
                result.errors.append(f"{raw_path}: could not allocate a unique slug")
                continue
            try:
                db.create_page(title, slug, cleaned_body, category_id, user_id)
            except Exception as exc:  # pragma: no cover (defensive)
                result.errors.append(f"{raw_path}: {exc}")
                continue
            result.created_pages.append(slug)

    return result


def extract_markdown_files_from_zip(
    raw_bytes: bytes,
    *,
    max_uncompressed: int,
    max_member: int,
    max_entries: int,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Read a ZIP archive and return ``(files, errors)``.

    Only ``.md`` / ``.markdown`` files are returned.  Members that exceed
    size limits or fail to decode as UTF-8 are skipped and reported in
    ``errors``.
    """
    files: list[tuple[str, str]] = []
    errors: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile:
        return files, ["Uploaded file is not a valid ZIP archive."]

    with zf:
        infolist = zf.infolist()
        if len(infolist) > max_entries:
            errors.append(
                f"ZIP contains {len(infolist)} entries (limit: {max_entries})."
            )
            return files, errors

        total_uncompressed = 0
        for member in infolist:
            if member.is_dir():
                continue
            name = member.filename
            ext = os.path.splitext(name)[1].lower()
            if ext not in _ALLOWED_EXTS:
                continue
            if member.file_size > max_member:
                errors.append(f"{name}: file exceeds the per-file size limit.")
                continue
            total_uncompressed += member.file_size
            if total_uncompressed > max_uncompressed:
                errors.append("Archive exceeds the total uncompressed size limit.")
                break
            try:
                raw = zf.read(member)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                errors.append(f"{name}: could not read ({exc})")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Fall back to latin-1 as a permissive last resort so
                # imports from Windows-encoded vaults don't silently drop
                # entire pages.
                text = raw.decode("latin-1", errors="replace")
            files.append((name, text))
    return files, errors
