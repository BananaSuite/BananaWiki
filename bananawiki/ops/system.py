"""Everything that touches the host: accounts, systemd, pip, Docker, HTTP probes.

Kept apart from the lifecycle decisions in :mod:`.manager` so tests can run
the whole controller against a fake host. All commands go through
:meth:`System.run` (injectable ``runner``) and all HTTP probes through
``probe``.
"""

from __future__ import annotations

import errno
import grp
import ipaddress
import json
import os
import pwd
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import MANAGED_MARKER, units
from .caddy_blocks import parse_version
from .files import atomic_write, digest_file, read_environment, read_json
from .profile import TENANT_IMAGE, ReleaseFeatures, Service, hosting_storage_link, inside_tenant
from .runtime_agent import ROUTES_FILE, ROUTES_WANTED

Runner = Callable[..., subprocess.CompletedProcess]
AUXILIARY_SUFFIXES = ("-tts", "-maintenance", "-agent")


def set_release_owner(path: Path, uid: int, gid: int, *, readonly: bool = False) -> None:
    """Change ownership through directory descriptors, never following links.

    ``readonly`` seals the tree (directories 0750, files 0640/0750). Hard links
    and special files are refused so the service cannot plant a link to a
    root-owned file for us to chown.
    """
    def visit(descriptor: int) -> None:
        if readonly:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o750)
        for name in os.listdir(descriptor):
            try:
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    continue  # virtual environments link to the system interpreter
                raise
            try:
                info = os.fstat(child)
                if stat.S_ISDIR(info.st_mode):
                    visit(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.fchown(child, uid, gid)
                    if readonly:
                        os.fchmod(child, 0o750 if info.st_mode & 0o111 else 0o640)
                else:
                    raise ValueError("Release builds cannot contain special files or hard links.")
            finally:
                os.close(child)
        if not readonly:
            os.fchown(descriptor, uid, gid)

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor)
    finally:
        os.close(descriptor)


def _http_status(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


class System:
    unit_dir = Path("/etc/systemd/system")
    bin_dir = Path("/usr/local/bin")
    proxy_file = Path("/etc/caddy/Caddyfile")
    state_dir = Path("/var/lib")

    def __init__(self, *, log_dir: Path | None = None, runner: Runner | None = None,
                 probe: Callable[[str], int] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.log_dir = log_dir
        self.runner = runner or subprocess.run
        self.probe = probe or _http_status
        self.sleep = sleep

    # Commands -------------------------------------------------------------

    def run(self, command: Sequence[Any], *, check: bool = True, timeout: int = 600,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        arguments = [str(part) for part in command]
        result = self.runner(arguments, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)
        if check and result.returncode:
            detail = ""
            if self.log_dir:
                log = self.log_dir / "last-command.log"
                atomic_write(log, ((result.stdout or "") + "\n" + (result.stderr or ""))[-100000:])
                detail = f" Details are in the private log {log}."
            name = Path(arguments[0]).name
            raise RuntimeError(f"{name} {arguments[1] if len(arguments) > 1 else ''} failed "
                               f"(exit {result.returncode}).{detail}")
        return result

    # Accounts and permissions ---------------------------------------------

    def identity(self, name: str) -> tuple[int, int]:
        entry = pwd.getpwnam(name)
        return entry.pw_uid, entry.pw_gid

    def account(self, settings: dict[str, Any]) -> tuple[int, int]:
        name = settings["service"]
        try:
            uid, gid = self.identity(name)
        except KeyError:
            self.run(["useradd", "--system", "--user-group", "--home-dir", str(Path(settings["root"]) / "data"),
                      "--shell", "/usr/sbin/nologin", name])
            uid, gid = self.identity(name)
        if uid == 0 or gid == 0:
            raise ValueError("The application cannot use the root account or group.")
        if settings["mode"] == "hosting":
            self.run(["docker", "info"], timeout=30)
        return uid, gid

    def docker_member(self, name: str) -> bool:
        try:
            return name in grp.getgrnam("docker").gr_mem
        except KeyError:
            return False

    def docker_access(self, settings: dict[str, Any], enabled: bool) -> None:
        """Grant the portal the Docker group only for 1.4 releases; 1.6 uses the runtime agent."""
        name = settings["service"]
        if settings["mode"] != "hosting":
            return
        if enabled and not self.docker_member(name):
            self.run(["usermod", "-aG", "docker", name])
        elif not enabled and self.docker_member(name):
            self.run(["gpasswd", "-d", name, "docker"])

    def data_permissions(self, settings: dict[str, Any]) -> None:
        """Give ``data/`` to the service account (0700/0600) and make ``site/`` world-readable.

        Tenants write inside ``data/instances/<tenant>`` while this runs as
        root (containers keep running during a unit convergence), so the walk
        uses directory descriptors and ``O_NOFOLLOW`` and never changes what a
        link points to: a file swapped for a link to ``/`` or ``/etc/shadow``
        in the middle of the walk is not chowned or chmodded. Links and special
        files a tenant made in its own directory are left alone; anywhere else
        they are refused, as are hard links to files of another owner.
        """
        uid, gid = self.identity(settings["service"])
        data = Path(settings["root"]) / "data"
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        hosting = settings["mode"] == "hosting"
        for current, directories, files, directory in os.fwalk(data, follow_symlinks=False):
            os.fchown(directory, uid, gid)
            os.fchmod(directory, 0o700)
            for name in (*directories, *files):
                path = Path(current) / name
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    continue  # visited (by descriptor) as its own step of the walk
                if stat.S_ISLNK(info.st_mode):
                    if hosting and hosting_storage_link(path, data):
                        os.chown(name, uid, gid, dir_fd=directory, follow_symlinks=False)
                    elif not (hosting and inside_tenant(path, data)):
                        raise ValueError(f"Managed data must not contain symlinks: {path}")
                    continue
                if not stat.S_ISREG(info.st_mode):
                    if hosting and inside_tenant(path, data):
                        continue
                    raise ValueError(f"Managed data must contain only regular files: {path}")
                self._own_file(directory, name, info, uid, gid, path)
        for current, directories, files in os.walk(Path(settings["root"]) / "site"):
            for path in (Path(current), *(Path(current) / name for name in (*directories, *files))):
                if path.is_symlink():
                    raise ValueError("The static site must not contain symlinks.")
                path.chmod(0o755 if path.is_dir() else 0o644)

    @staticmethod
    def _own_file(directory: int, name: str, info: os.stat_result, uid: int, gid: int, path: Path) -> None:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOENT):
                return  # replaced by a link, or removed, since it was listed
            raise
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return
            if opened.st_nlink > 1 and opened.st_uid != uid:
                raise ValueError(f"Managed data must not hard-link files of another owner: {path}")
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    # Releases -------------------------------------------------------------

    def prepare_release(self, settings: dict[str, Any], release: Path, features: ReleaseFeatures) -> None:
        """Build the release's venv as the service user (network: PyPI), then seal the tree read-only."""
        uid, gid = self.identity(settings["service"])
        print(f"Preparing BananaWiki {settings['mode']} at {settings['revision'][:12]}.", file=sys.stderr, flush=True)
        for current, _dirs, files in os.walk(release):
            Path(current).chmod(0o755)
            for name in files:
                path = Path(current) / name
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        python = release / ".venv/bin/python"
        pip = ["runuser", "-u", settings["service"], "--", str(python), "-m", "pip", "install",
               "--disable-pip-version-check", "--no-cache-dir", "--no-input"]
        self.run(["python3", "-m", "venv", str(release / ".venv")])
        set_release_owner(release / ".venv", uid, gid)
        # Distribution ensurepip wheels can predate security fixes; bootstrap only the installer first.
        self.run([*pip, "--only-binary=:all:", "--no-deps", "--upgrade", "pip>=26.2.1"], timeout=600, cwd=release)
        print("Installing Python dependencies; this may take several minutes.", file=sys.stderr, flush=True)
        # 1.6 releases install wheels only: no package build scripts run on the server.
        binary = ["--only-binary=:all:"] if features.hardened else []
        self.run([*pip, *binary, "-r", str(release / "requirements.txt")], timeout=1800, cwd=release)
        if settings["mode"] == "hosting":
            print("Building the tenant image from this source revision.", file=sys.stderr, flush=True)
            self.run(["docker", "build", "--pull=false", "-f", str(release / "Dockerfile.tenant"),
                      "-t", f"{TENANT_IMAGE}:{settings['revision']}", str(release)], timeout=1800)
        set_release_owner(release, 0, gid, readonly=True)

    # Units ----------------------------------------------------------------

    def _unit(self, name: str, suffix: str = ".service") -> Path:
        return self.unit_dir / (name + suffix)

    def _check_owned(self, path: Path, root: Path) -> None:
        if path.exists() and not units.is_managed(path.read_text(encoding="utf-8", errors="replace"), root):
            raise ValueError(f"{path.name} already exists and was not installed for {root}. "
                             "Choose another --name or migrate it explicitly.")

    def preflight(self, settings: dict[str, Any], services: list[Service]) -> None:
        root = Path(settings["root"])
        for service in services:
            self._check_owned(self._unit(service.name), root)
        self._check_owned(self.bin_dir / settings["service"], root)
        with socket.socket() as probe:
            probe.settimeout(1)
            occupied = probe.connect_ex(("127.0.0.1", int(settings["port"]))) == 0
        if occupied and not self.active(settings["service"]):
            raise ValueError(f"Port {settings['port']} is already in use. Choose another --port.")

    def unit_texts(self, settings: dict[str, Any], services: list[Service],
                   features: ReleaseFeatures) -> dict[Path, str]:
        output = {self._unit(service.name): units.service_unit(settings, service, features) for service in services}
        output[self.bin_dir / settings["service"]] = units.wrapper(settings)
        return output

    def units_outdated(self, settings: dict[str, Any], services: list[Service], features: ReleaseFeatures) -> bool:
        for path, text in self.unit_texts(settings, services, features).items():
            if not path.is_file() or path.read_text(encoding="utf-8", errors="replace") != text:
                return True
        return False

    def save_units(self, settings: dict[str, Any], names: list[str]) -> dict[str, bytes | None]:
        """The current unit files (for undoing a failed convergence)."""
        paths = [self._unit(name) for name in names] + [self.bin_dir / settings["service"]]
        return {str(path): path.read_bytes() if path.is_file() else None for path in paths}

    def restore_units(self, saved: dict[str, bytes | None]) -> None:
        for name, content in saved.items():
            path = Path(name)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content, 0o755 if path.parent == self.bin_dir else 0o644)
        self.run(["systemctl", "daemon-reload"])

    def install_units(self, settings: dict[str, Any], services: list[Service], features: ReleaseFeatures) -> None:
        root = Path(settings["root"])
        texts = self.unit_texts(settings, services, features)
        for path in texts:
            self._check_owned(path, root)
        wanted = {service.name for service in services}
        stale = [settings["service"] + suffix for suffix in AUXILIARY_SUFFIXES
                 if settings["service"] + suffix not in wanted and self._unit(settings["service"] + suffix).exists()]
        for name in stale:
            if units.is_managed(self._unit(name).read_text(encoding="utf-8", errors="replace"), root):
                self.run(["systemctl", "disable", "--now", name], check=False)
                self._unit(name).unlink()
        for path, text in texts.items():
            atomic_write(path, text, 0o755 if path.parent == self.bin_dir else 0o644)
        self.docker_access(settings, not features.runtime_agent)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", *(service.name for service in services)])

    def _install_schedule(self, name: str, texts: tuple[str, str], enabled: bool, root: Path) -> None:
        for suffix in (".service", ".timer"):
            path = self._unit(name, suffix)
            if path.exists() and MANAGED_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
                raise ValueError(f"An unrelated unit already uses the name {path.name}.")
        atomic_write(self._unit(name), texts[0], 0o644)
        atomic_write(self._unit(name, ".timer"), texts[1], 0o644)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable" if enabled else "disable", "--now", name + ".timer"])

    def install_timer(self, settings: dict[str, Any], policy: dict[str, Any]) -> None:
        self._install_schedule(settings["service"] + "-update", units.update_units(settings, policy),
                               policy["enabled"], Path(settings["root"]))

    def install_backup_timer(self, settings: dict[str, Any], policy: dict[str, Any]) -> None:
        self._install_schedule(settings["service"] + "-backup", units.backup_units(settings, policy),
                               policy["enabled"], Path(settings["root"]))

    def active(self, name: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", name], check=False, timeout=15).returncode == 0

    def start(self, names: list[str]) -> None:
        if names:
            self.run(["systemctl", "start", *names], timeout=300)

    def stop(self, names: list[str]) -> None:
        if names:
            self.run(["systemctl", "stop", *reversed(names)], timeout=300)

    # Tenant containers (the controller runs as root; the portal uses the runtime agent) --------------

    def containers(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        if settings["mode"] != "hosting":
            return []
        ids = self.run(["docker", "ps", "--all", "--quiet", "--filter", "label=org.bananawiki.role=tenant"]).stdout.split()
        if not ids:
            return []
        base = (Path(settings["root"]) / "data/instances").resolve()
        output = []
        for item in json.loads(self.run(["docker", "inspect", *ids]).stdout):
            labels = item.get("Config", {}).get("Labels") or {}
            directory = Path(labels.get("org.bananawiki.data-dir", "/"))
            if not directory.is_absolute() or not directory.resolve().is_relative_to(base):
                continue
            networks = (item.get("NetworkSettings") or {}).get("Networks") or {}
            output.append({
                "id": item["Id"], "running": bool((item.get("State") or {}).get("Running")),
                "addresses": [network["IPAddress"] for network in networks.values() if network.get("IPAddress")],
                "internal_port": int(labels.get("org.bananawiki.internal-port", "5001")),
                "data_dir": str(directory),
            })
        return output

    def expected_tenant_directories(self, settings: dict[str, Any]) -> set[str]:
        """Running tenants according to the portal database (C10's query)."""
        root = Path(settings["root"])
        environment = read_environment(root / "config/app.env")
        database = Path(environment.get("HOSTING_DATABASE_PATH", root / "data/hosting.db"))
        base = Path(environment.get("INSTANCES_DIR", root / "data/instances")).resolve()
        if not database.is_file():
            return set()
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute("SELECT subdomain, domain_mode FROM instances WHERE status='running'").fetchall()
        finally:
            connection.close()
        output = set()
        for slug, mode in rows:
            directory = (base / (str(slug) + ("__apex" if mode == "apex" else ""))).resolve()
            if not directory.is_relative_to(base) or directory == base:
                raise ValueError("Invalid tenant path in the hosting database.")
            output.add(str(directory))
        return output

    def stop_containers(self, containers: list[dict[str, Any]]) -> None:
        ids = [item["id"] for item in containers if item["running"]]
        if ids:
            self.run(["docker", "stop", "--time", "30", *ids], timeout=300)

    def resume_containers(self, containers: list[dict[str, Any]]) -> None:
        ids = [item["id"] for item in containers if item["running"]]
        if ids:
            self.run(["docker", "start", *ids], timeout=300)

    def remove_containers(self, containers: list[dict[str, Any]]) -> None:
        ids = [item["id"] for item in containers]
        if ids:
            self.run(["docker", "rm", "--force", *ids], timeout=300)

    def prune_images(self, keep_revisions: set[str]) -> list[str]:
        """Remove tenant images of releases no longer kept (images still in use are left alone)."""
        listing = self.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", TENANT_IMAGE],
                           check=False, timeout=60)
        if listing.returncode:
            return []
        removed = []
        for tag in listing.stdout.split():
            repository, _, revision = tag.partition(":")
            if repository != TENANT_IMAGE or revision in keep_revisions or revision == "<none>":
                continue
            if self.run(["docker", "image", "rm", tag], check=False, timeout=120).returncode == 0:
                removed.append(tag)
        return removed

    # Readiness ------------------------------------------------------------

    def readiness_timeout(self, settings: dict[str, Any], containers: Sequence[dict[str, Any]] = ()) -> int:
        if settings["mode"] != "hosting":
            return 180
        environment = read_environment(Path(settings["root"]) / "config/app.env")
        try:
            startup = max(30, min(600, int(environment.get("INSTANCE_STARTUP_TIMEOUT_SECONDS", "120"))))
            workers = max(1, min(16, int(environment.get("BW_HOSTING_RECOVERY_MAX_WORKERS", "2"))))
        except ValueError:
            startup, workers = 120, 2
        try:
            count = max(len(containers), len(self.expected_tenant_directories(settings)))
        except (OSError, sqlite3.Error, ValueError):
            count = len(containers)
        waves = max(1, (count + workers - 1) // workers)
        return min(7200, max(180, waves * (2 * startup + 120)))

    def _tenant_urls(self, settings: dict[str, Any], containers: Sequence[dict[str, Any]],
                     issues: list[str]) -> list[str] | None:
        try:
            expected = self.expected_tenant_directories(settings)
            expected.update(item["data_dir"] for item in containers if item["running"])
            current = {item["data_dir"]: item for item in self.containers(settings)}
        except (OSError, sqlite3.Error, ValueError, RuntimeError, json.JSONDecodeError) as error:
            issues.append(f"Cannot inspect required tenants: {type(error).__name__}")
            return None
        urls = []
        missing = False
        for directory in sorted(expected):
            item = current.get(directory)
            if not item or not item["running"] or not item["addresses"]:
                issues.append(f"Tenant has no running container: {directory}")
                missing = True
                continue
            port = item["internal_port"]
            if not 1 <= port <= 65535:
                issues.append(f"Invalid tenant port for {directory}")
                missing = True
                continue
            try:
                address = ipaddress.ip_address(item["addresses"][0])
            except ValueError:
                issues.append(f"Invalid tenant address for {directory}")
                missing = True
                continue
            urls.append(f"http://{address}:{port}/health")
        return None if missing else urls

    def healthy(self, settings: dict[str, Any], services: list[Service],
                containers: Sequence[dict[str, Any]] = (), timeout: int | None = None) -> bool:
        timeout = self.readiness_timeout(settings, containers) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        issues: list[str] = []
        while True:
            issues = [f"Service is not active: {service.name}" for service in services if not self.active(service.name)]
            urls = [f"http://127.0.0.1:{int(settings['port'])}/health"]
            if settings["mode"] == "hosting":
                tenants = self._tenant_urls(settings, containers, issues)
                urls += tenants or []
                if tenants is not None:
                    issues += self.route_issues(settings, services, containers)
            for url in urls:
                try:
                    status = self.probe(url)
                except OSError as error:
                    issues.append(f"{url}: {type(error).__name__}")
                    continue
                if status != 200:
                    issues.append(f"{url}: HTTP {status}")
            if not issues:
                return True
            if time.monotonic() >= deadline:
                break
            self.sleep(3)
        if self.log_dir:
            atomic_write(self.log_dir / "last-readiness-failure.json", json.dumps({
                "checked_at": time.time(), "revision": settings["revision"], "timeout_seconds": timeout,
                "issues": issues[:100], "omitted_issues": max(0, len(issues) - 100),
            }, indent=2) + "\n")
        return False

    # Reverse proxy ----------------------------------------------------------

    def routes_directory(self, settings: dict[str, Any]) -> Path:
        """The runtime agent's ``StateDirectory=<service>-routes``, imported by the managed Caddyfile."""
        return self.state_dir / (settings["service"] + "-routes")

    def ensure_routes_directory(self, settings: dict[str, Any]) -> None:
        """Create the routes directory before Caddy imports it (root-owned, 0755: Caddy reads it)."""
        path = self.routes_directory(settings)
        if path.is_symlink():
            raise ValueError(f"{path} must not be a symlink.")
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.chmod(0o755)

    def remove_routes_directory(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            for name in (ROUTES_FILE, ROUTES_WANTED):
                (path / name).unlink(missing_ok=True)
            try:
                path.rmdir()
            except OSError:
                pass  # something else was put there; leave it

    def validate_proxy(self, candidate: Path, environment: dict[str, str] | None = None) -> None:
        """``caddy validate`` with the variables Caddy's unit gets (the Cloudflare API token)."""
        extra = {**read_environment(self.caddy_environment_file), **(environment or {})}
        if not extra:
            self.run(["caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"], timeout=60)
            return
        result = self.runner(["caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"],
                             capture_output=True, text=True, timeout=60, check=False, env={**os.environ, **extra})
        if result.returncode:
            detail = ((result.stderr or "").strip().splitlines() or ["no output"])[-1]
            raise RuntimeError(f"caddy validate failed (exit {result.returncode}): {detail[:500]}")

    # Caddy's own installation (certificate modes) ---------------------------

    @property
    def caddy_environment_file(self) -> Path:
        """Secrets for Caddy's unit (``CLOUDFLARE_API_TOKEN``), root-only, loaded by a drop-in."""
        return self.proxy_file.parent / "bananawiki-cloudflare.env"

    @property
    def caddy_dropin(self) -> Path:
        return self.unit_dir / "caddy.service.d" / "bananawiki-cloudflare.conf"

    def caddy_version(self) -> tuple[int, int, int] | None:
        """The installed Caddy's version, or None when unknown."""
        try:
            result = self.run(["caddy", "version"], check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return parse_version(result.stdout or "") if result.returncode == 0 else None

    def caddy_modules(self) -> set[str]:
        try:
            result = self.run(["caddy", "list-modules"], check=False, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return set()
        return {line.split()[0] for line in (result.stdout or "").splitlines() if line.strip()}

    def install_caddy_environment(self, values: dict[str, str]) -> None:
        """Write Caddy's secret environment (0600) and the systemd drop-in that loads it."""
        lines = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
        atomic_write(self.caddy_environment_file, lines, 0o600)
        atomic_write(self.caddy_dropin, f"{MANAGED_MARKER}\n[Service]\nEnvironmentFile={self.caddy_environment_file}\n",
                     0o644)
        self.run(["systemctl", "daemon-reload"])

    def install_origin_certificate(self, certificate: Path, key: Path) -> tuple[str, str]:
        """Copy a Cloudflare Origin CA certificate and key where Caddy (user ``caddy``) can read them."""
        cert_text, key_text = certificate.read_text(encoding="ascii"), key.read_text(encoding="ascii")
        if "-----BEGIN CERTIFICATE-----" not in cert_text or "PRIVATE KEY-----" not in key_text:
            raise ValueError("--origin-cert and --origin-key must be PEM files (certificate, then private key).")
        targets = (self.proxy_file.parent / "bananawiki-origin.crt", self.proxy_file.parent / "bananawiki-origin.key")
        try:
            group = grp.getgrnam("caddy").gr_gid
        except KeyError:
            group = None
        key_mode = 0o640 if group is not None else 0o600
        for target, text, mode in ((targets[0], cert_text, 0o644), (targets[1], key_text, key_mode)):
            atomic_write(target, text, mode)
            if group is not None:
                os.chown(target, 0, group)
        return str(targets[0]), str(targets[1])

    def reload_proxy(self, *, check: bool = True) -> None:
        self.run(["systemctl", "reload-or-restart", "caddy"], check=check, timeout=120)

    def route_issues(self, settings: dict[str, Any], services: Sequence[Service],
                     containers: Sequence[dict[str, Any]]) -> list[str]:
        """Readiness of direct routing: every running wiki the portal routes has a current site block.

        Only for hosting releases with the runtime agent in subdomain mode.
        The portal publishes its table (``routes.json``) after it recovered the
        wikis; the agent renders ``tenants.caddy`` with each container's
        current address. A wiki absent from the table (for example one whose
        account is suspended) is not required.
        """
        if settings["mode"] != "hosting" or not any(service.privileged for service in services):
            return []
        environment = read_environment(Path(settings["root"]) / "config/app.env")
        mode = environment.get("HOSTING_MODE", "subdomain" if settings.get("domain") else "port").lower()
        if mode != "subdomain":
            return []
        try:
            expected = self.expected_tenant_directories(settings)
        except (OSError, sqlite3.Error, ValueError):
            expected = set()
        expected.update(item["data_dir"] for item in containers if item["running"])
        if not expected:
            return []
        directory = self.routes_directory(settings)
        try:
            wanted = json.loads((directory / ROUTES_WANTED).read_text(encoding="utf-8"))
            rendered = (directory / ROUTES_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return ["The portal has not published the wiki routes yet."]
        if not isinstance(wanted, dict):
            return ["The published wiki routes are unreadable."]
        blocks = {}
        for block in rendered.split("\n# tenant ")[1:]:
            name, _, body = block.partition("\n")
            blocks[name] = body
        try:
            current = {item["data_dir"]: item for item in self.containers(settings)}
        except (RuntimeError, ValueError, json.JSONDecodeError) as error:
            return [f"Cannot inspect routed tenants: {type(error).__name__}"]
        issues = []
        for directory_name in sorted(expected):
            tenant = Path(directory_name).name
            if tenant not in wanted:
                continue
            item = current.get(directory_name)
            upstream = f"{item['addresses'][0]}:{item['internal_port']} " if item and item["addresses"] else None
            if upstream is None or f"reverse_proxy {upstream}" not in blocks.get(tenant, ""):
                issues.append(f"Tenant is not routed to its container: {tenant}")
        return issues

    # Removal --------------------------------------------------------------

    def uninstall(self, settings: dict[str, Any], names: list[str]) -> None:
        name = settings["service"]
        root = Path(settings["root"])
        for schedule in (name + "-update", name + "-backup"):
            self.run(["systemctl", "disable", "--now", schedule + ".timer"], check=False)
            self.run(["systemctl", "stop", schedule + ".service"], check=False)
        everything = {*names, *(name + suffix for suffix in AUXILIARY_SUFFIXES), name + "-update", name + "-backup"}
        for unit in sorted(everything):
            self.run(["systemctl", "disable", unit], check=False)
            for suffix in (".service", ".timer"):
                path = self._unit(unit, suffix)
                if path.exists() and MANAGED_MARKER in path.read_text(encoding="utf-8", errors="replace"):
                    path.unlink()
        command = self.bin_dir / name
        if command.exists() and units.is_managed(command.read_text(encoding="utf-8", errors="replace"), root):
            command.unlink()
        self.docker_access(settings, False)
        self.remove_proxy(settings)
        self.run(["systemctl", "daemon-reload"])

    def remove_proxy(self, settings: dict[str, Any]) -> None:
        """Undo ``proxy --install`` only if the installed file is still exactly ours."""
        record = read_json(Path(settings["root"]) / "config/proxy.json", {})
        if not record or not self.proxy_file.is_file() or digest_file(self.proxy_file) != record.get("installed_sha256"):
            return
        backup = record.get("previous")
        if backup:
            old = Path(backup)
            if not old.is_relative_to(Path(settings["root"]) / "backups") or not old.is_file():
                raise ValueError("The saved proxy configuration is missing. Restore it before uninstalling.")
            atomic_write(self.proxy_file, old.read_bytes(), 0o644)
        else:
            atomic_write(self.proxy_file, "# BananaWiki uninstalled; no sites configured.\n", 0o644)
        self.run(["systemctl", "reload-or-restart", "caddy"])
