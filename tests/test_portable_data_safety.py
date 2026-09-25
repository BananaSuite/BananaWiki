"""A failed or hostile portable restore must leave the running data intact."""

from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import threading
import zipfile

import pytest

import db
from easy_deployment_app import data


@pytest.fixture
def portable(tmp_path):
    root = tmp_path / "portable"
    data.ensure_data_layout(root)
    with db.get_db_context() as source, closing(sqlite3.connect(data.wiki_database_path(root))) as target:
        source.backup(target)
    (root / "instance/.secret_key").write_bytes(b"application key kept with its database")
    (root / "uploads/kept.txt").write_text("existing customer content")
    return root


def test_export_restore_keeps_application_key_plugins_and_previous_data(portable, tmp_path):
    (portable / "plugins/private_plugin").mkdir()
    (portable / "plugins/private_plugin/settings.json").write_text('{"enabled":true}')
    archive = tmp_path / "export.zip"
    data.create_data_archive(portable, archive)
    if os.name != "nt":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    (portable / "uploads/kept.txt").write_text("changed after backup")
    previous = data.extract_data_archive(archive, portable)
    assert (portable / "uploads/kept.txt").read_text() == "existing customer content"
    assert (previous / "uploads/kept.txt").read_text() == "changed after backup"
    assert (portable / "instance/.secret_key").read_bytes() == (previous / "instance/.secret_key").read_bytes()
    assert (portable / "plugins/private_plugin/settings.json").is_file()


@pytest.mark.parametrize("member", [
    "uploads/../../outside.txt", "/uploads/outside.txt", "uploads\\..\\outside.txt",
    "uploads/C:/outside.txt", "uploads/./ambiguous.txt", "uploads/CON.txt",
    "uploads/ambiguous.", "plugins/../outside.py", "instance/unexpected.key",
])
def test_unsafe_archive_paths_preserve_current_data(portable, tmp_path, member):
    before = data.wiki_database_path(portable).read_bytes()
    archive = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.write(data.wiki_database_path(portable), "bananawiki.db")
        target.writestr(member, "hostile data")
    with pytest.raises(ValueError):
        data.extract_data_archive(archive, portable)
    assert data.wiki_database_path(portable).read_bytes() == before
    assert (portable / "uploads/kept.txt").read_text() == "existing customer content"
    assert not (tmp_path / "outside.txt").exists()


def test_links_and_case_collisions_are_rejected_before_replacement(portable, tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.write(data.wiki_database_path(portable), "bananawiki.db")
        link = zipfile.ZipInfo("uploads/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="links"):
        data.extract_data_archive(archive, portable)
    with zipfile.ZipFile(archive, "w") as target:
        target.write(data.wiki_database_path(portable), "bananawiki.db")
        target.writestr("uploads/Name.txt", "one")
        target.writestr("uploads/name.txt", "two")
    with pytest.raises(ValueError, match="ambiguous"):
        data.extract_data_archive(archive, portable)
    assert (portable / "uploads/kept.txt").read_text() == "existing customer content"


def test_size_limits_and_invalid_database_never_erase_current_installation(portable, tmp_path, monkeypatch):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("bananawiki.db", b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        data.extract_data_archive(archive, portable)
    data.create_data_archive(portable, tmp_path / "valid.zip")
    monkeypatch.setattr(data, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="size"):
        data.extract_data_archive(tmp_path / "valid.zip", portable)
    assert (portable / "uploads/kept.txt").read_text() == "existing customer content"


def _crash_between_folder_moves(root, archive, ready, after_install):
    original = data.os.replace
    def replace(source, destination):
        result = original(source, destination)
        source, destination = Path(source), Path(destination)
        moved_original = source == root and ".previous-" in destination.name
        installed_new = destination == root and ".restore-" in source.name
        if (installed_new if after_install else moved_original):
            ready.set()
            threading.Event().wait(30)
        return result
    data.os.replace = replace
    data.extract_data_archive(archive, root)


@pytest.mark.parametrize("after_install", [False, True])
def test_process_death_during_restore_recovers_previous_folder(portable, tmp_path, after_install):
    archive = tmp_path / "valid.zip"
    data.create_data_archive(portable, archive)
    (portable / "uploads/kept.txt").write_text("keep this newer data after an interrupted restore")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_crash_between_folder_moves, args=(portable, archive, ready, after_install))
    try:
        process.start()
        assert ready.wait(10)
        process.kill()
        process.join(timeout=5)
        assert process.exitcode != 0
        data.ensure_data_layout(portable)
        assert (portable / "uploads/kept.txt").read_text() == "keep this newer data after an interrupted restore"
        assert not data._journal_path(portable).exists()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def _hold_server_data_lock(root, ready):
    with data.data_lock(root):
        ready.set()
        threading.Event().wait(30)


def test_another_server_process_prevents_reset(portable):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_hold_server_data_lock, args=(portable, ready))
    try:
        process.start()
        assert ready.wait(10)
        with pytest.raises(RuntimeError, match="Stop every Wiki"):
            data.clear_all_data(portable)
        assert (portable / "uploads/kept.txt").read_text() == "existing customer content"
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)


def test_reset_restarts_as_new_install_and_cannot_wipe_an_unrelated_folder(portable, tmp_path, monkeypatch):
    import config
    data.clear_all_data(portable)
    assert not data.wiki_database_path(portable).exists()
    assert not Path(str(data.wiki_database_path(portable)) + ".initialized").exists()
    monkeypatch.setattr(config, "DATABASE_PATH", str(data.wiki_database_path(portable)))
    db.init_db()
    assert db.get_site_settings()["setup_done"] == 0
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "important.txt").write_text("do not remove")
    with pytest.raises(ValueError):
        data.clear_all_data(unrelated)
    assert (unrelated / "important.txt").read_text() == "do not remove"
