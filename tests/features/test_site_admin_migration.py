"""Whole-site export and import: imports never write code or keys."""

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


def admin_client_for(app, login, username="boss", role="owner"):
    """A signed-in client of *app*: an owner by default, the only ones (with superusers) who move sites."""
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        user = accounts.create(username, PASSWORD, role=role, emit_event=False)
    client = app.test_client()
    login(client, user)
    return client


@pytest.fixture
def owner_client(app, login):
    return admin_client_for(app, login, "site_owner")


@pytest.fixture
def source(app, admin_client, owner_client):
    """A wiki with a page, an uploaded file, a custom icon and a language; returns its archive."""
    make_page(app, "Exported page", "hello from the source")
    folders = app.config["BW"].folders
    Path(folders.uploads).mkdir(parents=True, exist_ok=True)
    (Path(folders.uploads) / "picture.png").write_bytes(b"png-bytes")
    Path(folders.favicons).mkdir(parents=True, exist_ok=True)
    (Path(folders.favicons) / "custom_icon.png").write_bytes(b"icon")
    (Path(folders.favicons) / "stray.png").write_bytes(b"stray")
    response = export(owner_client)
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


def test_export_needs_the_password(app, owner_client, db):
    response = export(owner_client, "wrong")
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


def test_export_leaves_out_stored_api_answers(app, owner_client, admin, db):
    """Idempotency-Key answers are a short-lived retry cache, not site data."""
    db.execute("INSERT INTO api_service__idempotency (user_id, token_id, idempotency_key, request_hash, status_code, "
               "response_body, created_at) VALUES (?, 1, 'k', 'x', 201, '{}', datetime('now'))", (admin["id"],))
    response = export(owner_client)
    assert response.status_code == 200
    path = Path(app.config["BW"].folders.exports) / "check.db"
    path.write_bytes(raw_db(response.get_data()))
    response.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM api_service__idempotency").fetchone()[0] == 0
    conn.close()
    path.unlink()


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


def _packed(source: bytes, method: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bananawiki.db", raw_db(source), compress_type=method)
    return buffer.getvalue()


def _flagged(source: bytes, flag: int = 1) -> bytes:
    """The database member with *flag* set, encrypted by default (zipfile cannot write such members)."""
    raw = bytearray(_packed(source, zipfile.ZIP_STORED))
    for position in (raw.index(b"PK\x03\x04") + 6, raw.rindex(b"PK\x01\x02") + 8):  # local and central headers
        raw[position:position + 2] = flag.to_bytes(2, "little")
    return bytes(raw)


@pytest.mark.parametrize("build", [
    lambda source: _packed(source, zipfile.ZIP_BZIP2),
    lambda source: _packed(source, zipfile.ZIP_LZMA),
    _flagged,
    lambda source: _flagged(source, 0x20),
    lambda source: _flagged(source, 0x40),
], ids=["bzip2", "lzma", "encrypted", "patched-data", "strong-encryption"])
def test_members_zipfile_cannot_read_safely_are_refused(source, target, build):
    """zipfile decompresses bzip2 and LZMA reads without an output limit: a member of a few hundred bytes
    can take gigabytes of memory, whatever size it declares. Nothing is read from such an archive."""
    other, client = target
    response = do_import(client, build(source))
    assert response.status_code == 302
    assert query(other, "SELECT id FROM users WHERE username = 'target_admin'")
    assert not query(other, "SELECT id FROM pages WHERE title = 'Exported page'")
    refused = query(other, "SELECT details FROM audit_log WHERE action = 'site.import_refused'")
    assert len(refused) == 1 and "unsupported_compression" in refused[0]["details"]
    exports = Path(other.config["BW"].folders.exports)
    assert not [p for p in exports.iterdir() if p.name.startswith("bw-site-import-")]


def _damaged(archive: bytes, member: str) -> bytes:
    """*archive* with bytes in the middle of *member*'s data flipped (an interrupted or bit-rotted download)."""
    info = zipfile.ZipFile(io.BytesIO(archive)).getinfo(member)
    raw = bytearray(archive)
    header = info.header_offset
    start = header + 30 + int.from_bytes(raw[header + 26:header + 28], "little") + int.from_bytes(
        raw[header + 28:header + 30], "little")
    middle = start + info.compress_size // 2
    for position in range(middle, middle + 64):
        raw[position] ^= 0x5A
    return bytes(raw)


def _json_dump(source: bytes, tmp_path, **settings_row) -> bytes:
    path = tmp_path / "legacy.db"
    path.write_bytes(raw_db(source))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    dump = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in ("users", "pages")}
    conn.close()
    dump["site_settings"] = [{"id": 1, "setup_done": 1, **settings_row}]
    return zip_bytes({"site_export.json": json.dumps(dump, default=str).encode()})


@pytest.mark.parametrize("build,reason", [
    (lambda source, _tmp: _damaged(_packed(source, zipfile.ZIP_DEFLATED), "bananawiki.db"), "corrupt"),
    (lambda source, _tmp: _damaged(_packed(source, zipfile.ZIP_STORED), "bananawiki.db"), "corrupt"),
    (lambda source, _tmp: zip_bytes({"bananawiki.db": raw_db(source), "uploads/a": b"x", "uploads/a/b": b"y"}),
     "corrupt"),
    (lambda source, _tmp: zip_bytes({"bananawiki.db": raw_db(source), "uploads/a/b": b"y", "uploads/a": b"x"}),
     "corrupt"),
    (lambda source, tmp: _json_dump(source, tmp, site_name={"__b64__": "abc"}), "json"),
    (lambda source, tmp: _json_dump(source, tmp, site_name={"__b64__": 5}), "json"),
    (lambda source, tmp: _json_dump(source, tmp, site_name=2 ** 70), "json"),
], ids=["bad-deflate-data", "bad-crc", "file-then-folder", "folder-then-file", "bad-base64", "base64-not-text",
        "huge-number"])
def test_damaged_archives_are_refused_with_a_message(source, target, tmp_path, build, reason):
    other, client = target
    response = do_import(client, build(source, tmp_path))
    assert response.status_code == 302
    refused = query(other, "SELECT details FROM audit_log WHERE action = 'site.import_refused'")
    assert len(refused) == 1 and reason in refused[0]["details"]
    assert query(other, "SELECT id FROM users WHERE username = 'target_admin'")
    assert not query(other, "SELECT id FROM pages WHERE title = 'Exported page'")
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


# Who moves a site, and what an archive may bring along


def test_plain_administrators_cannot_export_or_import(app, admin_client, source, db):
    """The archive holds every password hash and private message, and an import replaces every account."""
    assert export(admin_client).status_code == 403
    assert do_import(admin_client, source).status_code == 403
    page = admin_client.get("/admin/migration").get_data(as_text=True)
    assert "/admin/migration/export" not in page and "/admin/migration/import" not in page
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'admin_user' AND role = 'admin'") == 1


def test_import_drops_triggers_and_views_this_release_does_not_create(source, target, tmp_path):
    other, client = target
    archive_db = tmp_path / "evil.db"
    archive_db.write_bytes(raw_db(source))
    conn = sqlite3.connect(archive_db, isolation_level=None)
    conn.execute("CREATE TRIGGER backdoor AFTER UPDATE ON users BEGIN "
                 "UPDATE users SET role = 'owner', is_superuser = 1 WHERE username = 'admin_user'; END")
    conn.execute("CREATE VIEW leak AS SELECT username, password FROM users")
    conn.execute("DROP TRIGGER pages_fts_update")
    conn.execute("CREATE TRIGGER pages_fts_update AFTER UPDATE ON pages BEGIN "
                 "UPDATE users SET role = 'owner'; END")
    conn.close()
    response = do_import(client, zip_bytes({"manifest.json": b'{"format_version": 1}',
                                            "bananawiki.db": archive_db.read_bytes()}))
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    names = {row["name"]: row["sql"] for row in query(other, "SELECT name, sql FROM sqlite_master "
                                                             "WHERE type IN ('trigger', 'view')")}
    assert "backdoor" not in names and "leak" not in names
    assert {"pages_fts_insert", "pages_fts_delete", "pages_fts_update"} <= set(names)
    assert "users" not in names["pages_fts_update"]


def test_import_of_a_wiki_without_a_search_index_builds_one(source, target, tmp_path):
    """A wiki upgraded where SQLite lacked FTS5 has no pages_fts; its triggers must not come back without it."""
    other, client = target
    archive_db = tmp_path / "no_fts.db"
    archive_db.write_bytes(raw_db(source))
    conn = sqlite3.connect(archive_db, isolation_level=None)
    for trigger in ("pages_fts_insert", "pages_fts_delete", "pages_fts_update"):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE pages_fts")
    conn.close()
    response = do_import(client, zip_bytes({"manifest.json": b'{"format_version": 1}',
                                            "bananawiki.db": archive_db.read_bytes()}))
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    session = Session(other.extensions["bananawiki.database"].connect())
    try:
        assert session.all("SELECT rowid FROM pages_fts WHERE pages_fts MATCH 'source'")
        session.execute("UPDATE pages SET content = 'rewritten after the import' WHERE title = 'Exported page'")
        assert session.all("SELECT rowid FROM pages_fts WHERE pages_fts MATCH 'rewritten'")
        session.execute("DELETE FROM pages WHERE title = 'Exported page'")
    finally:
        session.conn.close()


def test_import_keeps_the_live_audit_log(source, target):
    other, client = target
    session = Session(other.extensions["bananawiki.database"].connect())
    session.execute("INSERT INTO audit_log (created_at, action, details) VALUES (datetime('now'), 'live.marker', '{}')")
    session.conn.close()
    do_import(client, source)
    actions = [row["action"] for row in query(other, "SELECT action FROM audit_log ORDER BY id")]
    assert "live.marker" in actions, "an import must not erase the record of what happened here"
    assert actions.index("live.marker") < actions.index("site.imported")
    assert query(other, "SELECT 1 FROM audit_log WHERE action = 'user.login' AND details LIKE '%site_owner%'"), \
        "the archive's own history is kept too"
