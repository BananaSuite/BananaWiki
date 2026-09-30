"""Portable wiki archives: export, strict import (1.4 layouts included) and copies."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from bananawiki.hosting.runtime import RuntimeFailure
from bananawiki.hosting.runtime.archives import destination

from .agent_fakes import make_runtime, make_spec, provision


@pytest.fixture
def setup(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    root = Path(runtime._cfg().instances_dir) / "acme"
    (root / "storage" / "uploads" / "photo.png").write_bytes(b"\x89PNG data")
    (root / "external_plugins" / "tool").mkdir()
    (root / "external_plugins" / "tool" / "plugin.py").write_text("print('hi')")
    return runtime, agent, spec, root


def _export(runtime, spec, tmp_path) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return runtime.export_archive(spec, out)


def test_export_writes_the_unified_format(setup, tmp_path):
    runtime, agent, spec, root = setup
    (root / "access.log").write_text("old log")
    (root / "storage" / "uploads" / "secret-link").symlink_to("/etc/hostname")
    with sqlite3.connect(root / "bananawiki.db") as conn:
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at) "
                     "VALUES ('s1', 't1', ?, 'x', 'x', 'x')", (user_id,))
    path = _export(runtime, spec, tmp_path)
    assert path.name.startswith("bananawiki-acme-") and path.suffix == ".zip"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        snapshot = tmp_path / "snap.db"
        snapshot.write_bytes(archive.read("bananawiki.db"))
    assert manifest["format_version"] == 1 and manifest["source"] == "hosting"
    assert "uploads/photo.png" in names, "asset folders are flat, as a self-hosted wiki expects"
    assert not any("secret-link" in n or n.endswith(".secret_key") or "access.log" in n or ".bw-host" in n
                   for n in names)
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 0, "sessions never travel"
    assert not list((root / ".bw-host").iterdir()), "the database copy is removed afterwards"
    assert agent.tasks()[-1] == "snapshot"


def test_export_import_round_trip(setup, tmp_path):
    runtime, agent, spec, _root = setup
    path = _export(runtime, spec, tmp_path)
    copy = make_spec("copy")
    runtime.import_archive(copy, path)
    new_root = Path(runtime._cfg().instances_dir) / "copy"
    assert (new_root / "storage" / "uploads" / "photo.png").read_bytes() == b"\x89PNG data"
    assert os.readlink(new_root / "uploads") == "storage/uploads"
    assert [u.username for u in runtime.list_users(copy)[0]] == ["owner1"]
    assert not (new_root / "external_plugins" / "tool").exists(), "code is never imported"
    assert "copy" in agent.containers
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(copy, path)
    assert error.value.code == "data_exists"


def test_import_accepts_1x_hosting_archives(setup, tmp_path):
    runtime, _agent, _spec, root = setup
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.write(root / "bananawiki.db", "oldwiki/bananawiki.db")
        archive.writestr("oldwiki/storage/uploads/a.png", b"a")
        archive.writestr("oldwiki/custom_page_files/page.html", b"<p>")
        archive.writestr("oldwiki/.secret_key", "leaked")
        archive.writestr("oldwiki/external_plugins/evil/plugin.py", "import os")
        archive.writestr("oldwiki/config.py", "BAD = 1")
    runtime.import_archive(make_spec("restored"), legacy)
    new_root = Path(runtime._cfg().instances_dir) / "restored"
    assert (new_root / "storage" / "uploads" / "a.png").read_bytes() == b"a"
    assert (new_root / "storage" / "custom_page_files" / "page.html").exists()
    assert not (new_root / "config.py").exists() and not (new_root / "external_plugins" / "evil").exists()
    assert (new_root / ".secret_key").read_text() != "leaked"


def test_import_accepts_a_json_dump_without_database(setup, tmp_path):
    runtime, _agent, _spec, _root = setup
    dump = {
        "users": [{"id": "u1", "username": "boss", "password": "pbkdf2:sha256:1000$x$y", "role": "admin"}],
        "pages": [{"id": 1, "title": "Home", "slug": "home", "content": "hi", "is_home": 1}],
        "site_settings": [{"id": 1, "setup_done": 1}],
    }
    archive_path = tmp_path / "standalone.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("site_export.json", json.dumps(dump))
        archive.writestr("assets/uploads/b.png", b"b")
    spec = make_spec("fromjson")
    runtime.import_archive(spec, archive_path)
    assert [u.username for u in runtime.list_users(spec)[0]] == ["boss"]
    root = Path(runtime._cfg().instances_dir) / "fromjson"
    assert (root / "storage" / "uploads" / "b.png").exists()
    assert not (root / ".bw-host" / "site_export.json").exists()


def _zip(tmp_path, members, name="bad.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members:
            archive.writestr(member, data)
    return path


def _symlink_zip(tmp_path) -> Path:
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("bananawiki.db", b"x")
        info = zipfile.ZipInfo("uploads/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    return path


@pytest.mark.parametrize(("build", "code"), [
    (lambda t: _zip(t, [("../evil", b"x"), ("bananawiki.db", b"x")]), "archive_invalid"),
    (lambda t: _zip(t, [("/abs/path", b"x")]), "archive_invalid"),
    (lambda t: _zip(t, [("uploads\\..\\x", b"x"), ("bananawiki.db", b"x")]), "archive_invalid"),
    (lambda t: _zip(t, [("uploads/a.png", b"x")]), "archive_invalid"),
    (_symlink_zip, "archive_invalid"),
    (lambda t: _zip(t, [("bananawiki.db", b"x"), ("storage/uploads/a", b"1"), ("uploads/a", b"2")]),
     "archive_invalid"),
    (lambda t: _zip(t, [("manifest.json", json.dumps({"format_version": 99})), ("bananawiki.db", b"x")]),
     "archive_invalid"),
    (lambda t: (t / "plain.zip").write_bytes(b"not a zip") and t / "plain.zip", "archive_invalid"),
])
def test_import_rejects_unsafe_archives_before_writing(setup, tmp_path, build, code):
    runtime, agent, _spec, _root = setup
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("target"), build(tmp_path))
    assert error.value.code == code
    base = Path(runtime._cfg().instances_dir)
    assert not (base / "target").exists()
    assert not [p for p in base.iterdir() if p.name.startswith(".import-")], "staging is removed"
    assert (tmp_path / "evil").exists() is False


def test_import_limits(tmp_path):
    runtime, _agent = make_runtime(tmp_path, HOSTING_IMPORT_MAX_MEMBERS="2", HOSTING_IMPORT_MAX_BYTES="100000")
    archive = _zip(tmp_path, [("bananawiki.db", b"x"), ("uploads/a", b"1"), ("uploads/b", b"2")])
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("many"), archive)
    assert error.value.code == "too_large"
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as handle:
        handle.writestr("bananawiki.db", os.urandom(200_000))
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("big"), big)
    assert error.value.code == "too_large"


def test_import_refuses_databases_of_other_applications(setup, tmp_path):
    runtime, _agent, _spec, _root = setup
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("PRAGMA application_id = 1234")
    archive = tmp_path / "other.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(other, "bananawiki.db")
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("alien"), archive)
    assert error.value.code == "archive_invalid"


def test_destination_mapping():
    assert destination("storage/uploads/x/y.png", "") == "storage/uploads/x/y.png"
    assert destination("slug/uploads/a", "slug/") == "storage/uploads/a"
    assert destination("assets/attachments/a", "") == "storage/attachments/a"
    assert destination("favicons/custom_1.png", "") == "favicons/custom_1.png"
    assert destination("favicons/default.png", "") is None
    assert destination("external_plugins/p/plugin.py", "") is None
    assert destination(".secret_key", "") is None


def test_duplicate_copies_data_and_revokes_credentials(setup):
    runtime, agent, spec, root = setup
    with sqlite3.connect(root / "bananawiki.db") as conn:
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute("INSERT INTO api_service__tokens (user_id, token_hash) VALUES (?, 'h')", (user_id,))
    target = replace(make_spec("clone"), instance_id="idclone")
    runtime.duplicate(spec, target)
    clone = Path(runtime._cfg().instances_dir) / "clone"
    assert (clone / "storage" / "uploads" / "photo.png").read_bytes() == b"\x89PNG data"
    with sqlite3.connect(clone / "bananawiki.db") as conn:
        assert conn.execute("SELECT active FROM api_service__tokens").fetchone()[0] == 0
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "owner1"
    with sqlite3.connect(root / "bananawiki.db") as conn:
        assert conn.execute("SELECT active FROM api_service__tokens").fetchone()[0] == 1, "the source is untouched"
    assert "clone" in agent.containers
    with pytest.raises(RuntimeFailure) as error:
        runtime.duplicate(spec, target)
    assert error.value.code == "data_exists"


def test_export_of_a_planted_database_link_fails_safely(setup, tmp_path):
    runtime, _agent, spec, root = setup
    (root / "bananawiki.db").unlink()
    (root / "bananawiki.db").symlink_to(tmp_path / "elsewhere.db")
    with pytest.raises(RuntimeFailure) as error:
        _export(runtime, spec, tmp_path)
    assert error.value.code == "db_unsafe"
