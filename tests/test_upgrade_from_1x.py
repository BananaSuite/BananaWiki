"""Booting BananaWiki 1.6 on a real 1.4 instance keeps users, sessions, tokens and content."""

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from bananawiki.core.sqlite import Session
from bananawiki.wiki.app import create_app
from bananawiki.wiki.config import load_config
from bananawiki.wiki.migrations import LATEST

FIXTURE = Path(__file__).parent / "fixtures" / "v1_instance"


def _boot(tmp_path, prepare=None):
    instance = tmp_path / "instance"
    (instance / "uploads").mkdir(parents=True)
    shutil.copy(FIXTURE / "bananawiki-v3.sqlite3", instance / "bananawiki.db")
    shutil.copy(FIXTURE / "uploads" / "abc123.png", instance / "uploads" / "abc123.png")
    if prepare is not None:
        with sqlite3.connect(instance / "bananawiki.db") as conn:
            prepare(conn)
        conn.close()
    key = (FIXTURE / "secret_key").read_text().strip()
    environ = {"BW_ENV": "test", "BW_INSTANCE_DIR": str(instance), "BW_BACKGROUND_JOBS": "0"}
    app = create_app(load_config(environ, secret_key=key))
    return app, instance, json.loads((FIXTURE / "credentials.json").read_text())


@pytest.fixture
def legacy(tmp_path):
    return _boot(tmp_path)


def _db(app):
    return Session(app.extensions["bananawiki.database"].connect())


def test_schema_upgraded_with_backup(legacy):
    app, instance, _ = legacy
    assert _db(app).scalar("PRAGMA user_version") == LATEST
    assert list((instance / "backups").glob("pre-upgrade-v3-*.db"))


def test_session_cookie_and_content_survive(legacy):
    app, _, creds = legacy
    _db(app).execute("UPDATE users SET onboarding_required = 0")
    client = app.test_client()
    client.set_cookie("bw_session", creds["cookie"])
    page = client.get("/page/photosynthesis")
    assert page.status_code == 200 and b"light energy" in page.data and b"/users/editor1" in page.data
    assert b"second edit" in client.get("/page/photosynthesis/history").data
    assert client.get("/static/uploads/abc123.png").status_code == 200
    assert b"Photosynthesis" in client.get("/search?q=light").data
    assert b"Team board" in client.get("/kanban").data
    assert b"editor1" in client.get("/chats").data
    assert b"#123456" in client.get("/").data


def test_password_and_api_token_survive(legacy):
    app, _, creds = legacy
    db = _db(app)
    db.execute("UPDATE site_settings SET api_service_enabled = 1")
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'api_service'")
    db.execute("UPDATE users SET api_access_enabled = 1, onboarding_required = 0")
    response = app.test_client().get("/api/v1/pages", headers={"Authorization": "Bearer " + creds["token"]})
    assert response.status_code == 200 and b"photosynthesis" in response.data
    client = app.test_client()
    token = re.search(rb'name="csrf-token" content="([^"]+)"', client.get("/login").data).group(1).decode()
    login = client.post("/login", data={"username": "editor1", "password": "editor-password-1",
                                        "csrf_token": token})
    assert login.status_code == 302


def test_saved_permissions_keep_reading_after_view_all_is_enforced(tmp_path):
    # 1.4 never checked the view_all keys, so an administrator could untick
    # them with no effect. The upgrade grants them to every saved set, and
    # nothing else.
    def prepare(conn):
        conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) "
                     "VALUES ('gr73yja0', 'read', 1), ('gr73yja0', 'write', 0)")
        conn.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) "
                     "VALUES ('gr73yja0', 2, 'read')")
        conn.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES ('gr73yja0', 'history.view')")
        conn.execute("INSERT INTO custom_roles (id, name, base_role) VALUES (7, 'Students', 'user')")
        conn.execute("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (7, 'search.pages')")

    app, _, _ = _boot(tmp_path, prepare)
    from bananawiki.wiki.permissions import load_grants

    db = _db(app)
    reader = db.one("SELECT * FROM users WHERE id = 'gr73yja0'")
    grants = load_grants(db, reader)
    assert {"page.view_all", "category.view_all", "history.view"} <= grants.keys
    assert "page.create" not in grants.keys and grants.read.restricted
    assert set(db.column("SELECT permission_key FROM custom_role_permissions WHERE role_id = 7")) == {
        "search.pages", "page.view_all", "category.view_all"}
    assert db.column("SELECT permission_key FROM user_permissions WHERE user_id = 'k9tipld7'") == []


def test_encrypted_setting_still_decrypts(legacy):
    app, _, _ = legacy
    from bananawiki.wiki import settings

    with app.test_request_context():
        from bananawiki.wiki.db import connection_scope

        with connection_scope():
            assert settings.load()["tts_gpu_auth_token"] == "gpu-secret-token"
