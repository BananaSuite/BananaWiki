"""Upgrade and routing guarantees for the public source release."""

import db
import io
from hosting import config as hosting_config
from hosting.app import create_hosting_app
from hosting import db as hosting_db


def test_upgrade_removes_registration_metadata_and_preserves_wiki(admin_user):
    page_id = db.create_page("Migration example", "migration-example", content="Keep this content", user_id=admin_user)
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE licenses (id INTEGER PRIMARY KEY, license_content TEXT)")
        conn.execute("INSERT INTO licenses VALUES (1, 'retired registration')")
        conn.execute("ALTER TABLE site_settings ADD COLUMN license_info_enabled INTEGER DEFAULT 1")
        conn.execute("PRAGMA user_version=0")  # Fixture represents a pre-versioned installation.
        conn.commit()

    db.init_db()
    db.init_db()

    with db.get_db_context() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='licenses'").fetchone() is None
        assert "license_info_enabled" not in {row[1] for row in conn.execute("PRAGMA table_info(site_settings)")}
        assert conn.execute("SELECT content FROM pages WHERE id=?", (page_id,)).fetchone()[0] == "Keep this content"
        assert conn.execute("SELECT 1 FROM users WHERE id=?", (admin_user,)).fetchone()


def test_wiki_has_no_registration_routes_or_banner(logged_in_admin):
    assert logged_in_admin.get("/admin/license").status_code == 404
    assert logged_in_admin.get("/license").status_code == 404
    response = logged_in_admin.get("/")
    assert response.status_code == 200
    assert b"not yet registered" not in response.data
    assert b"Upload a license" not in response.data


def test_hosting_upgrade_preserves_instance_and_retires_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    hosting_db.init_hosting_db()
    account = hosting_db.create_account("migration-owner", "hash")
    instance = hosting_db.create_instance(account, "migration-wiki", "admin", "temporary")
    with hosting_db.get_hosting_db_context() as conn:
        for column in ("custom_license_name", "custom_license_content", "custom_license_assigned_at"):
            conn.execute(f"ALTER TABLE instances ADD COLUMN {column} TEXT")
        conn.execute("UPDATE instances SET custom_license_content='retired registration'")
        conn.execute("PRAGMA user_version=0")  # Fixture represents a pre-versioned installation.
        conn.commit()

    hosting_db.init_hosting_db()
    hosting_db.init_hosting_db()

    migrated = hosting_db.get_instance(instance["id"])
    assert migrated["account_id"] == account
    assert not any(name.startswith("custom_license_") for name in migrated.keys())
    with hosting_db.get_hosting_db_context() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_portal_serves_its_own_help_and_has_no_site_editor(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    application = create_hosting_app()
    application.config.update(TESTING=True)
    client = application.test_client()

    assert client.get("/help").status_code == 200
    assert client.get("/help/getting-started").status_code == 200
    assert client.get("/help/..%3Fbad").status_code == 404
    assert not hasattr(hosting_config, "HOSTING_HELP_URL")
    assert all("/admin/landing" not in str(rule) and "/license" not in str(rule) for rule in application.url_map.iter_rules())


def test_portal_rejects_retired_application_package_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    account = hosting_db.create_account("package-admin", "hash", is_admin=True)
    version = hosting_db.get_account_by_id(account)["session_version"]
    _, token = hosting_db.create_hosting_session(account, version)
    client = application.test_client()
    with client.session_transaction() as session:
        session["hosting_account_id"] = account
        session["hosting_session_version"] = version
        session["hosting_auth_session_token"] = token
    settings = client.get("/admin/settings")
    assert settings.status_code == 200
    assert b'upgrade_file' not in settings.data
    response = client.post("/admin/settings", data={
        "action": "upgrade",
        "upgrade_file": (io.BytesIO(b"retired package"), "legacy.bwpackage"),
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Unknown settings action" in response.data


def test_export_never_reintroduces_retired_registration_data(admin_user):
    from db._migration import _get_migration_tables
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE licenses (id INTEGER PRIMARY KEY, license_content TEXT)")
        conn.execute("INSERT INTO licenses VALUES (1, 'retired key')")
        assert "licenses" not in _get_migration_tables(conn)
