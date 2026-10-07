"""Snapshots, packages, units, Caddy, Git sources, install and the host layer."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, git  # type: ignore[import-not-found]

from bananawiki.ops import MANAGED_MARKER, caddy, profile, units
from bananawiki.ops.files import (
    absolute_path,
    extract_archive,
    read_environment,
    read_json,
    read_package,
    regular_files,
    write_environment,
    write_package,
)
from bananawiki.ops.manager import Manager
from bananawiki.ops.snapshot import Snapshot
from bananawiki.ops.source import GitSource, valid_branch, valid_url
from bananawiki.ops.system import System

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "host")


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return Upstream(tmp_path)


def shipped(relative: str) -> Path:
    """A file shipped at the repository root."""
    return REPO / relative


# Snapshots -----------------------------------------------------------------------


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "opt/bananawiki"
    for name in ("config", "data", "site", "staging", "backups"):
        (root / name).mkdir(parents=True)
    return root


def test_snapshot_links_immutable_files_and_copies_databases_online(tmp_path):
    root = make_root(tmp_path)
    upload = root / "data/uploads/big.bin"
    upload.parent.mkdir()
    upload.write_bytes(b"u" * 100_000)
    (root / "data/logs").mkdir()
    (root / "data/logs/app.log").write_text("line\n" * 20_000)
    writer = sqlite3.connect(root / "data/bananawiki.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("INSERT INTO t VALUES ('committed')")
    writer.commit()  # the writer stays open: the WAL is not checkpointed
    snapshot = Snapshot.create(root)
    index = read_json(snapshot.path / "index.json")["files"]
    assert index["data/uploads/big.bin"]["method"] == "link"
    assert (snapshot.path / "data/uploads/big.bin").stat().st_ino == upload.stat().st_ino
    assert index["data/logs/app.log"]["method"] == "copy"
    assert index["data/bananawiki.db"]["method"] == "sqlite"
    assert "data/bananawiki.db-wal" not in index
    copy = sqlite3.connect(snapshot.path / "data/bananawiki.db")
    assert copy.execute("SELECT v FROM t").fetchall() == [("committed",)]
    copy.close()

    writer.execute("INSERT INTO t VALUES ('during quiesce')")
    writer.commit()
    writer.close()
    (root / "data/logs/app.log").write_text("changed\n")
    upload.unlink()
    (root / "data/uploads/new.bin").write_bytes(b"n" * 70_000)
    counts = snapshot.refresh()
    assert counts["removed"] == 1 and counts["updated"] >= 3
    assert not (snapshot.path / "data/uploads/big.bin").exists()
    copy = sqlite3.connect(snapshot.path / "data/bananawiki.db")
    assert len(copy.execute("SELECT v FROM t").fetchall()) == 2
    copy.close()
    names = [name for _path, name in snapshot.inputs()]
    assert "data/uploads/new.bin" in names and names == sorted(names)
    restored = tmp_path / "restored"
    snapshot.copy_tree("data", restored)
    assert (restored / "logs/app.log").read_text() == "changed\n"
    snapshot.remove()
    assert not snapshot.path.exists()


def test_snapshot_refuses_symlinks_in_data(tmp_path):
    root = make_root(tmp_path)
    (root / "data/link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="regular files"):
        Snapshot.create(root)
    assert not list((root / "staging").glob("snapshot-*"))


def test_snapshot_open_rejects_foreign_paths(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(ValueError):
        Snapshot.open(tmp_path / "elsewhere", root)


# Packages and files --------------------------------------------------------------


def test_package_round_trip_and_tamper_detection(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    package = write_package(tmp_path / "p.tar.gz", {"product": "BananaWiki"}, [(source / "a.txt", "data/a.txt")])
    assert package.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="never overwritten"):
        write_package(package, {"product": "BananaWiki"}, [])
    with read_package(package, "BananaWiki", tmp_path / "stage") as (tree, manifest):
        assert (tree / "data/a.txt").read_text() == "hello" and manifest["schema"] == 1
    with pytest.raises(ValueError, match="does not match the application"), \
            read_package(package, "BananaChat", tmp_path / "stage"):
        pass
    tampered = tmp_path / "t.tar.gz"
    with tarfile.open(package) as archive, tarfile.open(tampered, "w:gz") as output:
        for member in archive.getmembers():
            data = archive.extractfile(member).read()
            if member.name == "data/a.txt":
                data = b"HELLO"
            member.size = len(data)
            import io
            output.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="integrity"), read_package(tampered, "BananaWiki", tmp_path / "stage"):
        pass


def test_extract_rejects_traversal_and_links(tmp_path):
    import io

    for name, kind in (("../evil", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
        archive_path = tmp_path / f"{kind!r}.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            archive.addfile(info, io.BytesIO(b""))
        target = tmp_path / f"out{kind!r}"
        with pytest.raises(ValueError):
            extract_archive(archive_path, target)


def test_environment_files_and_paths(tmp_path):
    write_environment(tmp_path / "app.env", {"A_B": 'va"lue with spaces', "EMPTY": ""})
    assert read_environment(tmp_path / "app.env") == {"A_B": 'va"lue with spaces', "EMPTY": ""}
    with pytest.raises(ValueError):
        write_environment(tmp_path / "x.env", {"bad": "1"})
    with pytest.raises(ValueError):
        write_environment(tmp_path / "x.env", {"A": "two\nlines"})
    with pytest.raises(ValueError):
        absolute_path("/opt")
    with pytest.raises(ValueError):
        absolute_path("/opt/with space/x")
    assert list(regular_files(tmp_path / "missing")) == []


# Profile, units and Caddy ----------------------------------------------------------


def settings_for(mode: str) -> dict:
    return {"schema": 1, "product": "BananaWiki", "mode": mode, "root": "/opt/bananawiki", "service": "bananawiki",
            "domain": "example.org", "portal_domain": "portal.example.org", "port": 5099 if mode == "hosting" else 5001,
            "source_url": "git@github.com:someone/fork.git", "revision": "a" * 40}


def test_service_commands_keep_the_1x_contract():
    wiki = {service.name: service.command for service in profile.services(settings_for("wiki"))}
    assert wiki["bananawiki"][1:] == ["-c", "gunicorn.conf.py", "wsgi:app"]
    assert wiki["bananawiki-tts"][1:] == ["scripts/tts_worker.py"]
    hosting = profile.services(settings_for("hosting"), profile.ReleaseFeatures(True, True))
    names = [service.name for service in hosting]
    assert names == ["bananawiki-agent", "bananawiki", "bananawiki-maintenance"]
    assert hosting[1].command[-1] == "hosting.wsgi:app"
    assert hosting[2].command[1:] == ["-m", "hosting.maintenance", "--interval", "300"]
    assert profile.services(settings_for("hosting"))[0].name == "bananawiki"


def test_environment_defaults_never_override_operator_values():
    values = profile.environment(settings_for("wiki"), {"BW_PORT": "7000", "BW_SETUP_TOKEN": "mine"})
    assert values["BW_PORT"] == "7000" and values["BW_SETUP_TOKEN"] == "mine"  # noqa: S105
    assert values["BW_SOURCE_URL"] == "https://github.com/someone/fork"
    assert values["BANANA_MAINTENANCE_FILE"] == "/opt/bananawiki/data/.banana-maintenance"
    hosting = profile.environment(settings_for("hosting"), {"HOSTING_CONTAINER_IMAGE": "old",
                                                              "BW_RUNTIME_AGENT_SOCKET": "/x"})
    assert hosting["HOSTING_CONTAINER_IMAGE"] == "bananawiki-tenant:" + "a" * 40
    assert "BW_RUNTIME_AGENT_SOCKET" not in hosting


def test_validators():
    assert profile.validate_domain("Wiki.Example.ORG.") == "wiki.example.org"
    for bad in ("http://x.org", "localhost", "a..b", "x.org/path"):
        with pytest.raises(ValueError):
            profile.validate_domain(bad)
    with pytest.raises(ValueError):
        profile.validate_port(80)
    for bad in ("Root", "a", "-x", "x" * 60):
        with pytest.raises(ValueError):
            profile.validate_service_name(bad)
    for bad in ("main..x", "-x", "a/.b", "x.lock", "a b"):
        with pytest.raises(ValueError):
            valid_branch(bad)
    for bad in ("https://user:pw@example.org/r.git", "https://token@example.org/r.git", "file:///etc",
                "ext::sh -c id", "http://example.org/r.git"):
        with pytest.raises(ValueError):
            valid_url(bad)
    assert valid_url("git@github.com:o/r.git") and valid_url("ssh://git@example.org/o/r.git")


def test_hardened_and_legacy_units():
    settings = settings_for("wiki")
    service = profile.services(settings)[0]
    hardened = units.service_unit(settings, service, profile.ReleaseFeatures(True, True))
    for directive in ("NoNewPrivileges=true", "ProtectSystem=strict", "PrivateTmp=true", "CapabilityBoundingSet=\n",
                      "ReadWritePaths=/opt/bananawiki/data", "SystemCallFilter=@system-service", "User=bananawiki"):
        assert directive in hardened
    legacy = units.service_unit(settings_for("hosting"), profile.services(settings_for("hosting"))[0],
                                profile.ReleaseFeatures())
    assert legacy.startswith(MANAGED_MARKER + "\n[Unit]\nDescription=BananaWiki hosting\n")
    assert "SupplementaryGroups=docker" in legacy
    assert units.is_managed(hardened, Path("/opt/bananawiki"))
    assert not units.is_managed(hardened, Path("/srv/other"))


def test_shipped_examples_match_the_generators():
    for mode in ("wiki", "hosting", "compose"):
        assert shipped(f"deploy/Caddyfile.{mode}").read_text() == caddy.example(mode), mode
    for name, text in units.examples().items():
        assert shipped(f"deploy/systemd/{name}").read_text() == text, name


def test_hosting_unprivileged_units_filter_quota_ioctl_before_application():
    features = profile.ReleaseFeatures(True, True, True)
    hosting = settings_for("hosting")
    rendered = {
        service.name: (service, units.service_unit(hosting, service, features))
        for service in profile.services(hosting, features)
    }
    assert len(rendered) == 3
    for service, text in rendered.values():
        if service.privileged:
            assert "SystemCallFilter=~ioctl\n" not in text
            assert "SystemCallFilter=quotactl_fd\n" in text
        else:
            assert "SystemCallFilter=~ioctl\n" not in text
            assert "InaccessiblePaths=/proc\n" in text
            assert "CapabilityBoundingSet=\n" in text
            mode = "maintenance" if service.name.endswith("-maintenance") else "portal"
            assert service.command == ["/opt/bananawiki/current/.venv/bin/python", "-E", "-s", "-m",
                                       "bananawiki.ops.hosting_entrypoint", mode]
    wiki = settings_for("wiki")
    for service in profile.services(wiki, features):
        text = units.service_unit(wiki, service, features)
        assert "SystemCallFilter=~ioctl\n" not in text
        assert "InaccessiblePaths=/proc\n" not in text


def test_caddyfile_hardening():
    hosting = caddy.render(settings_for("hosting"))
    assert "ask http://127.0.0.1:5099/internal/domains/authorize" in hosting
    assert "respond @internal 404" in hosting and hosting.count("import bananawiki_public") == 3
    assert "Strict-Transport-Security" in hosting and "header_up -X-Forwarded-Prefix" in hosting
    wiki = caddy.render(settings_for("wiki"), email="ops@example.org")
    assert "email ops@example.org" in wiki and "flush_interval -1" in wiki
    with pytest.raises(ValueError):
        caddy.render({**settings_for("wiki"), "domain": ""})


# Git sources ----------------------------------------------------------------------


def test_token_credentials_use_askpass_and_strict_permissions(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    token = root / "config/repo.token"
    token.write_text("secret-token\n")
    token.chmod(0o644)
    source = GitSource(root, {"url": "https://example.org/r.git", "branch": "main", "auth": "token",
                              "username": "deploy"})
    with pytest.raises(ValueError, match="0600"):
        source.environment()
    token.chmod(0o600)
    environment = source.environment()
    assert "secret-token" not in json.dumps(environment)
    helper = Path(environment["GIT_ASKPASS"])
    assert helper.stat().st_mode & 0o777 == 0o700
    output = subprocess.run([helper, "Password for https://example.org"], env={**environment}, capture_output=True,
                            text=True, check=True).stdout.strip()
    assert output == "secret-token"


def test_configure_source_moves_and_clears_credentials(tmp_path):
    manager = Manager(tmp_path / "opt/bananawiki", system=FakeSystem(tmp_path / "host"))
    manager.layout()
    token = tmp_path / "token"
    token.write_text("abc")
    first = manager.configure_source(url="https://github.com/o/r.git", token_file=token)
    assert first["auth"] == "token" and (manager.config_dir / "repo.token").stat().st_mode & 0o777 == 0o600
    moved = manager.configure_source(url="https://gitlab.example.org/o/r.git")
    assert moved["auth"] == "none" and not (manager.config_dir / "repo.token").exists()
    key = tmp_path / "key"
    key.write_text("-----BEGIN KEY-----\nabc\n-----END KEY-----")
    with pytest.raises(ValueError, match="known-hosts"):
        manager.configure_source(url="git@github.com:o/r.git", ssh_key=key)
    with pytest.raises(ValueError, match="SSH repository URL"):
        manager.configure_source(ssh_key=key, known_hosts=key)
    ssh = manager.configure_source(url="git@github.com:o/r.git", ssh_key=key, known_hosts=key)
    assert ssh["auth"] == "ssh" and (manager.config_dir / "repo.key").is_file()


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_signature_requirement(tmp_path, upstream):
    key = tmp_path / "signing"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    unsigned = upstream.commit("unsigned")
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    signers = root / "config/repo.allowed_signers"
    signers.write_text("t@example.org " + (tmp_path / "signing.pub").read_text())
    signers.chmod(0o600)
    source = GitSource(root, {"url": str(upstream.bare), "branch": "main", "signing": "ssh"})
    with pytest.raises(RuntimeError, match="not signed"):
        source.resolve(unsigned)
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "--quiet", "--allow-empty", "-S",
        "-m", "signed", cwd=upstream.work)
    git("push", "--quiet", str(upstream.bare), "HEAD:refs/heads/main", cwd=upstream.work)
    sha, _branch, forward = source.resolve(unsigned)
    assert forward and sha != unsigned


def test_fallback_branch_and_missing_branch(tmp_path, upstream):
    first = upstream.commit("one")
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    source = GitSource(root, {"url": str(upstream.bare), "branch": "gone", "fallback_branch": "main"})
    assert source.resolve(first) == (first, "main", True)
    with pytest.raises(RuntimeError, match="deleted"):
        GitSource(root, {"url": str(upstream.bare), "branch": "gone"}).resolve(first)


# Fresh install ---------------------------------------------------------------------


def test_fresh_install_from_checkout(tmp_path, upstream, fake_system):
    upstream.commit("1.6")
    manager = Manager(tmp_path / "opt/bananawiki", system=fake_system)
    manager.layout()
    manager.configure_source(url=str(upstream.bare), allow_local=True)
    result = manager.install(upstream.work, mode="wiki", domain="wiki.example.org")
    assert result["outcome"] == "complete" and result["automatic_updates"] is False
    settings = manager.settings()
    assert settings["installed"] and settings["port"] == 5001 and settings["service"] == "bananawiki"
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["BW_PROXY_MODE"] == "1" and len(environment["BW_SETUP_TOKEN"]) == 64
    assert (fake_system.unit_dir / "bananawiki-update.timer").exists()
    assert ["systemctl", "disable", "--now", "bananawiki-update.timer"] in fake_system.commands
    with pytest.raises(ValueError, match="already exists"):
        manager.install(upstream.work, mode="wiki")
    (upstream.work / "dirty").write_text("x")
    other = Manager(tmp_path / "opt/second", system=FakeSystem(tmp_path / "host2"))
    other.layout()
    other.configure_source(url=str(upstream.bare), allow_local=True)
    with pytest.raises(ValueError, match="checkout contains changes"):
        other.install(upstream.work, mode="wiki", port=5002)


def test_preflight_refuses_foreign_units(tmp_path, fake_system):
    (fake_system.unit_dir / "bananawiki.service").write_text("[Service]\nExecStart=/bin/true\n")
    with pytest.raises(ValueError, match="was not installed"):
        fake_system.preflight(settings_for("wiki"), profile.services(settings_for("wiki")))


# Host layer with a recording runner --------------------------------------------------


def test_healthy_probes_tenants_from_the_hosting_database(tmp_path):
    root = tmp_path / "opt/bananawiki"
    (root / "config").mkdir(parents=True)
    instances = root / "data/instances"
    (instances / "acme").mkdir(parents=True)
    database = sqlite3.connect(root / "data/hosting.db")
    database.execute("CREATE TABLE instances (subdomain TEXT, domain_mode TEXT, status TEXT)")
    database.execute("INSERT INTO instances VALUES ('acme', 'subdomain', 'running'), ('gone', 'subdomain', 'stopped')")
    database.commit()
    database.close()
    write_environment(root / "config/app.env", {"HOSTING_DATABASE_PATH": str(root / "data/hosting.db"),
                                                "INSTANCES_DIR": str(instances)})
    inspect = [{"Id": "c1", "State": {"Running": True},
                "Config": {"Labels": {"org.bananawiki.role": "tenant",
                                      "org.bananawiki.data-dir": str((instances / "acme").resolve()),
                                      "org.bananawiki.internal-port": "5001"}},
                "NetworkSettings": {"Networks": {"n": {"IPAddress": "172.18.0.2"}}}}]
    probed = []

    def runner(command, **_):
        output = ""
        if command[:2] == ["docker", "ps"]:
            output = "c1\n"
        elif command[:2] == ["docker", "inspect"]:
            output = json.dumps(inspect)
        return subprocess.CompletedProcess(command, 0, output, "")

    system = System(runner=runner, probe=lambda url: probed.append(url) or 200, sleep=lambda _s: None)
    settings = {**settings_for("hosting"), "root": str(root)}
    assert system.healthy(settings, profile.services(settings), timeout=0)
    assert "http://172.18.0.2:5001/health" in probed
    inspect[0]["State"]["Running"] = False
    system.log_dir = root / "config"
    assert not system.healthy(settings, [], timeout=0)
    failure = read_json(root / "config/last-readiness-failure.json")
    assert any("no running container" in issue for issue in failure["issues"])


def test_run_hides_command_output_in_a_private_log(tmp_path):
    system = System(log_dir=tmp_path, runner=lambda command, **_: subprocess.CompletedProcess(
        command, 1, "token=secret", "boom"))
    with pytest.raises(RuntimeError) as error:
        system.run(["git", "fetch"])
    assert "secret" not in str(error.value)
    assert (tmp_path / "last-command.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bootstrap_fails", [False, True])
def test_release_refreshes_inherited_build_tools_before_installing_dependencies(tmp_path, monkeypatch,
                                                                              bootstrap_fails):
    release = tmp_path / "release"
    release.mkdir()
    (release / "requirements.txt").write_text("Flask>=3.1\n")
    commands = []
    ownership = []

    def runner(command, **_):
        commands.append(command)
        if command[:3] == ["python3", "-m", "venv"]:
            (release / ".venv").mkdir()
        failed = bootstrap_fails and "--upgrade" in command
        return subprocess.CompletedProcess(command, int(failed), "", "")

    system = System(runner=runner)
    monkeypatch.setattr(system, "identity", lambda _name: (4242, 4242))
    monkeypatch.setattr("bananawiki.ops.system.set_release_owner",
                        lambda path, uid, gid, **kw: ownership.append((path, uid, gid, kw)))
    settings = {"service": "wiki", "mode": "wiki", "revision": "a" * 40}
    if bootstrap_fails:
        with pytest.raises(RuntimeError):
            system.prepare_release(settings, release, profile.ReleaseFeatures(hardened=True))
    else:
        system.prepare_release(settings, release, profile.ReleaseFeatures(hardened=True))
    bootstrap = commands[1]
    assert bootstrap[:4] == ["runuser", "-u", "wiki", "--"]
    assert {"--only-binary=:all:", "--no-deps", "--upgrade", "pip>=26.2.1", "setuptools>=83"} <= set(bootstrap)
    installs = [command for command in commands if "-r" in command]
    assert len(installs) == (0 if bootstrap_fails else 1)
    if installs:
        assert commands.index(bootstrap) < commands.index(installs[0])
        assert "--only-binary=:all:" in installs[0]
        assert ownership[-1] == (release, 0, 4242, {"readonly": True})
    else:
        assert all(item[0] != release for item in ownership)


def test_set_release_owner_refuses_hard_links(tmp_path):
    from bananawiki.ops.system import set_release_owner

    release = tmp_path / "release"
    release.mkdir()
    (release / "a").write_text("x")
    os.link(release / "a", release / "b")
    with pytest.raises(ValueError, match="hard links"):
        set_release_owner(release, os.getuid(), os.getgid(), readonly=True)
