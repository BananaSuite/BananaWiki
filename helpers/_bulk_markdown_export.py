"""Content-only Markdown archive export for wiki pages.

This exporter is deliberately separate from the full site migration ZIP.  It
produces a portable Markdown tree plus page-facing assets, without users,
settings, history, chats, or other database records.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
import db
from helpers._bulk_markdown import _sanitize_segment


_TOP_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._()' -]+")


@dataclass
class BulkMarkdownExportResult:
    """Summary and payload for a Markdown archive export."""

    buffer: io.BytesIO
    filename: str
    page_count: int = 0
    upload_count: int = 0
    attachment_count: int = 0
    missing_assets: list[str] = field(default_factory=list)


def _yaml_escape(value) -> str:
    """Return *value* as a conservative double-quoted YAML scalar."""
    if value is None:
        return '""'
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe_path_segment(value, fallback: str) -> str:
    """Return a human-readable ZIP path segment."""
    segment = _sanitize_segment(str(value or ""))
    segment = segment.replace("/", " ").replace("\\", " ").strip()
    return segment or fallback


def _safe_filename(value, fallback: str) -> str:
    """Return a filename safe to place in a ZIP member path."""
    name = os.path.basename(str(value or "")).replace("\x00", "")
    name = _UNSAFE_FILENAME_RE.sub("_", name).strip(" .")
    return name or fallback


def _dedupe_path(path: str, used: set[str]) -> str:
    """Return a unique archive path by adding a numeric suffix when needed."""
    if path not in used:
        used.add(path)
        return path
    base, ext = os.path.splitext(path)
    counter = 2
    while True:
        candidate = f"{base}-{counter}{ext}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def _category_paths() -> dict[int, list[str]]:
    """Return category id -> sanitized ancestor path segments."""
    cats = {int(c["id"]): dict(c) for c in db.list_categories()}
    paths: dict[int, list[str]] = {}

    def walk(cat_id: int) -> list[str]:
        """Resolve one category id to its cached path segments."""
        if cat_id in paths:
            return paths[cat_id]
        segments: list[str] = []
        current = cat_id
        seen: set[int] = set()
        while current and current in cats and current not in seen:
            seen.add(current)
            cat = cats[current]
            segments.append(_safe_path_segment(cat["name"], f"category-{current}"))
            parent_id = cat.get("parent_id")
            current = int(parent_id) if parent_id else 0
        paths[cat_id] = list(reversed(segments))
        return paths[cat_id]

    for cid in cats:
        walk(cid)
    return paths


def _has_matching_top_h1(body: str, title: str) -> bool:
    """Return True when the body already starts with an H1 matching title."""
    stripped = (body or "").lstrip("\n")
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    match = _TOP_H1_RE.match(first_line)
    return bool(match and match.group(1).strip() == title)


def _markdown_document(page: dict, category_path: str, exported_at: str) -> str:
    """Build a Markdown file body that round-trips through bulk import."""
    frontmatter = (
        "---\n"
        f"title: {_yaml_escape(page.get('title'))}\n"
        f"slug: {_yaml_escape(page.get('slug'))}\n"
        f"category_path: {_yaml_escape(category_path)}\n"
        f"exported_at: {_yaml_escape(exported_at)}\n"
        "---\n\n"
    )
    body = page.get("content") or ""
    if _has_matching_top_h1(body, page.get("title") or ""):
        return frontmatter + body
    return frontmatter + f"# {page.get('title') or 'Untitled'}\n\n" + body


def _read_page_attachment(att) -> bytes | None:
    """Read a page attachment from blob storage, falling back to disk."""
    blob_id = att["blob_id"] if "blob_id" in att.keys() else None
    if blob_id:
        content = db.get_blob_content(blob_id)
        if content is not None:
            return content

    attach_root = os.path.abspath(config.ATTACHMENT_FOLDER)
    filepath = os.path.abspath(os.path.join(attach_root, att["filename"]))
    try:
        if os.path.commonpath([attach_root, filepath]) != attach_root:
            return None
    except ValueError:
        return None
    if not os.path.isfile(filepath):
        return None
    with open(filepath, "rb") as fh:
        return fh.read()


def _write_referenced_uploads(zf: zipfile.ZipFile, pages: list[dict], used: set[str]) -> tuple[int, list[str]]:
    """Write `/static/uploads/...` files referenced by exported page bodies."""
    filenames: set[str] = set()
    for page in pages:
        filenames.update(db._UPLOAD_REF_RE.findall(page.get("content") or ""))

    upload_root = os.path.abspath(config.UPLOAD_FOLDER)
    missing: list[str] = []
    written = 0
    for raw_name in sorted(filenames):
        name = os.path.basename(raw_name)
        if not name or name != raw_name or "\x00" in raw_name:
            missing.append(f"uploads/{raw_name}")
            continue
        filepath = os.path.abspath(os.path.join(upload_root, name))
        try:
            if os.path.commonpath([upload_root, filepath]) != upload_root:
                missing.append(f"uploads/{raw_name}")
                continue
        except ValueError:
            missing.append(f"uploads/{raw_name}")
            continue
        if not os.path.isfile(filepath):
            missing.append(f"uploads/{raw_name}")
            continue
        arcname = _dedupe_path(f"assets/uploads/{name}", used)
        zf.write(filepath, arcname)
        written += 1
    return written, missing


def _write_page_attachments(zf: zipfile.ZipFile, pages: list[dict], used: set[str]) -> tuple[int, list[str]]:
    """Write page attachments grouped by page slug."""
    missing: list[str] = []
    written = 0
    for page in pages:
        page_slug = _safe_filename(page.get("slug"), f"page-{page.get('id')}")
        for att in db.get_page_attachments(page["id"]):
            content = _read_page_attachment(att)
            display_name = _safe_filename(
                att["original_name"] if "original_name" in att.keys() else att["filename"],
                att["filename"],
            )
            if content is None:
                missing.append(f"page_attachments/{page_slug}/{display_name}")
                continue
            arcname = _dedupe_path(
                f"assets/page_attachments/{page_slug}/{display_name}",
                used,
            )
            zf.writestr(arcname, content)
            written += 1
    return written, missing


def build_bulk_markdown_export() -> BulkMarkdownExportResult:
    """Build a ZIP containing wiki pages as Markdown plus page-facing assets."""
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pages = [
        dict(p)
        for p in db.list_searchable_pages(include_deindexed=True)
        if not p.get("is_home")
    ]
    cat_paths = _category_paths()
    buf = io.BytesIO()
    used_paths: set[str] = set()
    missing: list[str] = []
    upload_count = 0
    attachment_count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for page in pages:
            segments = cat_paths.get(page.get("category_id") or 0, [])
            category_path = "/".join(segments)
            filename = _safe_filename(page.get("slug"), f"page-{page.get('id')}.md")
            if not filename.endswith(".md"):
                filename = f"{filename}.md"
            arcname = _dedupe_path("/".join([*segments, filename]) if segments else filename, used_paths)
            zf.writestr(arcname, _markdown_document(page, category_path, exported_at))

        upload_count, upload_missing = _write_referenced_uploads(zf, pages, used_paths)
        attachment_count, attachment_missing = _write_page_attachments(zf, pages, used_paths)
        missing.extend(upload_missing)
        missing.extend(attachment_missing)

        manifest = {
            "format": "bananawiki-markdown-export",
            "format_version": 1,
            "exported_at": exported_at,
            "page_count": len(pages),
            "upload_count": upload_count,
            "attachment_count": attachment_count,
            "missing_assets": missing,
            "notes": [
                "This is a content-only Markdown export, not a database backup.",
                "Markdown files can be imported through Admin -> Bulk Markdown Import.",
                "Assets are bundled for manual reuse and are not restored by the Markdown importer.",
            ],
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        zf.writestr(
            "assets/README.md",
            "# Assets\n\n"
            "Files in this directory are included for manual reuse. "
            "The Markdown importer creates pages and categories only; it does not restore assets.\n",
        )

    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return BulkMarkdownExportResult(
        buffer=buf,
        filename=f"markdown_export_{stamp}.zip",
        page_count=len(pages),
        upload_count=upload_count,
        attachment_count=attachment_count,
        missing_assets=missing,
    )
