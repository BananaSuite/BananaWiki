"""Whole-site export and import (1.4 audit C2: imports never write code or keys)."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from bananawiki.core.sqlite import Session
from tests.conftest import PASSWORD
from tests.features.pages_support import make_page


def export(client, password=PASSWORD):
    return client.post("/admin/migration/export", data={"password": password})


def do_import(client, archive: bytes, **extra):
    data = {"password": PASSWORD, "confirm_replace": "1", "import_file": (io.BytesIO(archive), "site.zip")}
    data.update(extra)
    return client.post("/admin/migration/import", data=data, content_type="multipart/form-data")


def query(app, sql, params=()):
    session = Session(app.extensions["bananawiki.database"].connect())
    try:
        return session.all(sql, params)
    finally:
        session.conn.close()


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def admin_client_for(app, login, username="boss"):
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        user = accounts.create(username, PASSWORD, role="admin", emit_event=False)
    client = app.test_client()
    login(client, user)
    return client


@pytest.fixture
def source(app, admin_client):
    """A wiki with a page, an uploaded file, a custom icon and a language; returns its archive."""
    make_page(app, "Exported page", "hello from the source")
    folders = app.config["BW"].folders
    Path(folders.uploads).mkdir(parents=True, exist_ok=True)
    (Path(folders.uploads) / "picture.png").write_bytes(b"png-bytes")
    Path(folders.favicons).mkdir(parents=True, exist_ok=True)
    (Path(folders.favicons) / "custom_icon.png").write_bytes(b"icon")
    (Path(folders.favicons) / "stray.png").write_bytes(b"stray")
    response = export(admin_client)
    assert response.status_code == 200, response.data[:300]
    data = response.get_data()
    response.close()
    return data


@pytest.fixture
def target(app_factory, tmp_path, login):
    other = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "target")})
    return other, admin_client_for(other, login, "target_admin")


def raw_db(archive: bytes) -> bytes:
    return zipfile.ZipFile(io.BytesIO(archive)).read("bananawiki.db")


def modified_db(archive: bytes, tmp_path, sql: str) -> bytes:
    path = tmp_path / "edit.db"
    path.write_bytes(raw_db(archive))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(sql)
    conn.close()
    return path.read_bytes()


def test_export_needs_the_password(app, admin_client, db):
    response = export(admin_client, "wrong")
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'site.export_refused'") == 1


def test_export_contents(app, source, db):
    archive = zipfile.ZipFile(io.BytesIO(source))
    names = set(archive.namelist())
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format_version"] == 1 and manifest["has_raw_db"] is True
    assert {"bananawiki.db", "uploads/picture.png", "favicons/custom_icon.png"} <= names
    assert "favicons/stray.png" not in names and not any(".secret_key" in name for name in names)
    path = Path(app.config["BW"].folders.exports) / "check.db"
    path.write_bytes(archive.read("bananawiki.db"))
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 0
    conn.close()
    path.unlink()
    leftovers = [p for p in Path(app.config["BW"].folders.exports).iterdir() if p.name.startswith("bw-site-export-")]
    assert leftovers == []


def test_round_trip_replaces_the_target(source, target):
    other, client = target
    response = do_import(client, source)
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert query(other, "SELECT title FROM pages WHERE title = 'Exported page'")
    assert query(other, "SELECT id FROM users WHERE username = 'admin_user'")
    assert not query(other, "SELECT id FROM users WHERE username = 'target_admin'")
    assert query(other, "SELECT COUNT(*) AS n FROM user_sessions")[0]["n"] == 0
    assert query(other, "SELECT maintenance_mode FROM site_settings")[0]["maintenance_mode"] == 0
    folders = other.config["BW"].folders
    assert (Path(folders.uploads) / "picture.png").read_bytes() == b"png-bytes"
    assert (Path(folders.favicons) / "custom_icon.png").exists()
    backups = list((Path(other.config["BW"].instance_dir) / "backups").glob("pre-import-*"))
    assert len(backups) == 1 and (backups[0] / "bananawiki.db").exists()
    assert query(other, "SELECT action FROM audit_log WHERE action = 'site.imported'")
    assert client.get("/admin/migration").status_code == 302  # signed out


def test_import_requires_confirmation_and_password(source, target):
    other, client = target
    do_import(client, source, confirm_replace="")
    do_import(client, source, password="wrong")
    assert query(other, "SELECT id FROM users WHERE username = 'target_admin'")


def test_managed_hosting_refuses_unless_the_host_allows(source, app_factory, tmp_path, login):
    managed = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "m"), "BW_MANAGED_HOSTING": "1"})
    client = admin_client_for(managed, login)
    do_import(client, source)
    assert not query(managed, "SELECT id FROM pages WHERE title = 'Exported page'")
    allowed = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "a"), "BW_MANAGED_HOSTING": "1",
                                   "BW_ALLOW_SITE_IMPORT": "1"})
    do_import(admin_client_for(allowed, login), source)
    assert query(allowed, "SELECT id FROM pages WHERE title = 'Exported page'")


def test_code_and_secrets_in_the_archive_are_ignored(source, target):
    other, client = target
    members = {name: zipfile.ZipFile(io.BytesIO(source)).read(name)
               for name in zipfile.ZipFile(io.BytesIO(source)).namelist()}
    members.update({
        "instance/config.py": b"import os; os.system('id')", "instance/.secret_key": b"stolen",
        ".secret_key": b"stolen", "database/bananawiki.db": b"junk", "plugins/evil/__init__.py": b"x",
        "uploads/../../escape.txt": b"x", "favicons/banana_yellow.png": b"x", "config.py": b"x",
    })
    key_file = Path(other.config["BW"].instance_dir) / ".secret_key"
    before = key_file.read_bytes() if key_file.exists() else None
    do_import(client, zip_bytes(members))
    assert query(other, "SELECT id FROM pages WHERE title = 'Exported page'")
    instance = Path(other.config["BW"].instance_dir)
    assert (key_file.read_bytes() if key_file.exists() else None) == before
    assert not (instance.parent / "escape.txt").exists() and not (instance / "escape.txt").exists()
    assert not (instance / "plugins" / "evil").exists()
    assert not (Path(other.config["BW"].folders.favicons) / "banana_yellow.png").exists()


@pytest.mark.parametrize("sql", [
    "PRAGMA application_id = 1234",
    "PRAGMA user_version = 99",
    "UPDATE users SET role = 'user'",
    "UPDATE site_settings SET setup_done = 0",
])
def test_unusable_databases_are_refused(source, target, tmp_path, sql):
    other, client = target
    archive = zip_bytes({"manifest.json": b'{"format_version": 1}', "bananawiki.db": modified_db(source, tmp_path, sql)})
    do_import(client, archive)
    assert query(other, "SELECT id FROM users WHERE username = 'target_admin'")
    assert query(other, "SELECT maintenance_mode FROM site_settings")[0]["maintenance_mode"] == 0


@pytest.mark.parametrize("archive", [
    b"not a zip",
    zip_bytes({"readme.txt": b"hi"}),
    zip_bytes({"manifest.json": b'{"format_version": 7}', "bananawiki.db": b""}),
    zip_bytes({"bananawiki.db": b"SQLite format 3\x00garbage" * 10}),
], ids=["not_zip", "no_database", "newer_format", "garbage_database"])
def test_broken_archives_are_refused(target, archive):
    other, client = target
    do_import(client, archive)
    assert query(other, "SELECT id FROM users WHERE username = 'target_admin'")
    exports = Path(other.config["BW"].folders.exports)
    assert not [p for p in exports.iterdir() if p.name.startswith("bw-site-import-")]


def test_legacy_hosting_layout(source, target):
    other, client = target
    archive = zip_bytes({"mywiki/bananawiki.db": raw_db(source), "mywiki/uploads/old.png": b"old",
                         "mywiki/error.log": b"log"})
    do_import(client, archive)
    assert query(other, "SELECT id FROM pages WHERE title = 'Exported page'")
    assert (Path(other.config["BW"].folders.uploads) / "old.png").exists()


def test_legacy_json_dump(source, target, tmp_path):
    other, client = target
    path = tmp_path / "legacy.db"
    path.write_bytes(raw_db(source))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    dump = {"_meta": {"version": 1}}
    for table in ("users", "site_settings", "pages", "categories"):
        dump[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    conn.close()
    archive = zip_bytes({"site_export.json": json.dumps(dump, default=str).encode(),
                         "assets/uploads/legacy.png": b"legacy"})
    response = do_import(client, archive)
    assert response.status_code == 302
    assert query(other, "SELECT id FROM pages WHERE title = 'Exported page'")
    assert (Path(other.config["BW"].folders.uploads) / "legacy.png").exists()
