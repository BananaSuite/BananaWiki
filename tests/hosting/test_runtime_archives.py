"""Portable wiki archives: export, strict import (1.4 layouts included) and copies."""

from __future__ import annotations

import errno
import io
import json
import os
import sqlite3
import stat
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananawiki.hosting.runtime import RuntimeFailure, tenantfs
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


def test_export_filename_collision_preserves_the_finished_archive(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import archives

    runtime, _agent, spec, _root = setup
    frozen = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(archives, "datetime", SimpleNamespace(now=lambda _tz: frozen, fromtimestamp=datetime.fromtimestamp))
    path = _export(runtime, spec, tmp_path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        _export(runtime, spec, tmp_path)
    assert path.read_bytes() == original


def test_export_caps_tenant_files_at_the_size_observed_when_opened(setup, tmp_path):
    from bananawiki.hosting.runtime import archives

    runtime, _agent, _spec, root = setup
    asset = root / "storage/uploads/growing.bin"
    asset.write_bytes(b"original")
    fd = os.open(asset, os.O_RDONLY)
    info = os.fstat(fd)
    with asset.open("ab") as output:
        output.write(b"newly appended data")
    path = tmp_path / "growing.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archives.write_fd(archive, fd, info, "uploads/growing.bin", runtime._cfg().archives)
    with zipfile.ZipFile(path) as archive:
        assert archive.read("uploads/growing.bin") == b"original"


def test_export_refuses_a_tenant_file_that_shrank(setup, tmp_path):
    from bananawiki.hosting.runtime import archives

    runtime, _agent, _spec, root = setup
    asset = root / "storage/uploads/shrinking.bin"
    asset.write_bytes(b"original")
    fd = os.open(asset, os.O_RDONLY)
    info = os.fstat(fd)
    asset.write_bytes(b"o")
    with zipfile.ZipFile(tmp_path / "shrinking.zip", "w") as archive, pytest.raises(RuntimeFailure, match="shrank"):
        archives.write_fd(archive, fd, info, "uploads/shrinking.bin", runtime._cfg().archives)


def test_export_waits_for_a_tenant_file_that_is_being_rewritten(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import archives, tenantfs

    runtime, _agent, _spec, root = setup
    asset = root / "storage/uploads/rewritten.bin"
    asset.write_bytes(b"original")
    fd = os.open(asset, os.O_RDONLY)  # write_fd takes it over and closes it
    try:
        info = os.fstat(fd)
        asset.write_bytes(b"")  # the tenant truncates the file, then writes it again
    except BaseException:
        os.close(fd)
        raise
    monkeypatch.setattr(tenantfs, "time", SimpleNamespace(sleep=lambda _seconds: asset.write_bytes(b"replaced")))
    with zipfile.ZipFile(tmp_path / "rewritten.zip", "w") as archive:
        archives.write_fd(archive, fd, info, "uploads/rewritten.bin", runtime._cfg().archives)
    with zipfile.ZipFile(tmp_path / "rewritten.zip") as archive:
        assert archive.read("uploads/rewritten.bin") == b"replaced"


class ScriptedFile:
    """A tenant file as the host reads it: each read returns (or raises) the next scripted result."""

    def __init__(self, *reads):
        self.reads = list(reads)

    def read(self, size):
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result[:size]


def test_a_file_that_shrinks_once_copied_from_is_never_joined_to_new_content(monkeypatch):
    """Retrying after part of the file is copied would read the new content from the old offset: a torn copy."""
    from bananawiki.hosting.runtime import tenantfs

    monkeypatch.setattr(tenantfs, "SHORT_READ_DELAYS", (0, 0, 0))
    target = io.BytesIO()
    with pytest.raises(RuntimeFailure, match="shrank"):
        tenantfs.copy_observed(ScriptedFile(b"old-", b"", b"new-content"), target, 8)
    assert target.getvalue() == b"old-"
    target = io.BytesIO()
    tenantfs.copy_observed(ScriptedFile(b"abcd", BlockingIOError(), b"efgh"), target, 8)
    assert target.getvalue() == b"abcdefgh", "a read that would block is only waited for"


@pytest.mark.parametrize(("folder", "name", "reported"), [
    ("storage/uploads", b"\xff\xfe.png", "storage/uploads/\\xff\\xfe.png"),
    ("external_plugins/tool", b"cache-12:00.json", "external_plugins/tool/cache-12:00.json"),
], ids=["not-utf8", "colon"])
def test_export_leaves_out_tenant_files_no_import_accepts(setup, tmp_path, app_factory, folder, name, reported):
    """A tenant (its plugins) can create a file name of any bytes: ZIP names are UTF-8, and an import refuses
    the whole archive for one name with a ':'. Such a file must not cost the owner the export of the wiki."""
    from werkzeug.datastructures import FileStorage

    from bananawiki.wiki.features.site_admin import migration

    runtime, _agent, spec, root = setup
    with open(os.path.join(os.fsencode(root / folder), name), "wb") as handle:
        handle.write(b"hostile")
    path = _export(runtime, spec, tmp_path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert {"bananawiki.db", "uploads/photo.png", "external_plugins/tool/plugin.py"} <= set(names)
    assert not [member for member in names if ":" in member], "the hosting import would refuse the archive"
    assert manifest["omitted_files"] == [reported] and manifest["omitted_file_count"] == 1, "the owner can be told"

    runtime.import_archive(make_spec("copy"), path)
    copy = Path(runtime._cfg().instances_dir) / "copy"
    assert (copy / "storage" / "uploads" / "photo.png").read_bytes() == b"\x89PNG data"
    wiki = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "standalone")})
    with wiki.app_context():
        staged = migration.stage(FileStorage(io.BytesIO(path.read_bytes()), filename="site.zip"))
    try:
        assert (staged.folders["uploads"] / "photo.png").read_bytes() == b"\x89PNG data"
    finally:
        staged.discard()


def _both_imports(runtime, path: Path, tmp_path, app_factory, folder: str, name: str) -> tuple[bytes, bytes]:
    """The file *folder*/*name* as the hosting import and the standalone import of *path* take it."""
    from werkzeug.datastructures import FileStorage

    from bananawiki.wiki.features.site_admin import migration

    runtime.import_archive(make_spec("copy"), path)
    copy = Path(runtime._cfg().instances_dir) / "copy"
    assert runtime.list_users(make_spec("copy"))[0], "the wiki's own database was imported"
    hosted = (copy / "storage" / folder / name if folder in tenantfs.ASSET_FOLDERS else copy / folder / name
              ).read_bytes()
    wiki = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "standalone")})
    with wiki.app_context():
        staged = migration.stage(FileStorage(io.BytesIO(path.read_bytes()), filename="site.zip"))
    try:
        return hosted, (staged.folders[folder] / name).read_bytes()
    finally:
        staged.discard()


@pytest.mark.parametrize(("hostile", "neighbour"), [
    ("assets/uploads/photo.png", None),
    ("assets/uploads/photo.png/x.png", None),
    ("assets/translations/xx.json", "translations/xx.json"),
    ("storage/translations/xx.json", "translations/xx.json"),
    ("storage/favicons/custom_a.png", "favicons/custom_a.png"),
], ids=["assets-file", "assets-folder", "assets-translation", "storage-translation", "storage-favicon"])
def test_export_leaves_out_tenant_files_imports_take_for_the_wikis_own(setup, tmp_path, app_factory, hostile,
                                                                     neighbour):
    """Imports strip assets/ (the hosting import storage/ as well) from member paths: a tenant file below such a
    folder lands where the wiki's own file goes. Next to it, the hosting import refused the export (duplicate
    files, a path used as a file and a folder) and the standalone import failed or took it instead; flattened
    like an asset folder, storage/translations/xx.json and storage/favicons/custom_a.png took the place of the
    wiki's own files in the export, whichever the walk met first."""
    runtime, _agent, spec, root = setup
    for relative in (hostile, neighbour):
        if relative:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(b'{"own": 1}' if relative == neighbour else b"hostile")
    path = _export(runtime, spec, tmp_path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("uploads/photo.png") == b"\x89PNG data"
        assert neighbour is None or archive.read(neighbour) == b'{"own": 1}'
    assert names.count(neighbour or "uploads/photo.png") == 1
    assert manifest["omitted_files"] == [hostile] and manifest["omitted_file_count"] == 1

    folder, name = (neighbour or "uploads/photo.png").split("/")
    hosted, standalone = _both_imports(runtime, path, tmp_path, app_factory, *(
        ("uploads", "photo.png") if folder == "translations" else (folder, name)))
    assert hosted == standalone == (b"\x89PNG data" if neighbour is None or folder == "translations"
                                    else b'{"own": 1}')
    if folder == "translations":
        # The standalone import takes only valid language packs; the hosting import takes the file as it is.
        copy = Path(runtime._cfg().instances_dir) / "copy"
        assert (copy / neighbour).read_bytes() == b'{"own": 1}'


@pytest.mark.parametrize("inert", ["storage/storage/uploads/photo.png", "storage/assets/uploads/photo.png",
                                   "storage/favicons/other.png"], ids=["storage-storage", "storage-assets", "other"])
def test_export_keeps_storage_paths_no_import_maps(setup, tmp_path, app_factory, inert):
    """Only storage/<asset folder>/ is flattened: below storage/, a path no import maps stays as it is and both
    imports ignore it."""
    runtime, _agent, spec, root = setup
    (root / inert).parent.mkdir(parents=True, exist_ok=True)
    (root / inert).write_bytes(b"inert")
    path = _export(runtime, spec, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert archive.read(inert) == b"inert" and archive.read("uploads/photo.png") == b"\x89PNG data"
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["omitted_files"] == [] and manifest["omitted_file_count"] == 0
    assert _both_imports(runtime, path, tmp_path, app_factory, "uploads", "photo.png") == (b"\x89PNG data",) * 2


def test_export_prefers_the_storage_copy_of_an_asset_folder(setup, tmp_path, app_factory, monkeypatch):
    """A wiki can replace its uploads link with a folder, or its attachments link with a file: the export kept
    whichever file the walk met first, so the top-level copy could replace the wiki's own, and a file named
    attachments left every storage/attachments file out."""
    runtime, _agent, spec, root = setup
    (root / "uploads").unlink()
    (root / "uploads").mkdir()
    (root / "uploads" / "photo.png").write_bytes(b"top-level")
    (root / "uploads" / "only.png").write_bytes(b"only here")
    (root / "attachments").unlink()
    (root / "attachments").write_bytes(b"not a folder")
    (root / "storage" / "attachments" / "doc.pdf").write_bytes(b"%PDF")
    walk = os.fwalk

    def top_level_folders_first(*args, **kwargs):
        # The order a file system may well list them in: storage/ last.
        for dirpath, dirnames, filenames, walk_fd in walk(*args, **kwargs):
            dirnames.sort(reverse=True)
            yield dirpath, dirnames, filenames, walk_fd

    monkeypatch.setattr(os, "fwalk", top_level_folders_first)
    path = _export(runtime, spec, tmp_path)
    monkeypatch.setattr(os, "fwalk", walk)
    with zipfile.ZipFile(path) as archive:
        assert archive.read("uploads/photo.png") == b"\x89PNG data"
        assert archive.read("uploads/only.png") == b"only here", "files only the top-level folder holds still move"
        assert archive.read("attachments/doc.pdf") == b"%PDF"
        manifest = json.loads(archive.read("manifest.json"))
    assert sorted(manifest["omitted_files"]) == ["attachments", "uploads/photo.png"]
    assert _both_imports(runtime, path, tmp_path, app_factory, "uploads", "photo.png") == (b"\x89PNG data",) * 2


def test_a_tenant_file_cannot_take_the_place_of_the_export_manifest(setup, tmp_path):
    """The manifest is written last: a tenant's own manifest.json would otherwise be the one imports read."""
    runtime, _agent, spec, root = setup
    (root / "manifest.json").write_text('{"format_version": 99}')
    path = _export(runtime, spec, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist().count("manifest.json") == 1
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format_version"] == 1 and manifest["omitted_files"] == ["manifest.json"]
    runtime.import_archive(make_spec("copy"), path)


@pytest.mark.parametrize("operation", ["copy_out", "copy_tree"])
def test_host_copies_finish_at_the_observed_size_of_growing_tenant_files(setup, tmp_path, monkeypatch, operation):
    from bananawiki.hosting.runtime import tenantfs

    _runtime, _agent, _spec, root = setup
    asset = root / "storage/uploads/growing.bin"
    asset.write_bytes(b"A" * 100_000)
    original_fstat = tenantfs.os.fstat
    changed = []

    def grow_after_check(fd):
        info = original_fstat(fd)
        if info.st_ino == asset.stat().st_ino:
            changed.append(fd)
            with asset.open("ab") as output:
                output.write(b"B" * 100_000)
        return info

    monkeypatch.setattr(tenantfs.os, "fstat", grow_after_check)
    if operation == "copy_out":
        target = tmp_path / "copy.bin"
        tenantfs.copy_out(root, "storage/uploads/growing.bin", target)
    else:
        tenantfs.copy_tree(root, "storage/uploads", tmp_path / "tree")
        target = tmp_path / "tree/growing.bin"
    assert changed and target.stat().st_size < asset.stat().st_size


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


def _unsupported_zip(tmp_path, *, encrypted=False) -> Path:
    path = _zip(tmp_path, [("bananawiki.db", b"database")])
    raw = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6 if encrypted else 8),
                              (b"PK\x01\x02", 8 if encrypted else 10)):
        position = raw.index(signature) + offset
        raw[position:position + 2] = (1 if encrypted else 99).to_bytes(2, "little")
    path.write_bytes(raw)
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
    (_unsupported_zip, "archive_invalid"),
    (lambda t: _unsupported_zip(t, encrypted=True), "archive_invalid"),
    (lambda t: _zip(t, [("bananawiki.db", b"x"), ("storage/uploads/a", b"1"), ("uploads/a", b"2")]),
     "archive_invalid"),
    (lambda t: _zip(t, [("bananawiki.db", b"x"), ("uploads/folder", b"1"), ("uploads/folder/file", b"2")]),
     "archive_invalid"),
    (lambda t: _zip(t, [("bananawiki.db", b"x"), ("uploads/folder/file", b"2"), ("uploads/folder", b"1")]),
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


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"])
def test_import_refuses_members_whose_decompression_cannot_be_bounded(setup, tmp_path, method):
    """zipfile decompresses bzip2 and LZMA reads without an output limit: a member of a few hundred bytes
    can take gigabytes of memory, whatever size it declares."""
    runtime, _agent, _spec, root = setup
    archive = tmp_path / "packed.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(root / "bananawiki.db", "bananawiki.db", compress_type=method)
        handle.writestr("uploads/a.png", b"a")
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("packed"), archive)
    assert error.value.code == "archive_invalid"
    assert not (Path(runtime._cfg().instances_dir) / "packed").exists()


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EDQUOT])
def test_import_storage_failures_are_reported_and_remove_staging(setup, tmp_path, monkeypatch, failure):
    from bananawiki.hosting.runtime import archives

    runtime, _agent, _spec, _root = setup
    archive = _zip(tmp_path, [("bananawiki.db", b"database")])

    def full(*_arguments):
        raise OSError(failure, "storage full")

    monkeypatch.setattr(archives.os, "fchmod", full)
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("target"), archive)
    assert error.value.code == "no_space"
    base = Path(runtime._cfg().instances_dir)
    assert not (base / "target").exists()
    assert not list(base.glob(".import-*"))


def test_import_rejects_a_large_manifest_before_decompressing_it(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import archives

    runtime, _agent, _spec, _root = setup
    path = _zip(tmp_path, [("manifest.json", b" " * (archives.MAX_MANIFEST_BYTES + 1)), ("bananawiki.db", b"x")])
    original_open = zipfile.ZipFile.open

    def checked_open(self, name, *args, **kwargs):
        assert getattr(name, "filename", name) != "manifest.json", "oversized manifests must never be decompressed"
        return original_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", checked_open)
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("large-manifest"), path)
    assert error.value.code == "archive_invalid"


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
    with pytest.raises(RuntimeFailure) as error:
        runtime.duplicate(make_spec("gone"), target)
    assert error.value.code == "data_exists", "a taken target is reported first: the portal never cleans it up"
    with pytest.raises(RuntimeFailure) as error:
        runtime.duplicate(make_spec("gone"), replace(make_spec("other"), instance_id="idother"))
    assert error.value.code == "not_found"
    assert not (Path(runtime._cfg().instances_dir) / "other").exists()


def test_import_never_cleans_up_a_directory_it_did_not_create(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import tenantfs

    runtime, _agent, spec, _root = setup
    path = _export(runtime, spec, tmp_path)
    late = Path(runtime._cfg().instances_dir) / "late"
    lexists = os.path.lexists
    removed = []

    def race(candidate):
        found = lexists(candidate)
        if Path(candidate) == late and not found:
            # Another directory takes the name between the check and the creation.
            late.mkdir()
            (late / "theirs.txt").write_text("not ours")
        return found

    monkeypatch.setattr(os.path, "lexists", race)
    monkeypatch.setattr(tenantfs, "remove_tree", removed.append)
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("late"), path)
    assert error.value.code == "data_exists", "the portal must not clean up a directory it did not create"
    assert (late / "theirs.txt").read_text() == "not ours"
    assert removed == []


def test_import_reports_unexpected_storage_errors_and_removes_its_folder(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import agent as agent_module

    runtime, _agent, _spec, _root = setup
    archive = _zip(tmp_path, [("bananawiki.db", b"database")])

    def full(*_arguments):
        raise OSError(errno.EDQUOT, "Disk quota exceeded")

    monkeypatch.setattr(agent_module.tenant_archives, "unpack", full)
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("target"), archive)
    assert error.value.code == "no_space"
    assert not (Path(runtime._cfg().instances_dir) / "target").exists()


def test_import_keeps_its_verdict_when_the_cleanup_fails(setup, tmp_path, monkeypatch):
    from bananawiki.hosting.runtime import tenantfs

    runtime, _agent, _spec, _root = setup
    archive = tmp_path / "plain.zip"
    archive.write_bytes(b"not a zip")

    def stuck(_path):
        raise RuntimeFailure("failed", "could not delete the import folder")

    monkeypatch.setattr(tenantfs, "remove_tree", stuck)
    with pytest.raises(RuntimeFailure) as error:
        runtime.import_archive(make_spec("stuck"), archive)
    assert error.value.code == "archive_invalid", "a failed cleanup does not replace the verdict"


def test_export_of_a_planted_database_link_fails_safely(setup, tmp_path):
    runtime, _agent, spec, root = setup
    (root / "bananawiki.db").unlink()
    (root / "bananawiki.db").symlink_to(tmp_path / "elsewhere.db")
    with pytest.raises(RuntimeFailure) as error:
        _export(runtime, spec, tmp_path)
    assert error.value.code == "db_unsafe"
