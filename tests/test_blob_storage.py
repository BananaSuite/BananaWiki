"""Tests for Phase 1 DB blob storage: dual-write, DB-first reads, migration round-trip."""

import base64
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Blob API unit tests
# ---------------------------------------------------------------------------

def test_store_and_retrieve_blob():
    import db
    content = b"Hello, blob storage!"
    blob_id = db.store_blob("test.txt", content, "text/plain")
    assert isinstance(blob_id, int)
    assert blob_id > 0
    retrieved = db.get_blob_content(blob_id)
    assert retrieved == content


def test_get_blob_metadata():
    import db
    content = b"metadata test"
    blob_id = db.store_blob("meta.bin", content, "application/octet-stream")
    meta = db.get_blob(blob_id)
    assert meta is not None
    assert meta["filename"] == "meta.bin"
    assert meta["file_size"] == len(content)
    assert meta["mime_type"] == "application/octet-stream"


def test_get_blob_content_missing_id():
    import db
    assert db.get_blob_content(None) is None
    assert db.get_blob_content(999999) is None


def test_delete_blob():
    import db
    blob_id = db.store_blob("todelete.bin", b"delete me", "application/octet-stream")
    assert db.get_blob_content(blob_id) == b"delete me"
    db.delete_blob(blob_id)
    assert db.get_blob_content(blob_id) is None


def test_delete_blob_none_id():
    import db
    result = db.delete_blob(None)
    assert result is False


def test_blob_sha256():
    import db
    import hashlib
    content = b"checksum test"
    blob_id = db.store_blob("check.bin", content, "text/plain")
    meta = db.get_blob(blob_id)
    expected = hashlib.sha256(content).hexdigest()
    assert meta["sha256"] == expected


def test_file_blobs_table_exists():
    import db
    conn = db.get_db()
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()
    assert "file_blobs" in tables


def test_blob_id_column_in_page_attachments():
    import db
    conn = db.get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(page_attachments)").fetchall()}
    finally:
        conn.close()
    assert "blob_id" in cols


def test_blob_id_column_in_chat_attachments():
    import db
    conn = db.get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_attachments)").fetchall()}
    finally:
        conn.close()
    assert "blob_id" in cols


def test_blob_id_column_in_group_attachments():
    import db
    conn = db.get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(group_attachments)").fetchall()}
    finally:
        conn.close()
    assert "blob_id" in cols


def test_blob_id_column_in_kanban_ticket_attachments():
    import db
    conn = db.get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_ticket_attachments)").fetchall()}
    finally:
        conn.close()
    assert "blob_id" in cols


def test_blob_id_column_in_custom_page_files():
    import db
    conn = db.get_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(custom_page_files)").fetchall()}
    finally:
        conn.close()
    assert "blob_id" in cols


# ---------------------------------------------------------------------------
# Page attachment dual-write + DB-first download
# ---------------------------------------------------------------------------

def test_page_attachment_dual_write(logged_in_admin, tmp_path, monkeypatch):
    """Uploading a page attachment should write to both disk and DB blob."""
    import db
    admin_id = db.get_user_by_username("admin")["id"]
    cat_id = db.create_category("TestCat")
    page_id = db.create_page("Test Page", "test-page", "", cat_id, admin_id)

    att_dir = str(tmp_path / "attachments")
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", att_dir)

    data = {
        "file": (io.BytesIO(b"attachment content"), "test.txt"),
    }
    resp = logged_in_admin.post(
        f"/api/page/{page_id}/attachments",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    rj = resp.get_json()
    assert "id" in rj

    attachment_id = rj["id"]
    att = db.get_page_attachment(attachment_id)
    assert att is not None
    # Phase 1: blob_id should be set
    assert att["blob_id"] is not None
    content = db.get_blob_content(att["blob_id"])
    assert content == b"attachment content"


def test_page_attachment_download_db_first(logged_in_admin, tmp_path, monkeypatch):
    """Downloading an attachment should succeed even if the disk file is gone (DB-first)."""
    import db
    admin_id = db.get_user_by_username("admin")["id"]
    cat_id = db.create_category("TestCat2")
    page_id = db.create_page("Test Page 2", "test-page-2", "", cat_id, admin_id)

    att_dir = str(tmp_path / "attachments2")
    os.makedirs(att_dir, exist_ok=True)
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", att_dir)

    data = {
        "file": (io.BytesIO(b"db-first content"), "dbfirst.txt"),
    }
    resp = logged_in_admin.post(
        f"/api/page/{page_id}/attachments",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    att_id = resp.get_json()["id"]

    # Remove the disk file to test DB-first fallback
    att = db.get_page_attachment(att_id)
    disk_path = os.path.join(att_dir, att["filename"])
    if os.path.isfile(disk_path):
        os.remove(disk_path)

    # Download should still work via DB blob
    slug = db.get_page(page_id)["slug"]
    resp = logged_in_admin.get(f"/page/{slug}/attachments/{att_id}/download")
    assert resp.status_code == 200
    assert resp.data == b"db-first content"


def test_page_attachment_delete_cleans_blob(logged_in_admin, tmp_path, monkeypatch):
    """Deleting a page attachment should also delete the DB blob."""
    import db
    admin_id = db.get_user_by_username("admin")["id"]
    cat_id = db.create_category("TestCat3")
    page_id = db.create_page("Test Page 3", "test-page-3", "", cat_id, admin_id)

    att_dir = str(tmp_path / "attachments3")
    os.makedirs(att_dir, exist_ok=True)
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", att_dir)

    data = {"file": (io.BytesIO(b"to be deleted"), "del.txt")}
    resp = logged_in_admin.post(
        f"/api/page/{page_id}/attachments",
        data=data,
        content_type="multipart/form-data",
    )
    att_id = resp.get_json()["id"]
    att = db.get_page_attachment(att_id)
    blob_id = att["blob_id"]
    assert blob_id is not None

    # Delete attachment
    del_resp = logged_in_admin.delete(f"/api/attachments/{att_id}")
    assert del_resp.status_code == 200

    # Blob should be gone
    assert db.get_blob_content(blob_id) is None


# ---------------------------------------------------------------------------
# Migration: file_blobs round-trip with base64
# ---------------------------------------------------------------------------

def test_migration_includes_file_blobs_table(admin_user):
    """Migration export should include the file_blobs table."""
    import db
    data = db.export_site_data()
    assert "file_blobs" in data


def test_migration_blob_content_base64_encoded(admin_user):
    """Binary blob content should be base64-encoded in migration JSON."""
    import db
    content = b"\x00\x01\x02\x03binary data"
    blob_id = db.store_blob("binary.bin", content, "application/octet-stream")
    data = db.export_site_data()
    blob_rows = data.get("file_blobs", [])
    blob_row = next((r for r in blob_rows if r["id"] == blob_id), None)
    assert blob_row is not None
    # content should be base64-encoded (not raw bytes)
    encoded = blob_row["content"]
    assert isinstance(encoded, dict)
    assert "__b64__" in encoded
    decoded = base64.b64decode(encoded["__b64__"])
    assert decoded == content


def test_migration_roundtrip_preserves_blobs(admin_user):
    """Export → import should preserve blob content."""
    import db
    content = b"roundtrip test content"
    blob_id = db.store_blob("rt.bin", content, "text/plain")

    data = db.export_site_data()
    # Clear and reimport
    db.import_site_data(data, "delete_all")

    # After reimport, blob should still be accessible
    retrieved = db.get_blob_content(blob_id)
    assert retrieved == content


def test_migration_json_serializable(admin_user):
    """export_site_data() result should be JSON-serializable even with blob data."""
    import db
    db.store_blob("json_test.bin", b"\xff\xfe\xfd binary", "application/octet-stream")
    data = db.export_site_data()
    # Should not raise
    serialized = json.dumps(data)
    assert "file_blobs" in serialized
