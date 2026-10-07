"""Export and import of canvases (``.canvas.json`` and ``.canvas.zip``, 1.4 compatible).

An export is ``{"title", "description", "version", "data"}``. When nodes show
images from this wiki's upload folder, the export is a zip holding
``canvas.json`` plus those files under ``assets/``.

An import creates a new canvas owned by the importing user. From ``assets/``
only images that a node points at are restored; each goes through
:func:`storage.save` (image check, metadata stripping, a new random name,
size and quota limits) and the node URLs are rewritten to the new names. A
refused import removes every file it already stored.
"""

from __future__ import annotations

import io
import json
import zipfile
import zlib
from collections.abc import Callable
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.json import loads as safe_json_loads
from ... import storage
from . import model

MAX_JSON_BYTES = 20 * 1024 * 1024
_CHUNK = 64 * 1024


class CanvasImportError(ValueError):
    """The file cannot be imported; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def export_payload(layout: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    return {"title": layout["title"], "description": layout["description"] or "", "version": layout["version"],
            "data": doc}


def build_export(layout: dict[str, Any], doc: dict[str, Any], *, plain: bool) -> tuple[bytes, str, str]:
    """``(body, mimetype, filename)``."""
    text = json.dumps(export_payload(layout, doc), ensure_ascii=False, indent=2).encode("utf-8")
    names = sorted(model.upload_names(doc))
    if plain or not names:
        return text, "application/json", f"{layout['slug']}.canvas.json"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("canvas.json", text)
        for name in names:
            path = storage.resolve("uploads", name)
            if path is not None:
                bundle.write(path, arcname=f"assets/{name}")
    return buffer.getvalue(), "application/zip", f"{layout['slug']}.canvas.zip"


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
    doc = model.clean_document(data)
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
        with zipfile.ZipFile(stream) as bundle:
            return _read_bundle(bundle)
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError,
            RuntimeError) as error:
        if isinstance(error, CanvasImportError | model.DocumentError):
            raise
        raise CanvasImportError("canvas.import.invalid") from error


def _read_bundle(bundle: zipfile.ZipFile) -> tuple[str, str, dict[str, Any], list[str]]:
    json_member, assets = None, {}
    for info in bundle.infolist():
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
