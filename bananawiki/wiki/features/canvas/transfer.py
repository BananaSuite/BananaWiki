"""Export and import of canvases (``.canvas.json`` and ``.canvas.zip``, 1.4 compatible).

An export is ``{"title", "description", "version", "data"}``. When nodes show
images from this wiki's upload folder, the export is a zip holding
``canvas.json`` plus those files under ``assets/``, written to a temporary
file (never built in memory) with already compressed formats stored as they
are. A canvas showing more than :data:`EXPORT_MAX_ASSETS` files or
:data:`EXPORT_MAX_ASSET_BYTES` of them is exported with ``canvas.json`` and a
``README.txt`` saying why the files are missing; its links still point to
this wiki.

An import creates a new canvas owned by the importing user. From ``assets/``
only images that a node points at are restored; each goes through
:func:`storage.save` (image check, metadata stripping, a new random name,
size and quota limits) and the node URLs are rewritten to the new names. A
refused import removes every file it already stored.

Nothing is read beyond what a valid export can hold: ``canvas.json`` (or a
plain export) at most :data:`MAX_JSON_BYTES`, a zip listing at most
:data:`MAX_MEMBERS` members (checked from its end record, before the listing
is loaded), images within the upload limit counted as they are
decompressed, and no more nodes or edges than a canvas may have, checked
before any of them is cleaned.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.json import loads as safe_json_loads
from ... import storage
from ...i18n import t
from . import model

#: The largest stored canvas plus room for the indentation and page titles of its export (an export that
#: would not fit is written without indentation).
MAX_JSON_BYTES = model.MAX_DOCUMENT_BYTES + 2 * 1024 * 1024
#: ``canvas.json`` and one image per node.
MAX_MEMBERS = model.MAX_NODES + 1
#: Bytes of zip listing per member (fixed fields, name and extra fields) accepted before the listing is read.
_LISTING_BYTES_PER_MEMBER = 1024
_CHUNK = 64 * 1024
#: Files an export zip holds at most (one per node, as an import accepts) and their total size.
EXPORT_MAX_ASSETS = model.MAX_NODES
EXPORT_MAX_ASSET_BYTES = 512 * 1024 * 1024
#: Formats that are compressed already: stored in the zip as they are.
_COMPRESSED_FORMATS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "avif", "heic", "mp3", "mp4", "m4a",
                                 "webm", "ogg", "zip", "gz", "pdf"})


class CanvasImportError(ValueError):
    """The file cannot be imported; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def export_payload(layout: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    return {"title": layout["title"], "description": layout["description"] or "", "version": layout["version"],
            "data": doc}


def export_json(layout: dict[str, Any], doc: dict[str, Any]) -> bytes:
    """``canvas.json``: indented, unless that would make it too large to import again."""
    payload = export_payload(layout, doc)
    text = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if len(text) > MAX_JSON_BYTES:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return text


def build_export(layout: dict[str, Any], doc: dict[str, Any], *, plain: bool) -> tuple[IO[bytes], str, str]:
    """``(file, mimetype, filename)``; the caller sends the file, which closes it."""
    text = export_json(layout, doc)
    names = sorted(model.upload_names(doc))
    if plain or not names:
        return io.BytesIO(text), "application/json", f"{layout['slug']}.canvas.json"
    assets = _export_assets(names)
    total = sum(size for _name, _path, size in assets)
    archive = tempfile.TemporaryFile(dir=_work_dir())  # noqa: SIM115 - handed to send_file, which closes it
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("canvas.json", text)
            if len(assets) > EXPORT_MAX_ASSETS or total > EXPORT_MAX_ASSET_BYTES:
                bundle.writestr("README.txt", t(
                    "canvas.export.assets_left_out", count=len(assets), size_mb=max(1, total // (1024 * 1024)),
                    limit=EXPORT_MAX_ASSETS, limit_mb=EXPORT_MAX_ASSET_BYTES // (1024 * 1024)) + "\n")
            else:
                for name, path, _size in assets:
                    stored = storage.extension(name) in _COMPRESSED_FORMATS
                    bundle.write(path, arcname=f"assets/{name}",
                                 compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        archive.seek(0)
    except BaseException:
        archive.close()
        raise
    return archive, "application/zip", f"{layout['slug']}.canvas.zip"


def _export_assets(names: list[str]) -> list[tuple[str, Path, int]]:
    """``(name, path, size)`` of the upload-folder files that still exist."""
    assets = []
    for name in names:
        path = storage.resolve("uploads", name)
        if path is None:
            continue
        try:
            assets.append((name, path, path.stat().st_size))
        except OSError:
            continue
    return assets


def _work_dir() -> str:
    folder = current_app.config["BW"].folders.exports
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


def _limit_bytes() -> int:
    return storage.max_upload_bytes(current_app.config["BW"].max_attachment_size)


def _read_member(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Read a member while counting the decompressed bytes (zip headers can lie)."""
    parts, size = [], 0
    with bundle.open(info) as source:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise CanvasImportError("canvas.import.too_large", limit_mb=max(1, limit // (1024 * 1024)))
            parts.append(chunk)
    return b"".join(parts)


def _parse(payload: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CanvasImportError("canvas.import.invalid")
    title = payload.get("title") if isinstance(payload.get("title"), str) else ""
    description = payload.get("description") if isinstance(payload.get("description"), str) else ""
    data = payload.get("data")
    if not isinstance(data, dict) or "nodes" not in data or "edges" not in data:
        data = model.EMPTY_DOCUMENT
    doc = model.clean_document(data, strict=True)
    for node in doc["nodes"]:
        if node["type"] == "wiki_page":
            model.drop_page_details(node)  # page titles in exports are not stored
    model.serialize(doc)  # enforce the size limits before anything is stored
    return " ".join(title.split())[:200] or "Imported canvas", description.strip()[:2000], doc


def read_import(upload: FileStorage | None) -> tuple[str, str, dict[str, Any], list[str]]:
    """Parse an uploaded export; return title, description, document and stored file names."""
    if upload is None or not upload.filename:
        raise CanvasImportError("canvas.import.no_file")
    stream = upload.stream
    head = stream.read(4)
    stream.seek(0)
    try:
        if head != b"PK\x03\x04":
            raw = stream.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise CanvasImportError("canvas.import.too_large", limit_mb=MAX_JSON_BYTES // (1024 * 1024))
            return (*_parse(safe_json_loads(raw.decode("utf-8"))), [])
        if _listing_too_large(stream):
            raise CanvasImportError("canvas.import.too_many_files", limit=MAX_MEMBERS)
        with zipfile.ZipFile(stream) as bundle:
            return _read_bundle(bundle)
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError,
            RuntimeError) as error:
        if isinstance(error, CanvasImportError | model.DocumentError):
            raise
        raise CanvasImportError("canvas.import.invalid") from error


def _listing_too_large(stream: IO[bytes]) -> bool:
    """Whether the zip's end record announces more members (or listing bytes) than an export has.

    :class:`zipfile.ZipFile` loads the whole listing when it opens an archive,
    so a small upload listing millions of empty members is refused first.
    (The member count is checked again once the archive is open.)
    """
    # The record ZipFile itself reads first, through private names a future Python may lack.
    end_record, entries, size = (getattr(zipfile, name, None) for name in ("_EndRecData", "_ECD_ENTRIES_TOTAL",
                                                                            "_ECD_SIZE"))
    if end_record is None or entries is None or size is None:
        return False
    end = end_record(stream)
    stream.seek(0)
    if not end:
        return False  # not a readable zip: ZipFile reports it
    return end[entries] > MAX_MEMBERS or end[size] > MAX_MEMBERS * _LISTING_BYTES_PER_MEMBER


def _read_bundle(bundle: zipfile.ZipFile) -> tuple[str, str, dict[str, Any], list[str]]:
    json_member, assets = None, {}
    members = bundle.infolist()
    if len(members) > MAX_MEMBERS:
        raise CanvasImportError("canvas.import.too_many_files", limit=MAX_MEMBERS)
    for info in members:
        name = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if name.startswith("/") or ".." in name.split("/"):
            raise CanvasImportError("canvas.import.invalid")
        if name == "canvas.json":
            json_member = info
        elif name.startswith("assets/") and "/" not in name[7:]:
            assets[name[7:]] = info
    if json_member is None:
        raise CanvasImportError("canvas.import.invalid")
    title, description, doc = _parse(safe_json_loads(_read_member(bundle, json_member, MAX_JSON_BYTES)))
    wanted = {name: info for name, info in assets.items() if name in model.upload_names(doc)}
    limit = _limit_bytes()
    stored: dict[str, str] = {}
    total = 0
    try:
        for name, info in sorted(wanted.items()):
            data = _read_member(bundle, info, limit - total)
            total += len(data)
            saved = storage.save(FileStorage(io.BytesIO(data), filename=name), "uploads", allowed=None,
                                 max_bytes=limit, images_only=True)
            stored[name] = saved.filename
    except storage.UploadError as error:
        discard(list(stored.values()))
        raise CanvasImportError(error.key, **error.values) from error
    except BaseException:
        discard(list(stored.values()))
        raise
    model.rename_uploads(doc, stored)
    return title, description, doc, list(stored.values())


def discard(filenames: list[str]) -> None:
    for name in filenames:
        storage.delete("uploads", name)


def _markdown_line(text: str) -> str:
    return " ".join(text.split())


def outline_markdown(layout: dict[str, Any], items: list[dict[str, Any]],
                     label: Callable[[dict[str, Any]], str]) -> str:
    """The text outline of a canvas as a Markdown document; *label* names an element."""
    by_id = {item["id"]: item for item in items}
    lines = [f"# {_markdown_line(layout['title'])}", ""]
    if layout.get("description"):
        lines += [layout["description"].strip(), ""]
    for item in items:
        lines.append(f"- **{_markdown_line(label(item))}**")
        for paragraph in item["text"].splitlines():
            if paragraph.strip():
                lines.append(f"  {paragraph.rstrip()}")
        if item["url"]:
            lines.append(f"  <{item['url']}>")
        for arrow, key in (("→", "outgoing"), ("←", "incoming")):
            for link in item[key]:
                target = by_id[link["id"]]
                suffix = f" ({_markdown_line(link['label'])})" if link["label"] else ""
                lines.append(f"  - {'↔' if link['both'] else arrow} {_markdown_line(label(target))}{suffix}")
    return "\n".join(lines).rstrip() + "\n"
