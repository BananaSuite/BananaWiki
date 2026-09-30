"""Test doubles for the lifecycle controller: a fake host and local Git sources.

There is deliberately no conftest.py in this directory: it would shadow
tests/conftest.py for `from conftest import …`.

Nothing here calls systemctl, docker, useradd or pip: :class:`FakeSystem`
keeps the "running" services in memory and records every command.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from bananawiki.ops import profile, units
from bananawiki.ops.files import write_environment, write_json
from bananawiki.ops.system import System


class FakeSystem(System):
    """A host where systemd and Docker are simulated and the application is always "healthy" unless told."""

    def __init__(self, tmp: Path):
        super().__init__(log_dir=None, runner=self._run, probe=lambda url: 200, sleep=lambda _s: None)
        self.unit_dir = tmp / "systemd"
        self.bin_dir = tmp / "bin"
        self.proxy_file = tmp / "caddy" / "Caddyfile"
        self.state_dir = tmp / "var-lib"
        self.tenant_containers: list[dict[str, Any]] = []
        self.unit_dir.mkdir(parents=True)
        self.bin_dir.mkdir(parents=True)
        self.commands: list[list[str]] = []
        self.running: set[str] = set()
        self.docker_group = False
        self.unhealthy_revisions: set[str] = set()
        self.on_start: list[Any] = []
        self.prepared: list[str] = []

    def _run(self, command: list[str], **_: Any) -> subprocess.CompletedProcess:
        self.commands.append(command)
        code = 0
        if command[:2] == ["systemctl", "start"]:
            self.running.update(command[2:])
        elif command[:2] == ["systemctl", "stop"]:
            self.running.difference_update(command[2:])
        elif command[:3] == ["systemctl", "is-active", "--quiet"]:
            code = 0 if command[3] in self.running else 3
        elif command[:2] == ["usermod", "-aG"]:
            self.docker_group = True
        elif command[:1] == ["gpasswd"]:
            self.docker_group = False
        return subprocess.CompletedProcess(command, code, "", "")

    def identity(self, name: str) -> tuple[int, int]:
        return os.getuid(), os.getgid()

    def account(self, settings: dict[str, Any]) -> tuple[int, int]:
        return self.identity(settings["service"])

    def docker_member(self, name: str) -> bool:
        return self.docker_group

    def prepare_release(self, settings, release, features) -> None:
        self.prepared.append(settings["revision"])

    def start(self, names: list[str]) -> None:
        super().start(names)
        for hook in self.on_start:
            hook(names)

    def healthy(self, settings, services, containers=(), timeout=None) -> bool:
        if settings["revision"] in self.unhealthy_revisions:
            return False
        if not all(service.name in self.running for service in services):
            return False
        return not self.route_issues(settings, services, containers)

    # Tenant containers live in memory (hosting tests add them to ``tenant_containers``).

    def containers(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in self.tenant_containers] if settings["mode"] == "hosting" else []

    def stop_containers(self, containers: list[dict[str, Any]]) -> None:
        stopped = {item["id"] for item in containers}
        for item in self.tenant_containers:
            if item["id"] in stopped:
                item["running"] = False

    def resume_containers(self, containers: list[dict[str, Any]]) -> None:
        resumed = {item["id"] for item in containers if item["running"]}
        for item in self.tenant_containers:
            if item["id"] in resumed:
                item["running"] = True

    def remove_containers(self, containers: list[dict[str, Any]]) -> None:
        removed = {item["id"] for item in containers}
        self.tenant_containers = [item for item in self.tenant_containers if item["id"] not in removed]

    def started(self) -> list[list[str]]:
        return [command for command in self.commands if command[:2] == ["systemctl", "start"]]


def git(*args: str, cwd: Path) -> str:
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=environment, check=True, capture_output=True,
                          text=True).stdout.strip()


class Upstream:
    """A working repository plus the bare repository servers fetch from."""

    def __init__(self, tmp: Path):
        self.work = tmp / "work"
        self.bare = tmp / "upstream.git"
        self.work.mkdir()
        git("init", "--quiet", "--initial-branch=main", cwd=self.work)
        git("init", "--quiet", "--bare", "--initial-branch=main", str(self.bare), cwd=tmp)

    def commit(self, message: str, *, modern: bool = True, extra: dict[str, str] | None = None) -> str:
        files = {"banana": "#!/usr/bin/env python3\n", "LICENSE": "AGPL\n", "requirements.txt": "",
                 "README": message + "\n", **(extra or {})}
        agent = self.work / "bananawiki/ops/runtime_agent.py"
        if modern:
            files["bananawiki/ops/runtime_agent.py"] = "# agent\n"
        elif agent.exists():
            git("rm", "--quiet", "-r", "bananawiki", cwd=self.work)
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git("add", "--all", cwd=self.work)
        git("commit", "--quiet", "-m", message, cwd=self.work)
        git("push", "--quiet", "--force", str(self.bare), "HEAD:refs/heads/main", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def orphan(self, message: str) -> str:
        git("checkout", "--quiet", "--orphan", "rewrite", cwd=self.work)
        sha = self.commit(message)
        return sha


def legacy_install(root: Path, system: FakeSystem, upstream: Upstream, revision: str, *, mode: str = "wiki") -> dict:
    """Recreate what `banana install` of 1.4 left on disk (layout, config json schema 1, units)."""
    for name in ("config", "backups", "staging", "releases", "data", "site"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    release = root / "releases" / revision
    release.mkdir()
    archive = root / "staging" / "seed.tar.gz"
    git("archive", "--format=tar.gz", "-o", str(archive), revision, cwd=upstream.work)
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(release)], check=True)
    os.replace(archive, release / ".source.tar.gz")
    write_json(release / ".release.json", {"revision": revision, "product": "BananaWiki"})
    (root / "current").symlink_to(Path("releases") / revision)
    settings = {"schema": 1, "product": "BananaWiki", "mode": mode, "root": str(root), "service": "bananawiki",
                "domain": "wiki.example.org", "portal_domain": "", "port": 5001, "backend_url": "",
                "ollama_binary": "", "source_url": str(upstream.bare), "installed": True, "revision": revision}
    write_json(root / "config/installation.json", settings)
    write_json(root / "config/source.json", {"url": str(upstream.bare), "branch": "main", "fallback_branch": None,
                                             "auth": "none", "signing": "none"})
    write_json(root / "config/updates.json", {"enabled": False, "interval_minutes": 60, "keep_backups": 3})
    write_environment(root / "config/app.env", profile.environment(settings, {"BW_CUSTOM": "kept"}))
    data = root / "data"
    (data / "uploads").mkdir()
    (data / "uploads" / "picture.png").write_bytes(b"\x89PNG" + b"x" * 200_000)
    database = sqlite3.connect(data / "bananawiki.db")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, title TEXT)")
    database.execute("INSERT INTO pages (title) VALUES ('Home')")
    database.commit()
    database.close()
    for service in profile.services(settings, profile.ReleaseFeatures()):
        (system.unit_dir / (service.name + ".service")).write_text(units.legacy_service_unit(settings, service))
        system.running.add(service.name)
    (system.bin_dir / "bananawiki").write_text(units.wrapper(settings))
    return settings
