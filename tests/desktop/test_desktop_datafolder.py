"""The data folder: layout, safety checks, lock, secret key and wiki environment."""

from __future__ import annotations

import json
import os
import stat

import pytest

from bananawiki.desktop import DesktopError
from bananawiki.desktop.datafolder import ASSET_DIRECTORIES, LAYOUT_DIRECTORIES, MARKER, DataFolder
from bananawiki.wiki.config import load_config


def test_prepare_creates_the_1x_layout_and_marker(folder):
    folder.prepare()
    for name in LAYOUT_DIRECTORIES:
        assert (folder.root / name).is_dir()
    assert json.loads((folder.root / MARKER).read_text())["product"] == "BananaWiki"
    folder.prepare()  # idempotent


def test_prepare_removes_1x_debug_leftovers(folder):
    folder.root.mkdir()
    (folder.root / "instance").mkdir()
    (folder.root / "_startup_marker.txt").write_text("main() reached")
    folder.prepare()
    assert not (folder.root / "_startup_marker.txt").exists()


def test_a_folder_with_other_files_is_refused(tmp_path):
    (tmp_path / "homework.docx").write_text("x")
    with pytest.raises(DesktopError) as caught:
        DataFolder(tmp_path).prepare()
    assert caught.value.key == "folder_not_empty"
    assert not (tmp_path / "instance").exists()


def test_a_folder_of_another_program_is_refused(folder):
    folder.root.mkdir()
    (folder.root / MARKER).write_text(json.dumps({"product": "Other"}))
    with pytest.raises(DesktopError) as caught:
        folder.check()
    assert caught.value.key == "folder_foreign"


def test_a_1x_folder_without_marker_is_accepted(folder):
    (folder.root / "instance").mkdir(parents=True)
    (folder.root / "uploads").mkdir()
    folder.prepare()
    assert (folder.root / MARKER).is_file()


def test_filesystem_root_and_files_are_refused(tmp_path):
    with pytest.raises(DesktopError):
        DataFolder("/").check()
    (tmp_path / "file").write_text("x")
    with pytest.raises(DesktopError) as caught:
        DataFolder(tmp_path / "file").check()
    assert caught.value.key == "folder_invalid"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_folder_and_subfolders_are_refused(tmp_path, folder):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(DesktopError):
        DataFolder(link).check()
    folder.root.mkdir()
    (folder.root / "uploads").symlink_to(target)
    with pytest.raises(DesktopError) as caught:
        folder.prepare()
    assert caught.value.key == "unsafe_path"


def test_lock_is_exclusive_and_released(folder):
    with folder.lock():
        with pytest.raises(DesktopError) as caught, DataFolder(folder.root).lock():
            pass
        assert caught.value.key == "folder_in_use"
    with folder.lock():
        pass


def test_secret_key_is_generated_once_and_private(folder):
    key = folder.secret_key()
    assert len(key) == 64
    assert folder.secret_key() == key
    if os.name == "posix":
        assert stat.S_IMODE(folder.secret_key_file.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_readable_1x_key_is_kept_but_made_private(folder):
    folder.instance.mkdir(parents=True)
    folder.secret_key_file.write_text("legacy-key\n")
    os.chmod(folder.secret_key_file, 0o644)
    assert folder.secret_key() == "legacy-key"
    assert stat.S_IMODE(folder.secret_key_file.stat().st_mode) == 0o600


def test_an_empty_key_file_is_an_error_not_silently_replaced(folder):
    folder.instance.mkdir(parents=True)
    folder.secret_key_file.write_text("")
    with pytest.raises(DesktopError) as caught:
        folder.secret_key()
    assert caught.value.key == "secret_key_invalid"


def test_environ_points_every_path_into_the_folder(folder):
    environ = folder.environ(host="127.0.0.1", port=8080, language="it")
    assert environ["BW_EASY_DEPLOYMENT"] == "1"
    assert environ["BW_HOST"] == "127.0.0.1"
    assert environ["BW_DEFAULT_INTERFACE_LANGUAGE"] == "it"
    cfg = load_config(environ, secret_key="k" * 64)
    assert cfg.easy_deployment and cfg.instance_dir == str(folder.instance)
    assert cfg.database_path == str(folder.database)
    for name in ASSET_DIRECTORIES:
        assert getattr(cfg.folders, name) == str(folder.root / name)
    for path in (cfg.database_path, cfg.log_file, *vars(cfg.folders).values()):
        assert os.path.commonpath([path, str(folder.root)]) == str(folder.root)


def test_environ_ignores_the_users_secret_key(folder, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "from-the-shell-" + "x" * 40)
    monkeypatch.setenv("BW_DATABASE_PATH", "/elsewhere.db")
    environ = folder.environ(host="127.0.0.1", port=80, language="en")
    assert "SECRET_KEY" not in environ
    assert environ["BW_DATABASE_PATH"] == str(folder.database)


def test_setup_done_reads_the_database(wiki_folder):
    from .conftest import set_setting

    assert wiki_folder.setup_done() is False
    set_setting(wiki_folder, "UPDATE site_settings SET setup_done = 1 WHERE id = 1")
    assert wiki_folder.setup_done() is True
    assert DataFolder(wiki_folder.root.parent / "none").setup_done() is False
