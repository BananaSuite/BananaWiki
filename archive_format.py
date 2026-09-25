"""Unified BananaWiki archive format.

This module defines the canonical ZIP layout used by both:

* the hosting platform's per-instance archive (``hosting/admin/Download
  archive`` and ``hosting/admin/Import wiki ZIP``); and
* the standalone wiki's site migration (``/admin/migration/export`` and
  ``/admin/migration/import``).

A unified ZIP looks like::

    manifest.json              # see ``Manifest`` below
    bananawiki.db              # raw SQLite snapshot (consistent backup)
    site_export.json           # logical JSON dump (db.export_site_data())
    uploads/                   # flat asset directories at the archive root
    attachments/
    chat_attachments/
    kanban_attachments/
    custom_page_files/
    favicons/                  # only files named ``custom_*``
    plugins/                   # optional per-plugin data dirs

Older archives that this module also understands (for read compatibility):

* **Legacy hosting layout**: everything nested under a ``<slug>/`` directory
  (``<slug>/bananawiki.db``, ``<slug>/uploads/`` …) with no ``manifest.json``.
* **Legacy standalone layout**: ``site_export.json`` + ``assets/uploads/``
  etc., with no raw SQLite database and no ``manifest.json``.

Both legacy shapes are accepted by the new importers; the new exporters
always write the unified layout above.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Iterable, Optional

# Format constants

FORMAT_VERSION = 1

MANIFEST_FILENAME = "manifest.json"
RAW_DB_FILENAME = "bananawiki.db"
SITE_EXPORT_FILENAME = "site_export.json"

# Standardised asset directories at the archive root.  Each entry maps an
# archive directory name to a logical asset kind.  The same names appear
# in legacy archives under different prefixes (``<slug>/`` or ``assets/``);
# the importers handle that transparently.
ASSET_DIRS = (
    "uploads",
    "attachments",
    "chat_attachments",
    "kanban_attachments",
    "custom_page_files",
    "favicons",
    "plugins",
)

# Volatile per-instance runtime files that should never end up in an
# archive (they are regenerated automatically on next process start).
VOLATILE_FILENAMES = frozenset({
    "bananawiki.pid",
    "error.log",
    "access.log",
    "bananawiki.db-wal",
    "bananawiki.db-shm",
})

# Files that are unsafe to include in a portable archive (would leak the
# session-signing secret or allow Remote Code Execution on import).
SECRET_FILENAMES = frozenset({
    ".secret_key",
})

# Source tags written into ``manifest.json``.
SOURCE_HOSTING = "hosting"
SOURCE_STANDALONE = "standalone"


# Manifest helpers


def build_manifest(
    *,
    source: str,
    original_subdomain: Optional[str] = None,
    has_raw_db: bool = True,
    has_site_export_json: bool = True,
    bananawiki_version: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Return a manifest dict ready to be JSON-serialised."""
    if source not in (SOURCE_HOSTING, SOURCE_STANDALONE):
        raise ValueError(f"Unknown archive source: {source!r}")
    manifest = {
        "format_version": FORMAT_VERSION,
        "source": source,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "has_raw_db": bool(has_raw_db),
        "has_site_export_json": bool(has_site_export_json),
    }
    if original_subdomain:
        manifest["original_subdomain"] = original_subdomain
    if bananawiki_version:
        manifest["bananawiki_version"] = bananawiki_version
    if extra:
        # Allow callers to attach implementation-specific metadata without
        # forcing schema changes here.  Reserved keys are not allowed to
        # be overridden.
        reserved = set(manifest.keys())
        for k, v in extra.items():
            if k in reserved:
                continue
            manifest[k] = v
    return manifest


def serialise_manifest(manifest: dict) -> bytes:
    """JSON-serialise a manifest into UTF-8 bytes ready for writing."""
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def parse_manifest(raw: bytes) -> dict:
    """Parse a ``manifest.json`` body into a dict.  Raises ``ValueError`` on
    malformed input or an unsupported ``format_version``."""
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must be a JSON object")
    version = manifest.get("format_version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("manifest.json is missing a positive format_version")
    if version > FORMAT_VERSION:
        raise ValueError(
            f"manifest.json declares format_version={version} which is "
            f"newer than this BananaWiki understands (max {FORMAT_VERSION})."
        )
    return manifest


# Layout detection


def detect_layout(zip_namelist: Iterable[str]) -> dict:
    """Inspect a ZIP's namelist and return a dict describing its shape.

    The dict has the keys:

    * ``layout``: one of ``"unified"``, ``"legacy_hosting"``,
      ``"legacy_standalone"``, or ``"unknown"``.
    * ``root_prefix``: the prefix (with trailing ``/``) under which the
      payload lives, or ``""`` if it is already at the archive root.
    * ``has_manifest``, ``has_raw_db``, ``has_site_export_json``: booleans.
    """
    names = [n for n in zip_namelist if n and not n.endswith("/")]

    has_manifest = any(n == MANIFEST_FILENAME for n in names)
    has_raw_db_root = any(n == RAW_DB_FILENAME for n in names)
    has_site_export_root = any(n == SITE_EXPORT_FILENAME for n in names)
    has_legacy_assets = any(n.startswith("assets/") for n in names)

    # A genuine unified archive has ``manifest.json`` at the root.  We
    # also fall back to unified if there are no other signals (raw DB
    # or site_export.json at root with *no* legacy ``assets/`` paths).
    if has_manifest:
        return {
            "layout": "unified",
            "root_prefix": "",
            "has_manifest": True,
            "has_raw_db": has_raw_db_root,
            "has_site_export_json": has_site_export_root,
        }

    # Legacy standalone layout: a top-level ``site_export.json`` and/or
    # asset directories under ``assets/``: no manifest.
    if has_legacy_assets or (
        has_site_export_root and not has_raw_db_root
    ):
        return {
            "layout": "legacy_standalone",
            "root_prefix": "",
            "has_manifest": False,
            "has_raw_db": has_raw_db_root,
            "has_site_export_json": has_site_export_root,
        }

    # Legacy hosting layout: every member starts with the same single
    # top-level directory and that directory contains ``bananawiki.db``.
    top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(top_dirs) == 1:
        prefix = next(iter(top_dirs)) + "/"
        if f"{prefix}{RAW_DB_FILENAME}" in names:
            return {
                "layout": "legacy_hosting",
                "root_prefix": prefix,
                "has_manifest": False,
                "has_raw_db": True,
                "has_site_export_json": (
                    f"{prefix}{SITE_EXPORT_FILENAME}" in names
                ),
            }

    # No manifest but raw DB or JSON dump at root, with no other legacy
    # signals: accept as a unified-ish archive (raw-DB-only hosting
    # exports without manifest.json fall here).
    if has_raw_db_root or has_site_export_root:
        return {
            "layout": "unified",
            "root_prefix": "",
            "has_manifest": False,
            "has_raw_db": has_raw_db_root,
            "has_site_export_json": has_site_export_root,
        }

    return {
        "layout": "unknown",
        "root_prefix": "",
        "has_manifest": False,
        "has_raw_db": False,
        "has_site_export_json": False,
    }


def asset_archive_name_for_legacy(name: str, root_prefix: str) -> Optional[str]:
    """Rewrite a legacy archive member name to its unified equivalent.

    Returns ``None`` for members that should be ignored (volatile runtime
    files, secrets, members outside the known asset / data roots).  Both
    legacy hosting (``<slug>/uploads/foo.png``) and legacy standalone
    (``assets/uploads/foo.png``) get mapped to ``uploads/foo.png``.
    """
    if not name or name.endswith("/"):
        return None
    if root_prefix and name.startswith(root_prefix):
        name = name[len(root_prefix):]
    if not name:
        return None

    basename = os.path.basename(name)
    if basename in VOLATILE_FILENAMES or basename in SECRET_FILENAMES:
        return None

    # Legacy standalone archives prefix every asset path with ``assets/``.
    if name.startswith("assets/"):
        name = name[len("assets/"):]
        if not name:
            return None

    # Reject path traversal up front.
    norm = os.path.normpath(name).replace(os.sep, "/")
    if norm.startswith("..") or os.path.isabs(norm):
        return None
    return norm


# Convenience builder used by both exporters


def write_manifest_to_zip(
    zf: zipfile.ZipFile,
    manifest: dict,
) -> None:
    """Write ``manifest.json`` to *zf* at the archive root."""
    zf.writestr(MANIFEST_FILENAME, serialise_manifest(manifest))


def manifest_bytes(manifest: dict) -> io.BytesIO:
    """Return a ``BytesIO`` containing the serialised manifest."""
    return io.BytesIO(serialise_manifest(manifest))
