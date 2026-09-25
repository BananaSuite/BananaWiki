"""
Tests for the site migration (export / import) feature.
"""

import io
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(tmp_path / "attachments"))
    monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", str(tmp_path / "chat_attachments"))
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import app
    import routes.admin_migration as admin_mod
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(config, "FAVICON_UPLOAD_FOLDER", str(tmp_path / "favicons"))
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# Full-site export and import ask for the admin's password again.
_CONFIRM = {"password": "admin123"}


# ---------------------------------------------------------------------------
# db.export_site_data
# ---------------------------------------------------------------------------

def test_export_site_data_structure(admin_user):
    """export_site_data() returns a dict with _meta and all expected tables."""
    import db
    data = db.export_site_data()
    assert "_meta" in data
    assert data["_meta"]["version"] == 1
    assert "exported_at" in data["_meta"]
    for table in ("users", "invite_codes", "categories", "pages",
                  "page_history", "drafts", "announcements",
                  "username_history", "site_settings"):
        assert table in data, f"Missing table: {table}"


def test_export_site_data_includes_all_current_tables(admin_user):
    """The migration export should include every user-data application table.

    Tables in ``_EXPORT_EXCLUDED_TABLES`` (rate-limit buckets, leader
    election leases, regenerable TTS cache) are intentionally omitted
    because they hold only ephemeral / regenerable state and would
    bloat site_export.json without contributing user data.
    """
    import db
    import db._migration as migration_mod
    conn = db.get_db()
    try:
        all_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()

    expected_exported = all_tables - migration_mod._EXPORT_EXCLUDED_TABLES

    data = db.export_site_data()
    exported_tables = set(data) - {"_meta"}

    # Every user-data table must be exported.
    missing = expected_exported - exported_tables
    assert not missing, (
        f"User-data tables missing from export: {sorted(missing)}"
    )
    # No excluded table should leak into the export.
    leaked = exported_tables & migration_mod._EXPORT_EXCLUDED_TABLES
    assert not leaked, (
        f"Excluded tables leaked into export: {sorted(leaked)}"
    )


def test_export_contains_user(admin_user):
    """Exported data includes the admin user."""
    import db
    data = db.export_site_data()
    usernames = [u["username"] for u in data["users"]]
    assert "admin" in usernames


def test_export_contains_home_page():
    """Exported data includes the home page that init_db() creates."""
    import db
    data = db.export_site_data()
    slugs = [p["slug"] for p in data["pages"]]
    assert "home" in slugs


def test_export_site_data_closes_conn_on_error(monkeypatch):
    """export_site_data() closes the DB connection even when an exception occurs."""
    import db
    from contextlib import contextmanager

    closed = []
    original_get_db_context = db._migration.get_db_context

    @contextmanager
    def _tracked_context():
        """Context manager that records whether the connection is cleaned up."""
        with original_get_db_context() as real_conn:

            class _Wrapper:
                """Wrapper that raises on SELECT to simulate an error."""

                def execute(self, sql, *a, **kw):
                    """Delegate execute, raising on SELECT to simulate an error."""
                    if sql.startswith("SELECT *"):
                        raise RuntimeError("simulated read failure")
                    return real_conn.execute(sql, *a, **kw)

                def __getattr__(self, name):
                    """Forward attribute access to the real connection."""
                    return getattr(real_conn, name)

            try:
                yield _Wrapper()
            finally:
                closed.append(True)

    monkeypatch.setattr(db._migration, "get_db_context", _tracked_context)

    with pytest.raises(RuntimeError, match="simulated read failure"):
        db.export_site_data()

    assert closed, "connection cleanup was never called after the exception"


# ---------------------------------------------------------------------------
# db.import_site_data: delete_all mode
# ---------------------------------------------------------------------------

def test_import_delete_all_replaces_users(admin_user):
    """delete_all import removes existing users and inserts exported ones."""
    import db
    from werkzeug.security import generate_password_hash

    # Export current state (has 'admin')
    data = db.export_site_data()

    # Add a new user that should be gone after the import
    db.create_user("ghost", generate_password_hash("pw"), role="user")

    db.import_site_data(data, "delete_all")

    usernames = [u["username"] for u in db.list_users()]
    assert "admin" in usernames
    assert "ghost" not in usernames


def test_import_delete_all_keeps_home_page(admin_user):
    """After a delete_all import the home page from the export is present."""
    import db
    data = db.export_site_data()
    db.import_site_data(data, "delete_all")
    assert db.get_home_page() is not None


# ---------------------------------------------------------------------------
# db.import_site_data: override mode
# ---------------------------------------------------------------------------

def test_import_override_keeps_existing_user(admin_user):
    """override mode keeps users not present in the export."""
    import db
    from werkzeug.security import generate_password_hash

    data = db.export_site_data()
    db.create_user("extra", generate_password_hash("pw"), role="user")

    db.import_site_data(data, "override")

    usernames = [u["username"] for u in db.list_users()]
    assert "admin" in usernames
    assert "extra" in usernames


# ---------------------------------------------------------------------------
# db.import_site_data: keep mode
# ---------------------------------------------------------------------------

def test_import_keep_preserves_existing_data(admin_user):
    """keep mode leaves existing records untouched."""
    import db
    from werkzeug.security import generate_password_hash

    data = db.export_site_data()

    # Modify admin's role in export data; keep mode should ignore the conflict
    for u in data["users"]:
        if u["username"] == "admin":
            u["role"] = "user"

    db.import_site_data(data, "keep")

    admin = db.get_user_by_username("admin")
    assert admin["role"] == "admin"  # original value preserved


# ---------------------------------------------------------------------------
# db.import_site_data: validation errors
# ---------------------------------------------------------------------------

def test_import_invalid_mode_raises():
    import db
    data = db.export_site_data()
    with pytest.raises(ValueError, match="Unknown import mode"):
        db.import_site_data(data, "bad_mode")


def test_import_wrong_version_raises():
    import db
    data = db.export_site_data()
    data["_meta"]["version"] = 99
    with pytest.raises(ValueError, match="Incompatible export version"):
        db.import_site_data(data, "delete_all")


def test_import_delete_all_rejects_no_admin(admin_user):
    """delete_all import must be rejected when the import has no admin user."""
    import db
    data = db.export_site_data()
    # Remove all admin/owner users from the import data
    data["users"] = [u for u in data["users"] if u["role"] not in ("admin", "owner")]
    with pytest.raises(ValueError, match="does not contain any admin"):
        db.import_site_data(data, "delete_all")
    # Verify existing data was not deleted
    assert db.get_user_by_username("admin") is not None


def test_import_delete_all_rejects_empty_users(admin_user):
    """delete_all import must be rejected when import data has no users at all."""
    import db
    data = db.export_site_data()
    data["users"] = []
    with pytest.raises(ValueError, match="does not contain any admin"):
        db.import_site_data(data, "delete_all")


def test_import_delete_all_rejects_missing_setup_done(admin_user):
    """delete_all import must be rejected when site_settings lacks setup_done=1."""
    import db
    data = db.export_site_data()
    # Set setup_done to 0 in all settings rows
    for s in data.get("site_settings", []):
        s["setup_done"] = 0
    with pytest.raises(ValueError, match="setup_done=1"):
        db.import_site_data(data, "delete_all")


def test_import_delete_all_rejects_missing_site_settings(admin_user):
    """delete_all import must be rejected when site_settings is absent."""
    import db
    data = db.export_site_data()
    data["site_settings"] = []
    with pytest.raises(ValueError, match="setup_done=1"):
        db.import_site_data(data, "delete_all")


def test_import_delete_all_accepts_owner(admin_user):
    """delete_all import must accept a owner user."""
    import db
    data = db.export_site_data()
    # Change the admin user role to owner
    for u in data["users"]:
        if u["role"] == "admin":
            u["role"] = "owner"
    db.import_site_data(data, "delete_all")
    users = db.list_users()
    assert any(u["role"] == "owner" for u in users)


def test_import_override_skips_validation(admin_user):
    """override mode should not apply the delete_all safety checks."""
    import db
    data = db.export_site_data()
    # Remove admin users: this is fine for override mode because it keeps
    # the existing data.
    data["users"] = [u for u in data["users"] if u["role"] not in ("admin", "owner")]
    # Should not raise
    db.import_site_data(data, "override")


def test_import_keep_skips_validation(admin_user):
    """keep mode should not apply the delete_all safety checks."""
    import db
    data = db.export_site_data()
    data["users"] = []
    # Should not raise
    db.import_site_data(data, "keep")


def test_import_delete_all_route_rejects_no_admin(logged_in_admin, admin_user):
    """HTTP import route should show error when delete_all data has no admin."""
    import db
    data = db.export_site_data()
    data["users"] = [u for u in data["users"] if u["role"] not in ("admin", "owner")]
    zip_bytes = _make_zip_from_data(data)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Import failed" in resp.data
    assert b"does not contain any admin" in resp.data


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

def test_migration_page_accessible_to_admin(logged_in_admin):
    resp = logged_in_admin.get("/admin/migration")
    assert resp.status_code == 200
    assert b"Site Migration" in resp.data


def test_migration_page_requires_admin(client, admin_user):
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("regularuser", generate_password_hash("pw"), role="user")
    client.post("/login", data={"username": "regularuser", "password": "pw"})
    resp = client.get("/admin/migration", follow_redirects=True)
    assert b"Admin access required" in resp.data


def test_export_route_returns_zip(logged_in_admin):
    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    assert any(n.endswith(".json") for n in names)


def test_export_zip_contains_valid_json(logged_in_admin):
    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    buf = io.BytesIO(resp.data)
    with zipfile.ZipFile(buf) as zf:
        # The unified archive format places manifest.json first, then
        # site_export.json.  The site dump we care about here is always
        # ``site_export.json``; ``manifest.json`` is metadata about the
        # archive itself.
        data = json.loads(zf.read("site_export.json"))
    assert "_meta" in data
    assert "users" in data


def test_large_export_skips_expensive_site_json(logged_in_admin, monkeypatch):
    import archive_format
    import db

    monkeypatch.setattr(config, "SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES", 1)

    def _fail_export(*args, **kwargs):
        raise AssertionError("site_export.json should be skipped for large DBs")

    monkeypatch.setattr(db, "export_site_data", _fail_export)

    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)

    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read(archive_format.MANIFEST_FILENAME))

    assert archive_format.RAW_DB_FILENAME in names
    assert archive_format.SITE_EXPORT_FILENAME not in names
    assert manifest["has_raw_db"] is True
    assert manifest["has_site_export_json"] is False


def test_export_route_includes_runtime_assets(logged_in_admin):
    """The site export should bundle uploads, attachments, chat files, and custom favicons."""
    import db
    import routes.admin_migration as admin_mod

    upload_file = os.path.join(config.UPLOAD_FOLDER, "wiki-image.png")
    avatar_file = os.path.join(config.UPLOAD_FOLDER, "avatars", "alice.png")
    attachment_file = os.path.join(config.ATTACHMENT_FOLDER, "page-file.txt")
    chat_file = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "chat-file.txt")
    favicon_dir = config.FAVICON_UPLOAD_FOLDER
    favicon_file = os.path.join(favicon_dir, "custom_site.png")

    for path in (upload_file, avatar_file, attachment_file, chat_file, favicon_file):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"asset-data")

    db.update_site_settings(
        favicon_enabled=1,
        favicon_type="custom",
        favicon_custom="custom_site.png",
    )

    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert resp.status_code == 200

    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())

    # Unified archive format: asset directories live at the archive
    # root (no ``assets/`` prefix).
    assert "uploads/wiki-image.png" in names
    assert "uploads/avatars/alice.png" in names
    assert "attachments/page-file.txt" in names
    assert "chat_attachments/chat-file.txt" in names
    assert "favicons/custom_site.png" in names


def _make_zip_from_data(data):
    """Helper: wrap a dict as a ZIP containing site_export.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site_export.json", json.dumps(data))
    buf.seek(0)
    return buf.read()


def test_import_route_delete_all(logged_in_admin, admin_user):
    import db
    data = db.export_site_data()
    zip_bytes = _make_zip_from_data(data)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data


def test_import_route_restores_assets_and_attachment_records(logged_in_admin, admin_user):
    """Import should restore omitted attachment records and runtime assets from the archive."""
    import db
    import routes.admin_migration as admin_mod

    home_page = db.get_home_page()
    db.add_page_attachment(home_page["id"], "page-file.txt", "Page File.txt", 9, admin_user)

    upload_file = os.path.join(config.UPLOAD_FOLDER, "wiki-image.png")
    attachment_file = os.path.join(config.ATTACHMENT_FOLDER, "page-file.txt")
    chat_file = os.path.join(config.CHAT_ATTACHMENT_FOLDER, "chat-file.txt")
    favicon_file = os.path.join(config.FAVICON_UPLOAD_FOLDER, "custom_site.png")

    for path in (upload_file, attachment_file, chat_file, favicon_file):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"before-export")

    db.update_site_settings(
        favicon_enabled=1,
        favicon_type="custom",
        favicon_custom="custom_site.png",
    )

    export_resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert export_resp.status_code == 200

    conn = db.get_db()
    try:
        conn.execute("DELETE FROM page_attachments")
        conn.commit()
    finally:
        conn.close()

    for path in (upload_file, attachment_file, chat_file, favicon_file):
        os.remove(path)

    import_resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(export_resp.data), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert import_resp.status_code == 200
    assert b"imported successfully" in import_resp.data
    attachments = db.get_page_attachments(home_page["id"])
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "page-file.txt"
    for path in (upload_file, attachment_file, chat_file, favicon_file):
        assert os.path.exists(path)


def test_import_route_invalid_mode(logged_in_admin):
    import db
    data = db.export_site_data()
    zip_bytes = _make_zip_from_data(data)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "bad",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Invalid import mode" in resp.data


def test_import_route_no_file(logged_in_admin):
    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={**_CONFIRM, "import_mode": "keep"},
        follow_redirects=True,
    )
    assert b"No file selected" in resp.data


def test_import_route_bad_zip(logged_in_admin):
    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (io.BytesIO(b"not a zip"), "bad.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"valid ZIP" in resp.data


def test_import_route_non_zip_extension(logged_in_admin):
    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (io.BytesIO(b"{}"), "backup.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b".zip" in resp.data


def test_migration_link_on_settings_page(logged_in_admin):
    """The admin settings page should contain a link to the migration tools."""
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b"migration" in resp.data.lower()


# ---------------------------------------------------------------------------
# ZIP bomb / decompressed-size protection
# ---------------------------------------------------------------------------

def test_import_rejects_oversized_member(logged_in_admin, monkeypatch):
    """A single archive member exceeding MAX_IMPORT_MEMBER_SIZE is rejected."""
    monkeypatch.setattr(config, "MAX_IMPORT_MEMBER_SIZE", 50)  # 50 bytes

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site_export.json", json.dumps({"_meta": {}, "users": []}))
        zf.writestr("big_file.bin", "X" * 100)
    buf.seek(0)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (buf, "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"too large" in resp.data


def test_import_rejects_oversized_total(logged_in_admin, monkeypatch):
    """Total uncompressed size exceeding MAX_IMPORT_UNCOMPRESSED_SIZE is rejected."""
    monkeypatch.setattr(config, "MAX_IMPORT_UNCOMPRESSED_SIZE", 100)  # 100 bytes
    monkeypatch.setattr(config, "MAX_IMPORT_MEMBER_SIZE", 200)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site_export.json", json.dumps({"_meta": {}, "users": []}))
        zf.writestr("file_a.bin", "A" * 60)
        zf.writestr("file_b.bin", "B" * 60)
    buf.seek(0)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (buf, "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"exceeds the" in resp.data


def test_import_accepts_within_limits(logged_in_admin, monkeypatch):
    """An archive within both size limits is accepted normally."""
    monkeypatch.setattr(config, "MAX_IMPORT_UNCOMPRESSED_SIZE", 10 * 1024 * 1024)
    monkeypatch.setattr(config, "MAX_IMPORT_MEMBER_SIZE", 5 * 1024 * 1024)

    import db
    data = db.export_site_data()
    zip_bytes = _make_zip_from_data(data)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data


# ---------------------------------------------------------------------------
# Comprehensive export: all tables, standalone files, logs
# ---------------------------------------------------------------------------

def test_export_includes_plugins_table(admin_user):
    """The export should include the plugins table."""
    import db
    data = db.export_site_data()
    assert "plugins" in data


def test_export_includes_api_tokens_table(admin_user):
    """The export should include the api_tokens table."""
    import db
    data = db.export_site_data()
    assert "api_tokens" in data


def test_export_includes_reservation_quota_requests_table(admin_user):
    """The export should include the reservation_quota_requests table."""
    import db
    data = db.export_site_data()
    assert "reservation_quota_requests" in data


def test_export_zip_excludes_secret_key(logged_in_admin, tmp_path, monkeypatch):
    """Site export must NOT include the secret key file (security fix)."""
    sk_path = str(tmp_path / "instance" / ".secret_key")
    os.makedirs(os.path.dirname(sk_path), exist_ok=True)
    with open(sk_path, "w") as f:
        f.write("test-secret-key-value")
    monkeypatch.setattr(config, "SECRET_KEY_FILE", sk_path)

    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
        assert "instance/.secret_key" not in names


def test_export_zip_contains_config(logged_in_admin):
    """Site export should include config.py."""
    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
        assert "instance/config.py" in names


def test_import_does_not_restore_config_py(logged_in_admin, admin_user):
    """Import must NOT overwrite config.py: arbitrary Python would be RCE."""
    import db

    # Read the original config.py content before export.
    config_path = os.path.join(config.BASE_DIR, "config.py")
    with open(config_path, "r") as f:
        original_content = f.read()

    # Export the site (export includes config.py).
    export_resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert export_resp.status_code == 200

    # Build a tampered ZIP that injects malicious code into config.py.
    data = db.export_site_data()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site_export.json", json.dumps(data))
        zf.writestr("instance/config.py", "MALICIOUS = True  # RCE payload")
    buf.seek(0)

    import_resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "delete_all",
            "import_file": (buf, "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert import_resp.status_code == 200
    assert b"imported successfully" in import_resp.data

    # config.py must remain unchanged. The malicious payload must NOT land.
    with open(config_path, "r") as f:
        assert f.read() == original_content


def test_export_zip_excludes_log_files(logged_in_admin, tmp_path, monkeypatch):
    """Site export must NOT include log files (information disclosure)."""
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "bananawiki.log")
    with open(log_path, "w") as f:
        f.write("test log entry\n")
    monkeypatch.setattr(config, "LOG_FILE", log_path)

    resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
        assert "assets/logs/bananawiki.log" not in names
        assert not any(n.startswith("assets/logs/") for n in names)


def test_import_does_not_restore_secret_key(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """Import must NOT restore the secret key. It is excluded from exports."""
    sk_path = str(tmp_path / "instance" / ".secret_key")
    os.makedirs(os.path.dirname(sk_path), exist_ok=True)
    with open(sk_path, "w") as f:
        f.write("original-key")
    monkeypatch.setattr(config, "SECRET_KEY_FILE", sk_path)

    export_resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert export_resp.status_code == 200

    # Change the secret key on disk
    with open(sk_path, "w") as f:
        f.write("changed-key")

    import_resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "delete_all",
            "import_file": (io.BytesIO(export_resp.data), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert import_resp.status_code == 200
    assert b"imported successfully" in import_resp.data

    # The key on disk must remain "changed-key": import must not touch it
    with open(sk_path, "r") as f:
        assert f.read() == "changed-key"


def test_import_keep_mode_preserves_existing_secret_key(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """Import in keep mode should not overwrite the existing secret key."""
    sk_path = str(tmp_path / "instance" / ".secret_key")
    os.makedirs(os.path.dirname(sk_path), exist_ok=True)
    with open(sk_path, "w") as f:
        f.write("current-key")
    monkeypatch.setattr(config, "SECRET_KEY_FILE", sk_path)

    # Build a ZIP that manually includes a secret key entry (simulating an old
    # export format) to ensure the importer ignores it.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf_out:
        zf_out.writestr("instance/.secret_key", "old-key-from-zip")
    buf.seek(0)

    import_resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "keep",
            "import_file": (buf, "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert import_resp.status_code == 200

    # The secret key on disk must never be touched by the importer
    with open(sk_path, "r") as f:
        assert f.read() == "current-key"


def test_import_does_not_restore_log_files(logged_in_admin, admin_user, tmp_path, monkeypatch):
    """Import must NOT restore log files. They are excluded from exports."""
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "bananawiki.log")
    with open(log_path, "w") as f:
        f.write("exported log content")
    monkeypatch.setattr(config, "LOG_FILE", log_path)

    export_resp = logged_in_admin.post("/admin/migration/export", data=_CONFIRM)
    assert export_resp.status_code == 200

    # Logs should not appear in the archive at all.
    with zipfile.ZipFile(io.BytesIO(export_resp.data)) as zf:
        assert not any(n.startswith("assets/logs/") for n in zf.namelist())


def test_migration_page_lists_all_contents(logged_in_admin):
    """The migration page should list what's included in the export."""
    resp = logged_in_admin.get("/admin/migration")
    assert resp.status_code == 200
    body = resp.data.lower()
    assert b"secret key" in body
    assert b"config" in body
    assert b"password hash of every account" in body
    # Log files are excluded from the export (see the tests above), so the
    # list must not claim them. The translation table embedded in the page
    # still carries the old string, hence the check on the list item.
    assert b"<li>log files</li>" not in body


# ---------------------------------------------------------------------------
# Separate error handling for DB vs. asset import
# ---------------------------------------------------------------------------

def test_import_asset_failure_does_not_claim_rollback(logged_in_admin, admin_user, monkeypatch):
    """When DB import succeeds but asset extraction fails, the flash message
    should NOT claim the operation was rolled back. Instead, it should report
    partial success with a warning about missing assets."""
    import db
    import routes.admin_migration as admin_mod

    data = db.export_site_data()
    zip_bytes = _make_zip_from_data(data)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(admin_mod, "_import_site_migration_assets", _boom)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "override",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data
    assert b"asset files could not be restored" in resp.data
    assert b'class="flash-message">An unexpected error' not in resp.data


def test_import_db_failure_reports_rollback(logged_in_admin, admin_user, monkeypatch):
    """When db.import_site_data() raises, the flash should report rollback."""
    import db
    import routes.admin_migration as admin_mod

    data = db.export_site_data()
    zip_bytes = _make_zip_from_data(data)

    def _db_fail(*_a, **_kw):
        raise RuntimeError("corrupt data")

    monkeypatch.setattr(admin_mod.db, "import_site_data", _db_fail)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "override",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"rolled back" in resp.data
    assert b'<span class="flash-message">Site data imported successfully' not in resp.data


def test_import_asset_failure_still_commits_db(logged_in_admin, admin_user, monkeypatch):
    """When asset extraction fails, the database changes should still be
    committed (they are not rolled back)."""
    import db
    import routes.admin_migration as admin_mod

    home_page = db.get_home_page()

    data = db.export_site_data()
    for p in data.get("pages", []):
        if p.get("slug") == home_page["slug"]:
            p["title"] = "Modified Title For Test"

    zip_bytes = _make_zip_from_data(data)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(admin_mod, "_import_site_migration_assets", _boom)

    resp = logged_in_admin.post(
        "/admin/migration/import",
        data={
            **_CONFIRM,
            "import_mode": "override",
            "import_file": (io.BytesIO(zip_bytes), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"imported successfully" in resp.data

    updated = db.get_home_page()
    assert updated["title"] == "Modified Title For Test"
