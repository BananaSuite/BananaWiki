"""Network helpers, preferences, launcher translations and the packaging script."""

from __future__ import annotations

import importlib.util
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from bananawiki.desktop import DesktopError, i18n, network, preferences

ROOT = Path(__file__).resolve().parents[2]


# ── network ───────────────────────────────────────────────────────────────────


def test_bind_host_is_loopback_unless_the_user_opted_in():
    assert network.bind_host(False) == "127.0.0.1"
    assert network.bind_host(True) == "0.0.0.0"


@pytest.mark.parametrize("ip, usable", [
    ("192.168.1.4", True), ("10.0.0.2", True), ("172.16.5.1", True), ("169.254.3.3", True),
    ("127.0.0.1", False), ("8.8.8.8", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("fe80::1", False), ("nonsense", False),
])
def test_usable_lan_addresses(ip, usable):
    assert network.is_usable_lan_ip(ip) is usable


def test_addresses_in_command_output():
    linux = "2: wlan0: <UP>\n    inet 192.168.1.23/24 brd 192.168.1.255 scope global wlan0\n    inet 127.0.0.1/8"
    mac = "en0: flags=8863\n\tinet 10.0.0.7 netmask 0xffffff00 broadcast 10.0.0.255"
    windows = ("   IPv4 Address. . . . . . . . . . . : 192.168.0.10\n"
               "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
               "   Default Gateway . . . . . . . . . : 192.168.0.1")
    assert network.addresses_in_text(linux) == ["192.168.1.23"]
    assert network.addresses_in_text(mac) == ["10.0.0.7"]
    assert network.addresses_in_text(windows) == ["192.168.0.10"]


def test_port_candidates_and_urls():
    assert network.port_candidates(None)[0] == 80
    candidates = network.port_candidates(8080)
    assert candidates[0] == 8080 and candidates.count(8080) == 1
    assert network.url("127.0.0.1", 80) == "http://127.0.0.1/"
    assert network.url("192.168.1.2", 8080, "/setup") == "http://192.168.1.2:8080/setup"


def test_port_available(free_port):
    import socket

    assert network.port_available("127.0.0.1", free_port)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", free_port))
        sock.listen()
        assert not network.port_available("127.0.0.1", free_port)


# ── preferences ───────────────────────────────────────────────────────────────


def test_preferences_round_trip(tmp_path):
    path = tmp_path / "cfg" / "desktop.json"
    prefs = preferences.Preferences(data_dir=str(tmp_path / "wiki"), language="it", share_on_lan=True, port=8080)
    preferences.save(prefs, path)
    assert preferences.load(path) == prefs


def test_invalid_preferences_fall_back_to_safe_defaults(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"data_dir": 5, "language": "xx", "share_on_lan": "yes", "port": 99999}))
    prefs = preferences.load(path)
    assert prefs.share_on_lan is False
    assert prefs.port is None
    assert prefs.language in i18n.LANGUAGES
    assert prefs.data_dir == str(preferences.default_data_dir())
    path.write_text("{broken")
    assert preferences.load(path).share_on_lan is False


def test_default_data_dir_when_running_from_source(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences.Path, "home", lambda: tmp_path)
    assert preferences.default_data_dir() == tmp_path / "BananaWiki"
    (tmp_path / "Documents").mkdir()
    assert preferences.default_data_dir() == tmp_path / "Documents" / "BananaWiki"


def test_packaged_app_keeps_data_beside_itself(monkeypatch, tmp_path):
    app_dir = tmp_path / "USB"
    bundle = app_dir / "BananaWiki.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(preferences.sys, "frozen", True, raising=False)
    monkeypatch.setattr(preferences.sys, "executable", str(bundle / "BananaWiki"))
    assert preferences.executable_dir() == app_dir
    assert preferences.default_data_dir() == app_dir / "bananawiki"


# ── translations ──────────────────────────────────────────────────────────────


def test_catalogues_have_the_same_keys_and_placeholders():
    english, italian = i18n.catalog("en"), i18n.catalog("it")
    assert english.keys() == italian.keys()
    for key, text in english.items():
        assert ("{path}" in text) == ("{path}" in italian[key]), key


def test_every_error_key_raised_in_the_code_is_translated():
    import re

    source = "\n".join(path.read_text() for path in (ROOT / "bananawiki" / "desktop").glob("*.py"))
    keys = set(re.findall(r'DesktopError\("([a-z_]+)"', source))
    assert keys and all(f"error.{key}" in i18n.catalog("en") for key in keys)


def test_translator_formats_errors():
    t = i18n.Translator("it")
    assert t("server.start") == "Avvia"
    assert t("data.backup_done", path="/x.zip").endswith("/x.zip")
    assert "/tmp/x" in t.error(DesktopError("backup_exists", "/tmp/x"))
    assert i18n.Translator("xx").language == "en"


# ── packaging ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("desktop_build", ROOT / "packaging" / "desktop" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spec_uses_a_neutral_bundle_id():
    spec = (ROOT / "packaging" / "desktop" / "bananawiki-desktop.spec").read_text()
    assert 'BUNDLE_ID = "org.bananawiki.desktop"' in spec
    assert "canalescuola" not in spec.lower()


def test_linux_package_is_executable(build, tmp_path):
    binary = tmp_path / "BananaWiki"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o644)
    archive = build.package_artifact(system="Linux", machine="AMD64", dist=tmp_path)
    assert archive.name == "BananaWiki-Desktop-linux-x86_64.tar.gz"
    with tarfile.open(archive) as tar:
        assert tar.getmember("BananaWiki").mode & stat.S_IXUSR


def test_windows_package_is_a_zip(build, tmp_path):
    (tmp_path / "BananaWiki.exe").write_bytes(b"MZ")
    archive = build.package_artifact(system="Windows", machine="AMD64", dist=tmp_path)
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["BananaWiki.exe"]


def test_macos_package_keeps_the_executable_bit(build, tmp_path, monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda _name: None)
    executable = tmp_path / "BananaWiki.app" / "Contents" / "MacOS" / "BananaWiki"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\xcf\xfa")
    executable.chmod(0o644)
    archive = build.package_artifact(system="Darwin", machine="arm64", dist=tmp_path)
    assert archive.name == "BananaWiki-Desktop-macos-arm64.zip"
    with zipfile.ZipFile(archive) as zf:
        mode = zf.getinfo("BananaWiki.app/Contents/MacOS/BananaWiki").external_attr >> 16
    assert mode & stat.S_IXUSR


def test_missing_build_output_is_reported(build, tmp_path):
    with pytest.raises(FileNotFoundError):
        build.package_artifact(system="Linux", machine="x86_64", dist=tmp_path)
