"""Bulk Markdown export and import (``/admin/bulk-markdown``).

Export writes every page as ``<category folders>/<slug>.md`` with front
matter, plus the images the pages embed (``assets/uploads/``), the page
attachments (``assets/page_attachments/<slug>/``) and a ``manifest.json``.
The archive is built in an anonymous temporary file, never in memory.

Import accepts ``.md`` files and ZIP archives of them. Folders become
categories (found case-insensitively or created), the title comes from the
front matter, the first ``# heading`` or the file name. Pages are written
through the pages service, attributed to the system (``author_id=None``)
unless the administrator chooses otherwise. Archives are checked before
anything is read: entry count, sizes, compression ratio, unsafe paths.
Assets in an archive are not imported.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO, Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql
from ... import storage
from ...db import db
from ..pages import categories
from ..pages import service as pages
from . import markdown_files as mdf

MARKDOWN_EXTENSIONS = (".md", ".markdown")
MAX_ZIP_ENTRIES = 20_000
MAX_FILES = 5_000
MAX_FILE_BYTES = 4 * pages.MAX_CONTENT  # a 1M-character page in UTF-8
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RATIO = 200
MAX_DEPTH = 10
MODES = ("skip", "update", "copy")
_UPLOAD_REF = re.compile(r"/static/uploads/([0-9A-Za-z_.-]+)")
_ORDER_PREFIX = re.compile(r"^\s*\d+\s*[-_.)]\s*")
_UNSAFE_SEGMENT = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


class ImportRefused(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def _work_file() -> IO[bytes]:
    folder = current_app.config["BW"].folders.exports
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return tempfile.TemporaryFile(dir=folder)


def _segment(name: str | None, fallback: str) -> str:
    """A file or folder name that is safe on every platform."""
    cleaned = _UNSAFE_SEGMENT.sub("_", str(name or "")).strip(" .")
    return cleaned[:120] or fallback


class _Names:
    """Hands out archive paths, adding ``-2``, ``-3``… to repeated ones."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def __call__(self, path: str) -> str:
        base, dot, ext = path.rpartition(".")
        if not dot or "/" in ext:
            base, ext = path, ""
        candidate, n = path, 2
        while candidate.casefold() in self.used:
            candidate = f"{base}-{n}.{ext}" if ext else f"{base}-{n}"
            n += 1
        self.used.add(candidate.casefold())
        return candidate


# ── Export ────────────────────────────────────────────────────────────────────


@dataclass
class ExportSummary:
    pages: int = 0
    uploads: int = 0
    attachments: int = 0
    missing: list[str] = field(default_factory=list)


def _category_names() -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """``(folders, names)``: per category id, safe folder segments and real names from the top."""
    rows = {row["id"]: row for row in categories.all_categories()}
    folders: dict[int, list[str]] = {}
    names: dict[int, list[str]] = {}
    for category_id in rows:
        chain: list[dict[str, Any]] = []
        current, seen = rows.get(category_id), set()
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(current)
            current = rows.get(current["parent_id"]) if current["parent_id"] else None
        chain.reverse()
        folders[category_id] = [_segment(c["name"], f"category-{c['id']}") for c in chain]
        names[category_id] = [c["name"].replace("/", "∕") for c in chain]
    return folders, names


def export_archive() -> tuple[IO[bytes], ExportSummary]:
    """Every page (except those pending deletion) as a Markdown archive in a temporary file."""
    folders, names = _category_names()
    summary = ExportSummary()
    exported_at = now_sql()
    archive = _work_file()
    uploads: set[str] = set()
    slugs: dict[int, str] = {}
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            unique = _Names()
            cursor = db.execute(
                "SELECT id, title, slug, category_id, is_home, content FROM pages WHERE pending_deletion = 0 "
                "ORDER BY category_id IS NOT NULL, category_id, sort_order, id"
            )
            for page in cursor:
                folder = folders.get(page["category_id"], [])
                meta = {"title": page["title"], "slug": page["slug"],
                        "category_path": "/".join(names.get(page["category_id"], [])), "exported_at": exported_at}
                path = unique("/".join([*folder, _segment(page["slug"], f"page-{page['id']}") + ".md"]))
                zf.writestr(path, mdf.document(meta, page["content"] or "", heading=page["title"]))
                uploads.update(_UPLOAD_REF.findall(page["content"] or ""))
                slugs[page["id"]] = page["slug"]
                summary.pages += 1
            _write_uploads(zf, sorted(uploads), unique, summary)
            _write_attachments(zf, slugs, unique, summary)
            zf.writestr("manifest.json", json.dumps({
                "format": "bananawiki-markdown-export", "format_version": 1, "exported_at": exported_at,
                "page_count": summary.pages, "upload_count": summary.uploads,
                "attachment_count": summary.attachments, "missing_assets": summary.missing,
            }, indent=2, sort_keys=True, ensure_ascii=False))
            zf.writestr("assets/README.md", "# Assets\n\nImages and attachments of the exported pages, for "
                        "manual reuse. The Markdown import creates pages and categories only.\n")
        archive.seek(0)
    except BaseException:
        archive.close()
        raise
    return archive, summary


def _write_uploads(zf: zipfile.ZipFile, names: list[str], unique: _Names, summary: ExportSummary) -> None:
    for name in names:
        path = storage.resolve("uploads", name)
        if path is None:
            summary.missing.append(f"uploads/{name}")
            continue
        zf.write(path, unique(f"assets/uploads/{name}"))
        summary.uploads += 1


def _write_attachments(zf: zipfile.ZipFile, slugs: dict[int, str], unique: _Names, summary: ExportSummary) -> None:
    rows = db.all("SELECT page_id, filename, original_name, blob_id FROM page_attachments ORDER BY page_id, id")
    for row in rows:
        slug = slugs.get(row["page_id"])
        if slug is None:
            continue
        name = _segment(row["original_name"], row["filename"])
        folder = _segment(slug, f"page-{row['page_id']}")
        path = storage.resolve("attachments", row["filename"], blob_id=row["blob_id"])
        if path is None:
            summary.missing.append(f"page_attachments/{folder}/{name}")
            continue
        zf.write(path, unique(f"assets/page_attachments/{folder}/{name}"))
        summary.attachments += 1


# ── Reading uploads ───────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, translation key)
    categories: int = 0
    problems: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (translation key, values)


def _is_markdown(name: str) -> bool:
    return name.lower().endswith(MARKDOWN_EXTENSIONS)


def _safe_parts(raw_path: str) -> list[str] | None:
    """Path segments of an archive member, or None when the path is unsafe."""
    path = raw_path.replace("\\", "/")
    if path.startswith("/") or "\x00" in path or re.match(r"^[A-Za-z]:", path):
        return None
    parts = [part for part in PurePosixPath(path).parts if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return parts


def _hidden(parts: list[str]) -> bool:
    """Editor and OS metadata (``.obsidian/``, ``__MACOSX/``, dot files) is skipped silently."""
    return any(part.startswith(".") or part == "__MACOSX" for part in parts)


def read_uploads(uploads: list[FileStorage], result: ImportResult) -> list[tuple[list[str], str]]:
    """Markdown files from the uploaded ``.md`` files and ZIP archives: ``[(path parts, text)]``."""
    files: list[tuple[list[str], str]] = []
    for upload in uploads:
        name = os.path.basename((upload.filename or "").replace("\\", "/"))
        if name.lower().endswith(".zip"):
            try:
                files.extend(_read_zip(upload.stream, name, result))
            except ImportRefused as refused:
                result.problems.append((refused.key, {"name": name, **refused.values}))
        elif _is_markdown(name):
            raw = upload.stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                result.problems.append(("page_export.import.file_too_large", {"name": name}))
            else:
                files.append(([name], mdf.decode(raw)))
        elif name:
            result.problems.append(("page_export.import.unsupported", {"name": name}))
        if len(files) > MAX_FILES:
            raise ImportRefused("page_export.import.too_many_files", limit=MAX_FILES)
    return files


def _check_archive(members: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    if len(members) > MAX_ZIP_ENTRIES:
        raise ImportRefused("page_export.import.too_many_entries", limit=MAX_ZIP_ENTRIES)
    wanted = [m for m in members if not m.is_dir() and _is_markdown(m.filename)]
    if len(wanted) > MAX_FILES:
        raise ImportRefused("page_export.import.too_many_files", limit=MAX_FILES)
    if sum(m.file_size for m in wanted) > MAX_TOTAL_BYTES:
        raise ImportRefused("page_export.import.too_large_unpacked")
    for member in wanted:
        if member.file_size > 1024 * 1024 and member.file_size > MAX_RATIO * max(member.compress_size, 1):
            raise ImportRefused("page_export.import.suspicious")
    return wanted


def _read_zip(stream: IO[bytes], archive_name: str, result: ImportResult) -> list[tuple[list[str], str]]:
    try:
        archive = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ImportRefused("page_export.import.bad_zip") from None
    files: list[tuple[list[str], str]] = []
    with archive:
        for member in _check_archive(archive.infolist()):
            parts = _safe_parts(member.filename)
            if parts is None or stat.S_ISLNK(member.external_attr >> 16):
                result.problems.append(("page_export.import.unsafe_path", {"name": member.filename}))
                continue
            if _hidden(parts):
                continue
            if member.file_size > MAX_FILE_BYTES:
                result.problems.append(("page_export.import.file_too_large", {"name": member.filename}))
                continue
            try:
                with archive.open(member) as source:
                    raw = source.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
                result.problems.append(("page_export.import.unreadable", {"name": member.filename}))
                continue
            if len(raw) > MAX_FILE_BYTES:
                result.problems.append(("page_export.import.file_too_large", {"name": member.filename}))
                continue
            files.append((parts, mdf.decode(raw)))
    return files


# ── Creating pages ────────────────────────────────────────────────────────────


def _folder_name(segment: str) -> str:
    """A category name from a folder name ("01 - Getting_started" -> "Getting started")."""
    name = _ORDER_PREFIX.sub("", segment).replace("_", " ")
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", name).split())[:categories.MAX_NAME]


class _CategoryResolver:
    """Finds categories by name under a parent (case-insensitively), creating missing ones."""

    def __init__(self, actor_id: str | None, result: ImportResult):
        self.actor_id = actor_id
        self.result = result
        self.known = {(row["parent_id"], row["name"].casefold()): row["id"] for row in categories.all_categories()}

    def resolve(self, names: list[str]) -> int | None:
        parent: int | None = None
        for name in names[:MAX_DEPTH]:
            if not name:
                continue
            key = (parent, name.casefold())
            if key not in self.known:
                created = categories.create(name, parent, actor_id=self.actor_id)
                self.known[key] = created["id"]
                self.result.categories += 1
            parent = self.known[key]
        return parent


def _title_for(parts: list[str], parsed: mdf.MarkdownFile) -> str:
    title = parsed.meta.get("title") or mdf.first_heading(parsed.body)
    if not title:
        stem = parts[-1].rsplit(".", 1)[0]
        title = _folder_name(stem) or stem
    return " ".join(title.split())[:pages.MAX_TITLE]


def _category_names_for(parts: list[str], parsed: mdf.MarkdownFile) -> list[str]:
    """The front matter's category path (exact names, as exported), else the folders."""
    if "category_path" in parsed.meta or (len(parts) == 1 and "category" in parsed.meta):
        path = parsed.meta.get("category_path", parsed.meta.get("category", ""))
        return [" ".join(name.replace("∕", "/").split())[:categories.MAX_NAME] for name in path.split("/")]
    return [_folder_name(part) for part in parts[:-1]]


def import_files(files: list[tuple[list[str], str]], *, author_id: str | None, actor_id: str | None,
                 mode: str = "skip", result: ImportResult | None = None) -> ImportResult:
    """Create (or, in ``update`` mode, update) one page per Markdown file."""
    result = result or ImportResult()
    resolver = _CategoryResolver(actor_id, result)
    seen: set[tuple[int | None, str]] = set()
    for parts, text in files:
        label = "/".join(parts)
        parsed = mdf.parse(text)
        title = _title_for(parts, parsed)
        body = mdf.strip_heading(parsed.body, title)
        try:
            category_id = resolver.resolve(_category_names_for(parts, parsed))
            key = (category_id, title.casefold())
            if key in seen:
                result.skipped.append((label, "page_export.import.skip.duplicate"))
                continue
            seen.add(key)
            slug = pages.slugify(parsed.meta.get("slug") or title)
            existing = pages.get_by_slug(slug)
            if existing is not None and mode != "copy":
                _update_existing(existing, title, body, label, author_id, mode, result)
                continue
            page = pages.create(title, body, category_id=category_id, author_id=author_id,
                                edit_message="Imported from Markdown", slug=slug)
            result.created.append(page["slug"])
        except (pages.PageError, categories.CategoryError) as error:
            result.problems.append(("page_export.import.page_failed",
                                    {"name": label, "reason_key": error.key, **error.values}))
    return result


def _update_existing(page: dict[str, Any], title: str, body: str, label: str, author_id: str | None,
                     mode: str, result: ImportResult) -> None:
    if mode == "skip":
        result.skipped.append((label, "page_export.import.skip.exists"))
    elif page.get("pending_deletion"):
        result.skipped.append((label, "page_export.import.skip.pending_deletion"))
    else:
        before = page["revision"]
        updated = pages.update(page, author_id=author_id, title=title, content=body,
                               edit_message="Updated from Markdown import")
        if updated["revision"] != before:
            result.updated.append(updated["slug"])
        else:
            result.skipped.append((label, "page_export.import.skip.unchanged"))
