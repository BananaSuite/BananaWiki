"""Tests for the unified BananaWiki archive format.

Covers:

* ``archive_format`` module: manifest build/parse, layout detection,
  legacy-name rewriting.
* Hosting export → standalone import round-trip (via the standalone
  ``/admin/migration/import`` route).
* Legacy-format detection on import (legacy standalone with
  ``assets/<kind>/`` paths is accepted by the new importer).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile

import pytest

import archive_format


# ---------------------------------------------------------------------------
# Manifest build / parse
# ---------------------------------------------------------------------------


def test_build_manifest_round_trip():
    manifest = archive_format.build_manifest(
        source=archive_format.SOURCE_HOSTING,
        original_subdomain="wiki",
        bananawiki_version="1.2.3",
        extra={"hosting_instance_id": "abc123"},
    )
    assert manifest["format_version"] == archive_format.FORMAT_VERSION
    assert manifest["source"] == archive_format.SOURCE_HOSTING
    assert manifest["original_subdomain"] == "wiki"
    assert manifest["bananawiki_version"] == "1.2.3"
    assert manifest["hosting_instance_id"] == "abc123"

    raw = archive_format.serialise_manifest(manifest)
    parsed = archive_format.parse_manifest(raw)
    assert parsed == manifest


def test_build_manifest_rejects_unknown_source():
    with pytest.raises(ValueError):
        archive_format.build_manifest(source="something-else")


def test_parse_manifest_rejects_future_version():
    raw = json.dumps({"format_version": archive_format.FORMAT_VERSION + 1}).encode()
    with pytest.raises(ValueError):
        archive_format.parse_manifest(raw)


def test_parse_manifest_rejects_invalid_json():
    with pytest.raises(ValueError):
        archive_format.parse_manifest(b"not json")


def test_parse_manifest_rejects_non_object():
    raw = json.dumps([1, 2, 3]).encode()
    with pytest.raises(ValueError):
        archive_format.parse_manifest(raw)


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------


def test_detect_layout_unified():
    names = [
        "manifest.json",
        "bananawiki.db",
        "site_export.json",
        "uploads/photo.png",
        "attachments/page-file.txt",
    ]
    layout = archive_format.detect_layout(names)
    assert layout["layout"] == "unified"
    assert layout["root_prefix"] == ""
    assert layout["has_manifest"] is True
    assert layout["has_raw_db"] is True
    assert layout["has_site_export_json"] is True


def test_detect_layout_legacy_hosting():
    names = [
        "myinst/bananawiki.db",
        "myinst/uploads/foo.png",
        "myinst/attachments/bar.txt",
    ]
    layout = archive_format.detect_layout(names)
    assert layout["layout"] == "legacy_hosting"
    assert layout["root_prefix"] == "myinst/"
    assert layout["has_raw_db"] is True
    assert layout["has_site_export_json"] is False


def test_detect_layout_legacy_standalone():
    names = [
        "site_export.json",
        "assets/uploads/photo.png",
        "assets/attachments/page-file.txt",
    ]
    layout = archive_format.detect_layout(names)
    assert layout["layout"] == "legacy_standalone"
    assert layout["root_prefix"] == ""
    assert layout["has_manifest"] is False
    assert layout["has_site_export_json"] is True


def test_detect_layout_unknown():
    layout = archive_format.detect_layout(["random.txt", "other.bin"])
    assert layout["layout"] == "unknown"


# ---------------------------------------------------------------------------
# asset_archive_name_for_legacy
# ---------------------------------------------------------------------------


def test_asset_archive_name_strips_legacy_hosting_slug():
    assert (
        archive_format.asset_archive_name_for_legacy(
            "myinst/uploads/photo.png", "myinst/"
        )
        == "uploads/photo.png"
    )


def test_asset_archive_name_strips_legacy_standalone_assets_prefix():
    assert (
        archive_format.asset_archive_name_for_legacy(
            "assets/uploads/photo.png", ""
        )
        == "uploads/photo.png"
    )


def test_asset_archive_name_drops_volatile_files():
    for volatile in archive_format.VOLATILE_FILENAMES:
        assert (
            archive_format.asset_archive_name_for_legacy(
                f"myinst/{volatile}", "myinst/"
            )
            is None
        )


def test_asset_archive_name_drops_secret_files():
    assert (
        archive_format.asset_archive_name_for_legacy(
            "myinst/.secret_key", "myinst/"
        )
        is None
    )


def test_asset_archive_name_rejects_path_traversal():
    assert (
        archive_format.asset_archive_name_for_legacy(
            "myinst/../etc/passwd", "myinst/"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Standalone export → unified-format inspection
# ---------------------------------------------------------------------------


def test_standalone_export_emits_unified_archive(logged_in_admin):
    """``/admin/migration/export`` produces a ZIP in the unified format."""
    resp = logged_in_admin.post("/admin/migration/export", data={"password": "admin123"})
    assert resp.status_code == 200

    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        assert archive_format.MANIFEST_FILENAME in names
        assert archive_format.SITE_EXPORT_FILENAME in names
        # The standalone exporter snapshots the live DB and ships it.
        assert archive_format.RAW_DB_FILENAME in names

        manifest = archive_format.parse_manifest(
            zf.read(archive_format.MANIFEST_FILENAME)
        )
        assert manifest["source"] == archive_format.SOURCE_STANDALONE
        assert manifest["has_site_export_json"] is True
        assert manifest["has_raw_db"] is True


# ---------------------------------------------------------------------------
# Standalone import: legacy + hosting + unified compatibility
# ---------------------------------------------------------------------------


def _build_unified_archive(*, data_payload, manifest_source):
    """Build an in-memory unified ZIP containing only manifest +
    ``site_export.json``.

    Used to verify the importer accepts the new format end-to-end.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = archive_format.build_manifest(
            source=manifest_source,
            has_raw_db=False,
            has_site_export_json=True,
        )
        archive_format.write_manifest_to_zip(zf, manifest)
        zf.writestr(
            archive_format.SITE_EXPORT_FILENAME,
            json.dumps(data_payload).encode("utf-8"),
        )
    buf.seek(0)
    return buf.read()


def _build_legacy_standalone_archive(*, data_payload, asset_files):
    """Build an in-memory legacy standalone ZIP (no manifest, ``assets/`` paths)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "site_export.json",
            json.dumps(data_payload).encode("utf-8"),
        )
        for archive_path, body in asset_files.items():
            zf.writestr(archive_path, body)
    buf.seek(0)
    return buf.read()


def _build_legacy_hosting_archive(*, slug, raw_db_bytes, asset_files):
    """Build an in-memory legacy hosting ZIP (everything under ``<slug>/``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/bananawiki.db", raw_db_bytes)
        for archive_path, body in asset_files.items():
            zf.writestr(f"{slug}/{archive_path}", body)
    buf.seek(0)
    return buf.read()


def test_standalone_import_accepts_unified_archive(logged_in_admin, admin_user):
    """Importing a unified ZIP (with manifest.json) succeeds."""
    import db
    db.create_page("Imported Author Page", "imported-author-page", "Body", user_id=admin_user)
    data = db.export_site_data()
    zip_bytes = _build_unified_archive(
        data_payload=data,
        manifest_source=archive_format.SOURCE_HOSTING,
    )

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            "password": "admin123",
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(zip_bytes), "unified.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data
    page = db.get_page_by_slug("imported-author-page")
    assert str(page["last_edited_by"]) == str(db.SYSTEM_USER_ID)
    history = db.get_page_history(page["id"])
    assert history[-1]["username"] == "the system"


def test_standalone_import_accepts_legacy_standalone_archive(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """Importing a legacy standalone ZIP (``assets/<kind>/`` paths) still works."""
    import db
    import config

    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(tmp_path / "attachments"))

    data = db.export_site_data()
    asset_files = {
        "assets/uploads/legacy-image.png": b"legacy-bytes",
        "assets/attachments/legacy-file.txt": b"legacy-attachment",
    }
    zip_bytes = _build_legacy_standalone_archive(
        data_payload=data,
        asset_files=asset_files,
    )

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            "password": "admin123",
            "import_mode": "override",
            "import_file": (io.BytesIO(zip_bytes), "legacy-standalone.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data

    # Legacy ``assets/uploads/`` should land at the flat unified destination.
    assert os.path.isfile(
        os.path.join(config.UPLOAD_FOLDER, "legacy-image.png")
    )
    assert os.path.isfile(
        os.path.join(config.ATTACHMENT_FOLDER, "legacy-file.txt")
    )


def test_standalone_import_accepts_legacy_hosting_archive(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """A legacy hosting ZIP (nested under ``<slug>/``) is also accepted.

    The importer derives a JSON dump from the raw ``bananawiki.db`` on
    the fly and strips the ``<slug>/`` prefix from asset paths.
    """
    import db
    import config

    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(tmp_path / "attachments"))

    # Snapshot the active wiki's DB to bytes so we can ship a raw
    # ``bananawiki.db`` inside the legacy-hosting archive.  Standalone
    # tests use the ``isolated_db`` fixture so this is the test DB.
    src_conn = sqlite3.connect(config.DATABASE_PATH)
    try:
        snap_buf = io.BytesIO()

        class _MemSink:
            def __init__(self):
                self.parts = []

            def write(self, b):
                self.parts.append(b)

            def flush(self):
                pass

        # ``sqlite3.Connection.backup`` requires a sqlite3 destination.
        dest = sqlite3.connect(":memory:")
        try:
            src_conn.backup(dest)
            for line in dest.iterdump():
                snap_buf.write((line + "\n").encode("utf-8"))
        finally:
            dest.close()
    finally:
        src_conn.close()

    # Build a tiny on-disk sqlite from the dump so we can read it as bytes.
    import tempfile
    fd, tmp_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        rebuilt = sqlite3.connect(tmp_db)
        rebuilt.executescript(snap_buf.getvalue().decode("utf-8"))
        rebuilt.close()
        with open(tmp_db, "rb") as fh:
            raw_db_bytes = fh.read()
    finally:
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    asset_files = {
        "uploads/host-photo.png": b"host-bytes",
        "attachments/host-doc.txt": b"host-attachment",
    }
    zip_bytes = _build_legacy_hosting_archive(
        slug="originslug",
        raw_db_bytes=raw_db_bytes,
        asset_files=asset_files,
    )

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            "password": "admin123",
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(zip_bytes), "legacy-hosting.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # The legacy hosting nesting must not propagate into the wiki's data
    # directory: assets land in the flat unified destination.
    assert os.path.isfile(
        os.path.join(config.UPLOAD_FOLDER, "host-photo.png")
    )
    assert os.path.isfile(
        os.path.join(config.ATTACHMENT_FOLDER, "host-doc.txt")
    )
    # And the ``originslug/`` prefix never leaks to disk.
    assert not os.path.exists(
        os.path.join(config.UPLOAD_FOLDER, "originslug")
    )


def test_standalone_import_rejects_future_manifest(logged_in_admin, admin_user):
    """A manifest with an unknown ``format_version`` aborts the import."""
    import db
    data = db.export_site_data()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            archive_format.MANIFEST_FILENAME,
            json.dumps({
                "format_version": archive_format.FORMAT_VERSION + 99,
                "source": "hosting",
            }).encode("utf-8"),
        )
        zf.writestr(
            archive_format.SITE_EXPORT_FILENAME,
            json.dumps(data).encode("utf-8"),
        )
    buf.seek(0)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            "password": "admin123",
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(buf.read()), "future.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'<span class="flash-message">Site data imported successfully' not in resp.data
