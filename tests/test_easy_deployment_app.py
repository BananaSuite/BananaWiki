import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easy_deployment_app import main as easy  # noqa: E402
from scripts import build_easy_deployment as builder  # noqa: E402


def test_portable_environment_points_mutable_paths_at_data_folder(tmp_path):
    tmp_path = tmp_path / "portable"
    env = easy.portable_environment(tmp_path, host="127.0.0.1", port=5010)

    assert env["BW_HOST"] == "127.0.0.1"
    assert env["BW_PORT"] == "5010"
    assert env["BW_PROXY_MODE"] == "0"
    assert env["BW_DATABASE_PATH"] == str(tmp_path / "instance" / "bananawiki.db")
    assert env["BW_UPLOAD_FOLDER"] == str(tmp_path / "uploads")
    assert env["BW_ATTACHMENT_FOLDER"] == str(tmp_path / "attachments")
    assert env["BW_CUSTOM_PAGE_FILES_FOLDER"] == str(tmp_path / "custom_page_files")
    assert env["BW_FAVICON_UPLOAD_FOLDER"] == str(tmp_path / "favicons")
    assert env["BW_LOG_FILE"] == str(tmp_path / "logs" / "bananawiki.log")


def test_portable_environment_ignores_parent_secret_key(tmp_path, monkeypatch):
    tmp_path = tmp_path / "portable"
    monkeypatch.setenv("SECRET_KEY", "host-secret-that-should-not-leak")

    env = easy.portable_environment(tmp_path, host="127.0.0.1", port=5010)

    assert "SECRET_KEY" not in env


def test_portable_environment_does_not_set_wiki_language_by_default(tmp_path, monkeypatch):
    tmp_path = tmp_path / "portable"
    monkeypatch.delenv("BW_DEFAULT_INTERFACE_LANGUAGE", raising=False)

    env = easy.portable_environment(tmp_path, host="127.0.0.1", port=5010)

    assert env["BW_EASY_DEPLOYMENT"] == "1"
    assert "BW_DEFAULT_INTERFACE_LANGUAGE" not in env


def test_portable_environment_preserves_explicit_wiki_language_env(tmp_path, monkeypatch):
    tmp_path = tmp_path / "portable"
    monkeypatch.setenv("BW_DEFAULT_INTERFACE_LANGUAGE", "it")

    env = easy.portable_environment(tmp_path, host="127.0.0.1", port=5010)

    assert env["BW_EASY_DEPLOYMENT"] == "1"
    assert env["BW_DEFAULT_INTERFACE_LANGUAGE"] == "it"


def test_portable_environment_sets_explicit_wiki_language(tmp_path, monkeypatch):
    tmp_path = tmp_path / "portable"
    monkeypatch.delenv("BW_DEFAULT_INTERFACE_LANGUAGE", raising=False)

    env = easy.portable_environment(
        tmp_path,
        host="127.0.0.1",
        port=5010,
        wiki_language="it",
    )

    assert env["BW_EASY_DEPLOYMENT"] == "1"
    assert env["BW_DEFAULT_INTERFACE_LANGUAGE"] == "it"


def test_ensure_data_layout_creates_expected_directories(tmp_path):
    tmp_path = tmp_path / "portable"
    easy.ensure_data_layout(tmp_path)

    for name in (
        "instance",
        "uploads",
        "attachments",
        "chat_attachments",
        "kanban_attachments",
        "custom_page_files",
        "favicons",
        "tts",
        "logs",
        "tmp_exports",
    ):
        assert (tmp_path / name).is_dir()


def test_has_previous_session_detects_completed_setup(tmp_path):
    tmp_path = tmp_path / "portable"
    easy.ensure_data_layout(tmp_path)
    db_path = easy.wiki_database_path(tmp_path)

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE site_settings (id INTEGER PRIMARY KEY, setup_done INTEGER)")
        conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")

    assert easy.has_previous_session(tmp_path) is True


def test_server_command_uses_source_entrypoint_when_not_frozen(monkeypatch):
    monkeypatch.setattr(easy, "is_frozen", lambda: False)

    cmd = easy.server_command(
        root=Path("/tmp/BananaWiki Files"),
        host="127.0.0.1",
        port=5012,
    )

    assert cmd[0] == sys.executable
    assert cmd[1].endswith(os.path.join("easy_deployment_app", "main.py"))
    assert cmd[2] == "--server-child"
    assert "--data-dir" in cmd
    assert "--host" in cmd
    assert "--port" in cmd
    assert "--language" not in cmd
    assert "--wiki-language" not in cmd


def test_server_command_supports_explicit_wiki_language(monkeypatch):
    monkeypatch.setattr(easy, "is_frozen", lambda: False)

    cmd = easy.server_command(
        root=Path("/tmp/BananaWiki Files"),
        host="127.0.0.1",
        port=5012,
        wiki_language="it",
    )

    assert cmd[-2:] == ["--wiki-language", "it"]


def test_visit_url_is_language_neutral():
    assert easy.visit_url(5001) == "http://127.0.0.1:5001/"


def test_lan_visit_url_requires_ip_address():
    assert easy.lan_visit_url(5001, "192.168.1.20") == "http://192.168.1.20:5001/"
    assert easy.lan_visit_url(5001, None) is None


def test_address_message_for_computer_only_mode():
    assert easy.address_message(port=5001, shared=False, ip_address="192.168.1.20") == (
        "Teacher computer: http://127.0.0.1:5001/"
    )


def test_address_message_for_classroom_network_mode():
    message = easy.address_message(port=5001, shared=True, ip_address="192.168.1.20")

    assert "Teacher computer: http://127.0.0.1:5001/" in message
    assert "Kids' devices: http://192.168.1.20:5001/" in message


def test_address_info_prefers_classroom_url_when_shared():
    info = easy.address_info(port=5001, shared=True, ip_address="192.168.1.20")

    assert info.local_url == "http://127.0.0.1:5001/"
    assert info.classroom_url == "http://192.168.1.20:5001/"


def test_candidate_ipv4_addresses_from_text_filters_unusable_addresses():
    text = """
    inet 127.0.0.1 netmask 255.0.0.0
    inet 192.168.50.22
    inet 0.0.0.0
    inet 169.254.10.20
    inet 224.0.0.1
    Default Gateway . . . . . . . . . : 192.168.50.1
    DNS Servers . . . . . . . . . . . : 192.168.50.1
    """

    assert easy._candidate_ipv4_addresses_from_text(text) == [
        "192.168.50.22",
        "169.254.10.20",
    ]


def test_usable_lan_ip_rejects_public_and_loopback_addresses():
    assert easy._is_usable_lan_ip("192.168.1.10") is True
    assert easy._is_usable_lan_ip("10.0.0.5") is True
    assert easy._is_usable_lan_ip("169.254.4.5") is True
    assert easy._is_usable_lan_ip("127.0.0.1") is False
    assert easy._is_usable_lan_ip("8.8.8.8") is False


def test_linux_portable_package_preserves_executable_mode(tmp_path):
    exe = tmp_path / "BananaWikiEasyDeployment"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o644)

    package = builder.package_portable_artifact(
        system="Linux",
        machine="x86_64",
        dist_dir=tmp_path,
    )

    assert package.name == "BananaWikiEasyDeployment-linux-x86_64.tar.gz"
    with tarfile.open(package) as tf:
        info = tf.getmember("BananaWikiEasyDeployment")
    assert info.mode & stat.S_IXUSR


def test_macos_portable_package_preserves_bundle_executable_mode(tmp_path):
    app_exe = (
        tmp_path
        / "BananaWiki Easy Deployment.app"
        / "Contents"
        / "MacOS"
        / "BananaWikiEasyDeployment"
    )
    app_exe.parent.mkdir(parents=True)
    app_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    app_exe.chmod(0o644)

    package = builder.package_portable_artifact(
        system="Darwin",
        machine="arm64",
        dist_dir=tmp_path,
    )

    assert package.name == "BananaWikiEasyDeployment-macos-arm64.zip"
    with zipfile.ZipFile(package) as zf:
        info = zf.getinfo(
            "BananaWiki Easy Deployment.app/Contents/MacOS/BananaWikiEasyDeployment"
        )
    mode = (info.external_attr >> 16) & 0o777
    assert mode & stat.S_IXUSR


def test_check_internet_detects_no_network(monkeypatch):
    """check_internet should return False when there's no reachable host."""
    import socket

    def mock_connect(self, addr):
        raise OSError("No network")

    monkeypatch.setattr(socket.socket, "connect", mock_connect)
    assert easy.check_internet(timeout=0.01) is False


def test_create_archive_roundtrip(tmp_path):
    tmp_path = tmp_path / "portable"
    easy.ensure_data_layout(tmp_path)
    import db
    import sqlite3
    from contextlib import closing
    with db.get_db_context() as source, closing(sqlite3.connect(easy.wiki_database_path(tmp_path))) as target:
        source.backup(target)
    (tmp_path / "uploads" / "file.txt").write_text("content")
    archive = tmp_path.parent / "export.zip"
    easy.create_data_archive(tmp_path, archive)
    assert archive.exists()
    import zipfile
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "bananawiki.db" in names
        assert "uploads/file.txt" in names


def test_extract_archive_roundtrip(tmp_path):
    tmp_path = tmp_path / "portable"
    easy.ensure_data_layout(tmp_path)
    import db
    import sqlite3
    from contextlib import closing
    with db.get_db_context() as source, closing(sqlite3.connect(easy.wiki_database_path(tmp_path))) as target:
        source.backup(target)
    (tmp_path / "uploads" / "file.txt").write_text("content")
    archive = tmp_path.parent / "export.zip"
    easy.create_data_archive(tmp_path, archive)
    # Clear and restore
    dest = tmp_path / "restored"
    dest.mkdir()
    easy.clear_all_data(dest)
    easy.extract_data_archive(archive, dest)
    assert (dest / "instance" / "bananawiki.db").exists()
    assert (dest / "uploads" / "file.txt").exists()
    assert (dest / "uploads" / "file.txt").read_text() == "content"


def test_clear_all_data_preserves_secret_key(tmp_path):
    tmp_path = tmp_path / "portable"
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(parents=True)
    secret = instance_dir / ".secret_key"
    secret.write_bytes(b"test-secret")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "file.txt").write_text("content")
    easy.clear_all_data(tmp_path)
    assert secret.exists()
    assert secret.read_bytes() == b"test-secret"
    assert not (tmp_path / "uploads" / "file.txt").exists()
    assert (tmp_path / "instance").exists()


def test_windows_portable_package_produces_zip(tmp_path):
    exe = tmp_path / "BananaWikiEasyDeployment.exe"
    exe.write_bytes(b"MZ")

    package = builder.package_portable_artifact(
        system="Windows",
        machine="AMD64",
        dist_dir=tmp_path,
    )

    assert package.name == "BananaWikiEasyDeployment-windows-x86_64.zip"
    with zipfile.ZipFile(package) as zf:
        assert "BananaWikiEasyDeployment.exe" in zf.namelist()
