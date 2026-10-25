"""Install, update, back up, restore, recover and remove one managed deployment.

Safety properties:

* every operation holds ``config/maintenance.lock`` (``flock``) and first
  finishes any interrupted transaction (``config/transaction.json``);
* releases are staged next to the running one, sealed read-only, and switched
  by an atomic symlink replacement;
* before anything changes, the data is captured; if the new release fails its
  readiness checks the data, the release and the units are put back;
* update sources are fetched with a hardened Git, fast-forward only unless the
  operator explicitly allows otherwise, optionally signature-verified.

Data is captured by an online :class:`~.snapshot.Snapshot` (short downtime);
the portable package is written after the service is back and verified as a
restore would before it is used. ``before-update-*`` packages, releases and
tenant images are pruned; an update that fails removes the release and tenant
image it prepared, and one whose snapshot would not fit is refused before it
prepares anything. Hosting installs get the runtime agent instead of
Docker group membership, and units installed by the 1.4 updater are converged
to the hardened units.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Collection, Iterable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import DEFAULT_SOURCE_URL, PRODUCT, caddy, profile, tenant_quotas
from .files import (
    DEFAULT_MAX_FILES,
    MAINTENANCE_MARKER,
    Refused,
    absolute_path,
    append_jsonl,
    atomic_write,
    digest_file,
    extract_archive,
    maintenance_lock,
    read_environment,
    read_json,
    read_package,
    untrusted_marker,
    write_environment,
    write_json,
    write_package,
)
from .profile import TENANT_IMAGE, ReleaseFeatures, Service
from .snapshot import Snapshot, stale_snapshots
from .snapshot import sources as snapshot_sources
from .source import GitSource, credential_host, valid_branch, valid_revision, valid_track, valid_url
from .system import (
    TRUE_VALUES,
    CandidateRefused,
    CandidateUnavailable,
    InvalidTenantQuotas,
    System,
    TenantStorageUnsupported,
)

DEFAULT_POLICY = {"enabled": False, "interval_minutes": 60, "keep_backups": 3}
UNREADY_POLICIES = ("isolate", "rollback")  # BANANA_UPDATE_UNREADY_WIKIS
ISOLATION_ATTEMPTS = 3  # stop, verify; again when a start in progress brought one back
PRUNED_PREFIXES = ("auto", "before-update", "before-restore", "remote")
PROXY_ROLLBACK = "Caddyfile.rollback"
FAILED_REVISION = "failed-revision.json"
# The units a convergence could not start (see Manager.converge): automatic updates do not install them again.
FAILED_CONVERGENCE = "failed-convergence.json"
# ``.release.json`` "prepared_by": a controller that installs a release's dependency locks with their hashes and
# builds its tenant image on a freshly pulled base (1.7). An earlier updater prepared releases otherwise.
PREPARED_BY = 3

log = logging.getLogger("bananawiki.ops")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InvalidPackageLimit(Refused, ValueError):
    """``BANANA_PACKAGE_MAX_FILES`` is not a valid number of files; checked before anything changes."""


class InvalidUpdatePolicy(Refused, ValueError):
    """``BANANA_UPDATE_UNREADY_WIKIS`` is not a known policy; checked before anything changes."""


def isolatable(failed: int, waited: int) -> bool:
    """Whether an update may keep its release although *failed* of the *waited* wikis do not serve under it.

    Only a few, and never all: when most wikis that served before fail, the
    release is the likely cause, not their own data. At most one wiki in ten
    (at least one) is isolated, and at least one other must serve again.
    """
    return 0 < failed < waited and failed <= max(1, waited // 10)


def declared_version(release: Path) -> tuple[int, int, int] | None:
    """The ``bananawiki.__version__`` a release tree declares (read as data, never imported), or None."""
    try:
        tree = ast.parse((release / "bananawiki" / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None
    for node in tree.body:
        # ``__version__ = "X.Y.Z"`` or, annotated, ``__version__: str = "X.Y.Z"``.
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if (any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            match = re.match(r"(\d+)\.(\d+)\.(\d+)", node.value.value)
            return (int(match[1]), int(match[2]), int(match[3])) if match else None
    return None


def check_dependency_locks(release: Path, *, allow_unpinned: bool = False) -> bool:
    """Refuse a release that should ship hash-pinned dependency locks and does not; return whether it is unpinned.

    1.7 and later releases install exactly the files ``requirements.lock``
    and ``requirements-build.lock`` pin. Without them the installer
    resolves the newest versions ``requirements.txt`` allows, from PyPI,
    with no hash: right for a release from before 1.7 (a rollback), which
    never had locks, never for a later one missing them by mistake. Only
    the operator's explicit *allow_unpinned* deploys such a release. A tree
    that declares no version is from before 1.7 (1.4 had no package
    version).
    """
    present = [(release / name).is_file() for name in ("requirements.lock", "requirements-build.lock")]
    if all(present):
        return False
    version = declared_version(release)
    # One lock without the other, or none in a release that says it is 1.7 or later: a release made by mistake.
    if not any(present) and (version is None or version < (1, 7, 0)) or allow_unpinned:
        return True
    missing = " and ".join(name for name, found in zip(("requirements.lock", "requirements-build.lock"), present,
                                                        strict=True) if not found)
    raise ValueError(f"This release lacks {missing}: since 1.7 a release installs exactly the dependency versions "
                     "and files its locks pin, and without them every dependency would be resolved anew from PyPI "
                     "with no hash check. Nothing was deployed. Ask its maintainers for a release with its locks; "
                     "to install this one from its requirements.txt ranges anyway, run the update (or restore) "
                     "again with --allow-unpinned.")


def storage_details(policy: tenant_quotas.Policy) -> dict[str, str]:
    """What a hosting operation reports about hard quotas (:attr:`.tenant_quotas.Policy.severity`).

    ``storage_warning`` when they were lost (or wikis cannot start),
    ``storage_note`` when the wikis never had them here or they are off, and
    nothing while they are enforced.
    """
    message = policy.warning()
    if not message:
        return {}
    return {"storage_warning" if policy.severity in ("warning", "error") else "storage_note": message}


def print_storage(policy: tenant_quotas.Policy, *, printed: Iterable[str] = ()) -> None:
    """Print :func:`storage_details` as a warning or a note, unless it is one of the messages already *printed*."""
    already = set(printed)
    for key, message in storage_details(policy).items():
        if message[:2000] not in already:
            print(f"{'Warning' if key == 'storage_warning' else 'Note'}: {message}", file=sys.stderr, flush=True)


def tenant_names(directories: Iterable[str]) -> list[str]:
    """Wikis by their directory name (``<slug>`` or ``<slug>__apex``), for reports."""
    return sorted(Path(directory).name for directory in directories)


class Manager:
    def __init__(self, root: str | os.PathLike[str], *, system: System | None = None):
        self.root = absolute_path(root)
        self.config_dir = self.root / "config"
        self.system = system or System(log_dir=self.config_dir)
        self.product = PRODUCT
        self.last_event: dict[str, Any] = {}  # the last event recorded, for commands whose result is not one

    # Configuration

    def settings(self) -> dict[str, Any]:
        data = read_json(self.config_dir / "installation.json")
        if not data or data.get("schema") != 1 or data.get("product") != PRODUCT:
            raise ValueError("No matching managed installation was found. Use install or install --restore PACKAGE.")
        if data.get("mode") not in profile.MODES or data.get("root") != str(self.root):
            raise ValueError("Installation metadata does not match this root or deployment mode.")
        profile.validate_service_name(data.get("service", ""))
        return data

    def save(self, settings: dict[str, Any]) -> None:
        write_json(self.config_dir / "installation.json", settings)

    def policy(self) -> dict[str, Any]:
        data = read_json(self.config_dir / "updates.json", DEFAULT_POLICY.copy())
        try:
            interval, keep = int(data.get("interval_minutes", 0)), int(data.get("keep_backups", 0))
        except (TypeError, ValueError):
            raise ValueError("Invalid automatic-update policy.") from None
        if type(data.get("enabled")) is not bool or not 5 <= interval <= 10080:
            raise ValueError("Invalid automatic-update policy.")
        if not 2 <= keep <= 30:
            raise ValueError("Keep between 2 and 30 automatic backups.")
        return data

    def package_limit(self) -> int:
        """The most files a package (so a backup or an update) may hold: ``BANANA_PACKAGE_MAX_FILES`` in ``app.env``.

        Without ``app.env`` yet (``install --restore`` on a new server) the default applies.
        An invalid value raises :class:`InvalidPackageLimit`: operations read
        it before they stop anything.
        """
        value = read_environment(self.config_dir / "app.env").get("BANANA_PACKAGE_MAX_FILES", "").strip()
        if not value:
            return DEFAULT_MAX_FILES
        if not value.isascii() or not value.isdigit() or not 1000 <= int(value) <= 100_000_000:
            raise InvalidPackageLimit("BANANA_PACKAGE_MAX_FILES in config/app.env must be a number of files "
                                      "between 1000 and 100000000.")
        return int(value)

    def unready_policy(self) -> str:
        """What an update does when wikis that served before do not serve under the new release.

        ``BANANA_UPDATE_UNREADY_WIKIS`` in ``app.env``: ``isolate`` (the
        default) keeps the new release when the platform is ready and only a
        few wikis fail (:func:`isolatable`), stops those wikis and reports
        them; ``rollback`` puts the whole platform back. An
        invalid value raises :class:`InvalidUpdatePolicy` before anything stops.
        """
        value = read_environment(self.config_dir / "app.env").get("BANANA_UPDATE_UNREADY_WIKIS", "").strip().lower()
        if not value:
            return "isolate"
        if value not in UNREADY_POLICIES:
            raise InvalidUpdatePolicy("BANANA_UPDATE_UNREADY_WIKIS in config/app.env must be isolate or rollback.")
        return value

    def source(self) -> dict[str, Any]:
        """``config/source.json``; one written before 1.7 has no ``track`` and keeps following its branch."""
        data = read_json(self.config_dir / "source.json")
        if not data:
            raise ValueError("No update source is configured.")
        valid_branch(data["branch"])
        if data.get("fallback_branch"):
            valid_branch(data["fallback_branch"])
        return {**data, "track": valid_track(data.get("track", "branch"))}

    def event(self, operation: str, outcome: str, **details: Any) -> dict[str, Any]:
        value = {"time": _now(), "operation": operation, "outcome": outcome, **details}
        write_json(self.config_dir / "status.json", value)
        append_jsonl(self.config_dir / "history.jsonl", value)
        self.last_event = value
        return value

    def layout(self) -> None:
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in ("config", "backups", "staging", "releases", "data", "site"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("The managed installation contains a directory symlink.")
            path.mkdir(mode=0o755 if name in {"releases", "site"} else 0o700, exist_ok=True)

    @staticmethod
    def read_secret(path: Path, *, multiline: bool = False) -> str:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Use a regular credential file smaller than 1 MiB.")
        value = path.read_text(encoding="utf-8").strip()
        if not value or "\0" in value or (not multiline and any(char.isspace() for char in value)):
            raise ValueError("Invalid credential file.")
        return value

    def configure_source(self, *, url: str | None = None, branch: str | None = None, fallback: str | None = None,
                         clear_fallback: bool = False, token_file: Path | None = None, username: str = "git",
                         ssh_key: Path | None = None, known_hosts: Path | None = None,
                         clear_credentials: bool = False, allow_local: bool = False,
                         signers_file: Path | None = None, clear_signatures: bool = False,
                         track: str | None = None) -> dict[str, Any]:
        # A new source follows release tags; one configured before 1.7 keeps its branch unless told otherwise.
        old = read_json(self.config_dir / "source.json", {
            "url": DEFAULT_SOURCE_URL, "branch": "main", "fallback_branch": None, "auth": "none", "signing": "none",
            "track": "tags",
        })
        new = dict(old)
        if track:
            new["track"] = valid_track(track)
        if sum(bool(item) for item in (token_file, ssh_key, clear_credentials)) > 1:
            raise ValueError("Choose one repository authentication method.")
        if signers_file and clear_signatures:
            raise ValueError("Choose either an allowed signers file or clearing signature checks.")
        if url:
            new["url"] = valid_url(url, allow_local=allow_local)
            # Credentials never follow a source to another host.
            if credential_host(old["url"]) != credential_host(new["url"]) and not token_file and not ssh_key:
                self._remove_credentials()
                new["auth"] = "none"
        if branch:
            new["branch"] = valid_branch(branch)
        if fallback:
            new["fallback_branch"] = valid_branch(fallback)
        elif clear_fallback:
            new["fallback_branch"] = None
        if token_file:
            secret = self.read_secret(token_file)
            if not username or any(char.isspace() for char in username):
                raise ValueError("Invalid repository username.")
            if not new["url"].startswith("https://"):
                raise ValueError("HTTPS access tokens require an HTTPS repository URL.")
            self._remove_credentials()
            atomic_write(self.config_dir / "repo.token", secret + "\n")
            new.update(auth="token", username=username)
        elif ssh_key:
            if not known_hosts:
                raise ValueError("Supply a verified --known-hosts file with an SSH deploy key.")
            if new["url"].startswith("https://"):
                raise ValueError("SSH deploy keys require an SSH repository URL.")
            key = self.read_secret(ssh_key, multiline=True)
            hosts = self.read_secret(known_hosts, multiline=True)
            self._remove_credentials()
            atomic_write(self.config_dir / "repo.key", key + "\n")
            atomic_write(self.config_dir / "repo.known_hosts", hosts + "\n")
            new["auth"] = "ssh"
        elif clear_credentials:
            self._remove_credentials()
            new["auth"] = "none"
        if signers_file:
            atomic_write(self.config_dir / "repo.allowed_signers", self.read_secret(signers_file, multiline=True) + "\n")
            new["signing"] = "ssh"
        elif clear_signatures:
            (self.config_dir / "repo.allowed_signers").unlink(missing_ok=True)
            new["signing"] = "none"
        new.setdefault("signing", "none")
        write_json(self.config_dir / "source.json", new)
        return new

    def _remove_credentials(self) -> None:
        for name in ("repo.token", "repo.key", "repo.known_hosts", "git-askpass"):
            (self.config_dir / name).unlink(missing_ok=True)

    # Releases

    def release(self, revision: str) -> Path:
        return self.root / "releases" / valid_revision(revision)

    def features(self, revision: str) -> ReleaseFeatures:
        return ReleaseFeatures.of(self.release(revision))

    def services(self, settings: dict[str, Any]) -> list[Service]:
        return profile.services(settings, self.features(settings["revision"]))

    def names(self, settings: dict[str, Any]) -> list[str]:
        return [service.name for service in self.services(settings)]

    def prepared_by_older_updater(self, release: Path) -> bool:
        """A release that ships dependency locks, prepared by an updater from before 1.7, and not running.

        That updater left it behind when its update failed: a virtual
        environment resolved from ``requirements.txt`` ranges without hashes
        and a tenant image built on a cached base without fresh distribution
        updates. An update prepares it again rather than deploying it so.
        """
        if not (release / "requirements.lock").is_file():
            return False
        if (read_json(release / ".release.json", {}) or {}).get("prepared_by"):
            return False
        try:
            return (self.root / "current").resolve() != release.resolve()
        except OSError:
            return False

    def preparation_bytes(self, settings: dict[str, Any]) -> int:
        """What preparing a new release is expected to take on the staging volume: what the running one takes.

        Its tree (the virtual environment included) when it is on that volume,
        and on hosting servers its tenant image when Docker keeps images there.
        What cannot be measured counts as nothing.
        """
        staging = self.root / "staging"
        total = 0
        try:
            running = (self.root / "current").resolve()
            if os.stat(running).st_dev == os.stat(staging).st_dev:
                for directory, _directories, files in os.walk(running):
                    for name in files:
                        try:
                            total += os.lstat(os.path.join(directory, name)).st_blocks * 512
                        except OSError:
                            continue
        except OSError:
            pass
        if settings["mode"] == "hosting":
            total += self.system.image_bytes(f"{TENANT_IMAGE}:{settings['revision']}", staging)
        return total

    def stage(self, settings: dict[str, Any], *, archive: Path | None = None, allow_unpinned: bool = False,
              reprepare: bool = False) -> Path:
        """Prepare a release once; *reprepare*: again when an older updater prepared it (see above)."""
        revision = settings["revision"]
        release = self.release(revision)
        if (release / ".release.json").exists():
            if not (reprepare and self.prepared_by_older_updater(release)):
                return release
            print("This release was prepared by an older updater (without its dependency locks); preparing it "
                  "again.", file=sys.stderr, flush=True)
        if release.exists():
            if (self.root / "current").resolve() == release.resolve():
                raise ValueError("The current release is incomplete; restore a backup before updating.")
            shutil.rmtree(release)
        release.mkdir(mode=0o755, parents=True)
        archive_file = self.root / "staging" / (revision + ".tar.gz")
        archive_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if archive:
                shutil.copyfile(archive, archive_file)
            else:
                GitSource(self.root, self.source()).archive(revision, archive_file)
            try:
                extract_archive(archive_file, release, max_bytes=4 * 1024 ** 3)
            except tarfile.TarError as error:
                raise ValueError("The selected revision produced no readable source. Choose a compatible branch.") from error
            if not (release / "banana").is_file() or not (release / "LICENSE").is_file():
                raise ValueError("The selected revision does not support the managed lifecycle. Choose a compatible branch.")
            unpinned = check_dependency_locks(release, allow_unpinned=allow_unpinned)
            if unpinned:
                print("Warning: this release has no dependency locks, so its dependencies are installed from the "
                      "ranges in requirements.txt, without hash checks.", file=sys.stderr, flush=True)
            warnings = self.system.prepare_release(settings, release, ReleaseFeatures.of(release)) or []
            shutil.copyfile(archive_file, release / ".source.tar.gz")
            # Kept with the release: an update that reuses it reports them again.
            write_json(release / ".release.json", {"revision": revision, "product": PRODUCT,
                                                   "prepared_by": PREPARED_BY,
                                                   **({"warnings": warnings} if warnings else {}),
                                                   **({"unpinned": True} if unpinned else {})})
            return release
        except BaseException:
            if release.exists():
                shutil.rmtree(release)
            raise
        finally:
            archive_file.unlink(missing_ok=True)

    def switch(self, revision: str) -> None:
        release = self.release(revision)
        if not (release / ".release.json").exists():
            raise ValueError("The requested release has not been prepared.")
        temporary = self.root / (".current-" + secrets.token_hex(5))
        temporary.symlink_to(Path("releases") / revision)
        os.replace(temporary, self.root / "current")

    def write_runtime(self, settings: dict[str, Any], previous: dict[str, str] | None = None) -> None:
        current = read_environment(self.config_dir / "app.env") if previous is None else previous
        write_environment(self.config_dir / "app.env",
                          profile.environment(settings, current, self.features(settings["revision"])))
        (self.root / "data/logs").mkdir(mode=0o700, parents=True, exist_ok=True)
        if settings["mode"] == "hosting":
            profile.prepare_hosting_storage(self.root / "data")
            if not any((self.root / "site").iterdir()) and (self.root / "current/site").is_dir():
                shutil.copytree(self.root / "current/site", self.root / "site", dirs_exist_ok=True)
        self.system.data_permissions(settings)

    def install_units(self, settings: dict[str, Any]) -> None:
        self.system.install_units(settings, self.services(settings), self.features(settings["revision"]))

    # Reverse proxy

    def proxy_plan(self, settings: dict[str, Any]) -> tuple[str | None, str | None]:
        """``(new Caddyfile, warning)`` for a hosting release that routes wikis through the runtime agent.

        Tenant hostnames reach their containers only through the routes file
        the agent writes and the managed Caddyfile imports. A Caddyfile this
        installation installed (``proxy --install``, 1.4 or 1.6: its digest is
        in ``config/proxy.json``) is re-rendered; one the operator wrote is
        left alone, with a warning when it does not import the routes.
        """
        if (settings["mode"] != "hosting" or not settings.get("domain")
                or not self.features(settings["revision"]).runtime_agent):
            return None, None
        destination = self.system.proxy_file
        if destination.is_symlink() or not destination.is_file():
            return None, None
        routes = self.system.routes_directory(settings)
        record = read_json(self.config_dir / "proxy.json", {}) or {}
        current = destination.read_bytes()
        if record.get("installed_sha256") != digest_file(destination):
            if f"import {routes}/".encode() in current:
                return None, None
            return None, (f"{destination} was not installed by this server and does not import {routes}/*.caddy, "
                          "so wikis are not reachable; merge the output of 'proxy' into it.")
        options = caddy.options_for(record, self.system.caddy_version())
        text = caddy.render(settings, email=str(record.get("email") or ""), routes=str(routes), options=options)
        return (None if text.encode() == current else text), None

    def refresh_proxy(self, settings: dict[str, Any], undo: dict[str, Any], *,
                      persist: Callable[[], None] = lambda: None) -> dict[str, Any]:
        """Create the agent's routes directory and re-render a managed Caddyfile; reload Caddy only on change.

        *undo* receives what :meth:`restore_proxy` needs (JSON, so a journal can
        carry it across a crash): the saved previous file and ``proxy.json``;
        *persist* is called whenever it grows, before the change it describes.
        """
        details: dict[str, Any] = {}
        if settings["mode"] != "hosting" or not self.features(settings["revision"]).runtime_agent:
            return details
        routes = self.system.routes_directory(settings)
        if not routes.is_dir():
            undo["routes_created"] = str(routes)
            persist()
            self.system.ensure_routes_directory(settings)
        text, warning = self.proxy_plan(settings)
        if warning:
            details["proxy_warning"] = warning
        if text is None:
            return details
        destination = self.system.proxy_file
        record = read_json(self.config_dir / "proxy.json", {}) or {}
        saved = self.config_dir / PROXY_ROLLBACK
        atomic_write(saved, destination.read_bytes())
        undo.update(caddyfile=str(saved), proxy_record=record)
        persist()
        candidate = self.config_dir / "Caddyfile.next"
        atomic_write(candidate, text)
        try:
            self.system.validate_proxy(candidate)
        finally:
            candidate.unlink(missing_ok=True)
        atomic_write(destination, text, 0o644)
        write_json(self.config_dir / "proxy.json", {**record, "installed_sha256": digest_file(destination)})
        self.system.reload_proxy()
        details["proxy_updated"] = True
        return details

    def restore_proxy(self, undo: dict[str, Any] | None) -> None:
        """Put back the Caddyfile and routes directory :meth:`refresh_proxy` changed (idempotent)."""
        if not undo:
            return
        saved = Path(undo["caddyfile"]) if undo.get("caddyfile") else None
        if saved is not None and saved.is_file() and saved.parent == self.config_dir:
            previous = saved.read_bytes()
            destination = self.system.proxy_file
            if not destination.is_file() or destination.read_bytes() != previous:
                atomic_write(destination, previous, 0o644)
                self.system.reload_proxy(check=False)
            write_json(self.config_dir / "proxy.json", undo.get("proxy_record") or {})
            saved.unlink()
        if undo.get("routes_created"):
            self.system.remove_routes_directory(Path(undo["routes_created"]))

    # Install

    def install(self, checkout: Path, *, mode: str, name: str | None = None, domain: str = "",
                portal_domain: str = "", port: int | None = None, source_options: dict[str, Any] | None = None,
                reuse_data: bool = False, tenant_quotas_mode: str | None = None) -> dict[str, Any]:
        self.layout()
        with maintenance_lock(self.root):
            existing = read_json(self.config_dir / "installation.json")
            if existing and (existing.get("installed") or not reuse_data):
                raise ValueError("An installation already exists. Use update, restore, or install --reuse-data "
                                 "after uninstalling.")
            if existing and existing.get("mode") != mode:
                raise ValueError("Changing deployment mode requires a separate root and an explicit data migration.")
            if mode not in profile.MODES:
                raise ValueError("Choose a supported deployment mode (wiki or hosting).")
            name = profile.validate_service_name(name or PRODUCT.lower())
            domain = profile.validate_domain(domain)
            portal_domain = profile.validate_domain(portal_domain or ("portal." + domain if mode == "hosting" and domain
                                                                      else ""))
            port = profile.validate_port(port or profile.DEFAULT_PORTS[mode])
            environment = read_environment(self.config_dir / "app.env")
            if mode == "hosting":
                # auto unless the operator chooses: hard per-wiki quotas wherever the storage enforces them.
                chosen = (tenant_quotas_mode or environment.get(tenant_quotas.VARIABLE)
                          or tenant_quotas.INSTALL_DEFAULT)
                environment[tenant_quotas.VARIABLE] = tenant_quotas.setting({tenant_quotas.VARIABLE: chosen})
            elif tenant_quotas_mode:
                raise ValueError("--tenant-quotas applies to hosting installations.")
            source = self.configure_source(**(source_options or {}))
            settings = profile.new_settings(self.root, mode=mode, service=name, domain=domain,
                                            portal_domain=portal_domain, port=port, source_url=source["url"])
            self.system.account(settings)
            if mode == "hosting":
                self.system.check_buildkit(settings)
                storage = self.check_new_platform_storage(settings, environment[tenant_quotas.VARIABLE])
            settings["revision"] = GitSource(self.root, source).seed(checkout)
            self.system.preflight(settings, profile.services(settings, ReleaseFeatures(True, True)))
            self.stage(settings)
            self.save(settings)
            self.switch(settings["revision"])
            self.write_runtime(settings, environment)
            write_json(self.config_dir / "updates.json", DEFAULT_POLICY.copy())
            self.install_units(settings)
            self.system.install_timer(settings, self.policy())
            atomic_write(self.root / "data" / MAINTENANCE_MARKER, "Installing\n")
            services = self.services(settings)
            self.system.start([service.name for service in services])
            if not self.system.readiness(settings, services).ready:
                self.event("install", "failed", revision=settings["revision"],
                           reason="Initial health checks failed; maintenance mode remains active.")
                raise RuntimeError("Initial health checks failed. Inspect the service journal, correct the "
                                   "configuration, and run install --reuse-data.")
            settings["installed"] = True
            self.save(settings)
            (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)
            details = {"tenant_storage": storage.to_dict(), **storage_details(storage)} if mode == "hosting" else {}
            return self.event("install", "complete", revision=settings["revision"], automatic_updates=False,
                              **details)

    def check_new_platform_storage(self, settings: dict[str, Any], mode: str) -> tenant_quotas.Policy:
        """Say which per-wiki storage limits a new hosting platform will have, before anything is installed.

        Any storage will do: with ``auto`` (the default) the wikis get hard
        quotas where the storage enforces them (XFS mounted with
        ``prjquota``), and are otherwise held to their plans by the
        application. Only ``required``, the operator's explicit choice,
        refuses storage without them. Checked: ``data/instances`` when the
        operator mounted it already, else ``data/`` (where it will be
        created). Prints which limits apply and, without hard quotas, how to
        get them; returns the policy.
        """
        instances = self.root / "data/instances"
        storage = instances if instances.is_dir() else self.root / "data"
        problem = self.system.storage_problem(storage) if mode == "required" else None
        if problem:
            raise TenantStorageUnsupported(
                f"The wiki storage {storage} does not enforce XFS project quotas ({problem}), and "
                f"{tenant_quotas.VARIABLE}=required asks for hard per-wiki byte and inode limits. Put the "
                "installation's data/ directory on an XFS filesystem mounted with prjquota (see docs/hosting.md), "
                "or install with --tenant-quotas auto (the default: hard quotas where the storage enforces them, "
                "and otherwise each wiki is held to its plan by the application's own checks). Nothing was "
                "installed.")
        policy = (tenant_quotas.Policy("required", True) if mode == "required"
                  else self.system.tenant_storage(settings, configured=mode, storage=storage))
        if policy.refused:
            raise TenantStorageUnsupported(f"{policy.reason} Nothing was installed.")
        if policy.hard_quotas:
            print(f"Hard per-wiki storage quotas will be enforced ({tenant_quotas.VARIABLE}={policy.setting}): the "
                  "wiki storage is XFS mounted with prjquota.", file=sys.stderr, flush=True)
        print_storage(policy)
        return policy

    # Transactions

    def journal_state(self, settings: dict[str, Any]) -> dict[str, Any]:
        names = self.names(settings)
        # The tenant containers are recorded by quiesce(), once nothing can stop or remove them any more.
        return {"settings": settings, "services": names, "active": [name for name in names if self.system.active(name)],
                "containers": [], "serving": [], "backup": None, "snapshot": None, "candidate": None,
                "phase": "preparing"}

    @staticmethod
    def serving(journal: dict[str, Any]) -> list[str]:
        """The wikis that served when the transaction began (older journals: those with a running container)."""
        if "serving" in journal:
            return list(journal["serving"])
        return [item["data_dir"] for item in journal.get("containers") or [] if item.get("running")]

    def tenant_maintenance(self, settings: dict[str, Any], enabled: bool) -> None:
        instances = self.root / "data/instances"
        if settings["mode"] != "hosting" or not instances.is_dir():
            return
        for directory in instances.iterdir():
            if directory.is_dir() and not directory.is_symlink():
                # The tenant owns this directory: whatever it put under the marker's name must
                # neither redirect a root write nor fail (and so block) the update for everyone.
                try:
                    untrusted_marker(directory, MAINTENANCE_MARKER,
                                     "Hosting maintenance in progress\n" if enabled else None)
                except OSError as error:
                    log.warning("Maintenance marker not %s for tenant %s: %s", "set" if enabled else "removed",
                                directory.name, error)

    def quiesce(self, journal: dict[str, Any]) -> None:
        settings = journal["settings"]
        write_json(self.config_dir / "transaction.json", journal)
        atomic_write(self.root / "data" / MAINTENANCE_MARKER, "Maintenance in progress\n")
        self.tenant_maintenance(settings, True)
        self.system.stop(journal["services"])
        # Listed once the maintenance service has stopped: until then it could stop or remove wikis.
        journal["containers"] = self.system.containers(settings)
        # Probed with the maintenance markers set: a wiki that does not serve in maintenance mode now is not
        # expected to serve in it after the operation either.
        journal["serving"] = self.system.serving_tenants(journal["containers"])
        write_json(self.config_dir / "transaction.json", journal)
        self.system.stop_containers(journal["containers"])

    def finish(self, journal: dict[str, Any], settings: dict[str, Any], *,
               required: bool = False, quarantine: bool = False,
               isolate: bool = False) -> tuple[list[str], list[str]]:
        """Start *settings*' services, wait for them and the wikis that served before, end the transaction.

        The application must become ready. For a wiki that served when the
        transaction began and does not serve again:

        * *required*: the operation fails and is undone;
        * *quarantine*: first try again with its plugins quarantined
          (:meth:`quarantine_plugins`);
        * *isolate*: if only a few still fail (:func:`isolatable`), stop them
          and record them in ``journal["isolated"]`` (name -> why).

        Without *required* such a wiki is only reported.

        Returns ``(wikis waited for that do not serve, data directories of the
        wikis running without serving at the start)``. The caller checks the
        latter with :meth:`check_tenants` once the operation is complete.
        """
        # Containers that quiesce stopped are never started again as they are:
        # the agent starts each wiki behind a mount gate that only its own
        # tenant.start releases, so ``docker start`` would bring them back
        # inert. Every operation removes them before this point, and the
        # maintenance service started here restarts the running wikis through
        # the agent.
        services = self.services(settings)
        names = [service.name for service in services]
        self.system.start(names)
        serving = self.serving(journal)
        readiness = self.system.readiness(settings, services, serving)
        if readiness.platform:
            raise RuntimeError("The application failed its readiness checks.")
        unready = tenant_names(readiness.tenants)
        if unready and required:
            failing = readiness.tenants
            quarantined = (self.quarantine_plugins(journal, settings, services, failing, isolate=isolate)
                           if quarantine else None)
            if quarantined is not None:
                failing = quarantined  # what still fails with the plugins quarantined (or not quarantinable)
            if failing and (not isolate or not isolatable(len(failing), len(serving))):
                refused = journal.get("quarantine_failed") or {}
                if refused:  # as without the isolate policy (see quarantine_plugins)
                    directory = min(refused)
                    raise RuntimeError(f"The plugins of the wiki {Path(directory).name} could not be quarantined: "
                                       f"{refused[directory]}")
                listed = tenant_names(failing)
                raise RuntimeError("Wikis that were running failed their readiness checks"
                                   + (" also with their plugins quarantined" if quarantined else "") + ": "
                                   + ", ".join(listed[:20])
                                   + (f" and {len(listed) - 20} more" if len(listed) > 20 else "") + ".")
            if failing:
                journal["isolated"] = self.isolate(settings, failing)
            unready = tenant_names(failing)
        others = {item["data_dir"] for item in journal["containers"] if item.get("running")} - set(serving)
        known = journal.get("services") or names
        self.system.stop([name for name in names if name in known and name not in journal["active"]])
        self.tenant_maintenance(settings, False)
        (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)
        (self.config_dir / "transaction.json").unlink(missing_ok=True)
        return unready, sorted(others)

    def isolate(self, settings: dict[str, Any], failed: dict[str, str]) -> dict[str, str]:
        """Stop the wikis of *failed* (data directory -> why it does not serve) for good; return them by name.

        Left stopped, so their users see the platform's "unavailable" page
        rather than a wiki half started under the new release. In this order:

        1. recorded in the portal's ``isolated-wikis.json``, so its recovery
           (now, or after a restart of its maintenance service or the host)
           never starts them again by itself;
        2. stopped through the runtime agent, which waits for a start in
           progress (the portal may still be starting one, during a slow
           migration) rather than racing it;
        3. verified: none of them has a running container any more.

        A wiki that keeps running fails the operation, which is then rolled
        back: a wiki is never reported isolated while it serves. Their portal
        status is unchanged: once the cause (a full storage quota, data a
        migration refuses) is fixed, starting the wiki from the portal or
        ``restart`` brings it back and lifts the isolation.
        """
        names = {Path(directory).name: str(reason) for directory, reason in sorted(failed.items())}
        try:
            self.system.mark_isolated(settings, names)
        except (OSError, ValueError) as error:
            log.warning("The isolated wikis could not be recorded for the portal: %s", error)
        running: list[str] = []
        for attempt in range(ISOLATION_ATTEMPTS):
            try:
                self.system.stop_tenants(settings, list(failed))
                running = tenant_names(item["data_dir"] for item in self.system.containers(settings)
                                       if item.get("data_dir") in failed and item.get("running"))
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                running = sorted(names)
                log.warning("The wikis to isolate could not all be stopped (attempt %d): %s", attempt + 1, error)
            if not running:
                return dict(sorted(names.items()))
        raise RuntimeError("These wikis did not serve under the new release and could not be stopped, so they were "
                           "not isolated: " + ", ".join(running) + ".")

    def quarantine_plugins(self, journal: dict[str, Any], settings: dict[str, Any], services: list[Service],
                           failing: dict[str, str], *, isolate: bool = False) -> dict[str, str] | None:
        """Quarantine the plugins of the wikis *failing* after an update, when their own code may be why.

        A wiki's own plugin code can fail under a new release, and rolling the
        update back for that would keep later releases, security fixes
        included, off the platform. So when every failing wiki has plugins
        not yet quarantined, each is quarantined as the portal's
        administrators would (:meth:`.System.quarantine_plugins`) and waited
        for again.

        Returns the wikis that still do not serve (data directory -> why;
        empty: the update stands), or None when a failing wiki has no plugins
        to quarantine, so the caller rolls back or, with policy ``isolate``,
        isolates them. The journal lists ``quarantined``, ``quarantine_failed``
        and ``quarantine_marked`` (marker in force, plugins still switched on
        in the wiki's database).

        A quarantine that cannot be applied fails the update, unless
        *isolate*: that wiki then counts as failing, and :meth:`finish`
        isolates it with the rest or fails the update when more wikis fail
        than isolation takes.
        """
        try:
            candidates = self.system.plugin_quarantine_candidates(settings, failing)
        except Exception as error:  # noqa: BLE001 - the portal database unreadable: roll back
            log.warning("The wikis' plugins could not be checked for a quarantine: %s", error)
            return None
        if not candidates or candidates.keys() != failing.keys():
            return None
        journal["quarantined"] = sorted(candidates)
        write_json(self.config_dir / "transaction.json", journal)
        refused: dict[str, str] = {}
        marked: list[str] = []
        for directory, instance_id in sorted(candidates.items()):
            name = Path(directory).name
            log.warning("Wiki %s does not serve under the new release; quarantining its plugins.", name)
            try:
                self.system.quarantine_plugins(settings, instance_id)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                if not isolate:
                    raise RuntimeError(f"The plugins of the wiki {name} could not be quarantined: {error}") from error
                log.warning("The plugins of the wiki %s could not be quarantined: %s", name, error)
                refused[directory] = str(error)[:500]
                try:
                    if self.system.plugin_quarantine_in_force(settings, instance_id):
                        marked.append(directory)
                except (OSError, ValueError) as check:
                    log.warning("The plugin quarantine of the wiki %s could not be checked: %s", name, check)
        applied = [directory for directory in sorted(candidates) if directory not in refused]
        if refused:
            journal.update(quarantined=applied, quarantine_failed=refused, quarantine_marked=marked)
            write_json(self.config_dir / "transaction.json", journal)
        still: dict[str, str] = {}
        if applied:  # a wiki whose quarantine failed is not waited for once more
            readiness = self.system.readiness(settings, services, applied,
                                              timeout=self.system.readiness_timeout(settings, len(applied)))
            if readiness.platform:
                raise RuntimeError("The application failed its readiness checks.")
            still = dict(readiness.tenants)
        return {**still, **{directory: ("its plugin quarantine is in force, but its plugins could not be switched "
                                        f"off in its database: {error}" if directory in marked
                                        else f"its plugins could not be quarantined: {error}")
                            for directory, error in refused.items()}}

    def check_tenants(self, settings: dict[str, Any], directories: Collection[str] | None, *,
                      waited: Collection[str] = ()) -> list[str]:
        """The wikis of *directories* still running in the portal database that do not serve now (one probe each).

        *directories* None means every wiki running in the portal database
        (none of them served when the operation began: after ``stop``, or on
        a new server), except those already *waited* for. Only a report, made
        once an operation is complete: whatever goes wrong here leaves the
        operation and its result as they are.
        """
        if settings["mode"] != "hosting" or directories is not None and not directories:
            return []
        platform: list[str] = []  # Docker or the portal database not answering, for example
        try:
            if directories is None:
                directories = self.system.expected_tenant_directories(settings)
            directories = set(directories) - set(waited)
            issues = (self.system.tenant_issues(settings, self.services(settings), directories, platform)
                      if directories else {})
        except Exception as error:  # noqa: BLE001 - never turns a completed operation into a failed one
            platform.append(str(error))
            issues = {}
        if platform:
            log.warning("Once the operation was complete, the wikis were not fully checked: %s", "; ".join(platform))
        return tenant_names(issues)

    def allowed_link(self, settings: dict[str, Any]) -> Callable[[Path], bool] | None:
        if settings["mode"] != "hosting":
            return None
        data = self.root / "data"
        return lambda path: profile.inside_tenant(path, data)

    def preflight(self, settings: dict[str, Any], *revisions: str) -> None:
        """Refuse an operation (:class:`~.files.Refused`) before it stops anything.

        Docker must answer, and the wikis running now must be able to start
        again under each release of *revisions* (default: the current one):
        under ``BW_HOSTING_TENANT_QUOTAS`` for releases that honour it, on
        storage enforcing XFS project quotas for those with project quotas
        that predate it.
        """
        self.system.check_docker(settings)
        quotas = [features for features in map(self.features, revisions or (settings["revision"],))
                  if features.project_quotas]
        if quotas:
            self.system.check_tenant_storage(settings, required=any(not features.quota_modes for features in quotas))

    def guarded(self, settings: dict[str, Any], body: Callable[[dict[str, Any]], Any], *,
                candidate: str | None = None, target: str | None = None) -> tuple[Any, Snapshot]:
        """Snapshot (live), quiesce, refresh, run *body*; on any failure put everything back.

        *target* is the release that runs once *body* succeeded (default:
        *candidate*, else the current one). Returns ``(body result,
        snapshot)``; the caller writes the package from the snapshot once the
        service is back and then removes it.
        """
        # Before anything stops. The journal's list of containers is taken only in quiesce, once the maintenance
        # service is stopped.
        max_files = self.package_limit()
        self.preflight(settings, settings["revision"], target or candidate or settings["revision"])
        # Staged next to the wikis (hosting), the copy must leave their admitted storage reservations free.
        reserve = self.system.staging_reserve(settings, self.root / "staging")
        snapshot = Snapshot.create(self.root, self.allowed_link(settings), max_files=max_files, reserve=reserve)
        journal = self.journal_state(settings)
        journal.update(candidate=candidate, snapshot=str(snapshot.path))
        try:
            self.quiesce(journal)
            snapshot.refresh()
            journal["phase"] = "snapshotted"
            write_json(self.config_dir / "transaction.json", journal)
            result = body(journal)
        except BaseException:
            try:
                self.recover()
            finally:
                if not (self.config_dir / "transaction.json").exists():
                    snapshot.remove()
            raise
        return result, snapshot

    def package_from(self, snapshot: Snapshot, settings: dict[str, Any], destination: Path) -> Path:
        """Write a package from *snapshot*, remove the snapshot, then verify the package.

        The package is only returned (recorded as the rollback target, uploaded
        and followed by remote pruning) once :func:`read_package` accepts it,
        exactly as a restore would. The snapshot is removed first, so the
        verification does not need space for both. The package limit is the
        one the snapshot was taken under (a restore replaces ``app.env``).
        """
        source_archive = self.release(settings["revision"]) / ".source.tar.gz"
        inputs = [*snapshot.inputs(), (source_archive, "source.tar.gz")]
        try:
            package = write_package(destination, {
                "product": PRODUCT, "mode": settings["mode"], "revision": settings["revision"],
                "created_at": _now(), "old_root": str(self.root), "model_weights_excluded": False,
                "excluded_paths": [],
            }, inputs, max_files=snapshot.max_files)
        finally:
            snapshot.remove()
        self.verify_package(package, snapshot.max_files)
        return package

    def verify_package(self, package: Path, max_files: int) -> None:
        """Refuse (and delete) a package that a restore would refuse."""
        try:
            with read_package(package, PRODUCT, self.root / "staging", max_files=max_files):
                pass
        except Exception as error:
            package.unlink(missing_ok=True)
            raise ValueError(f"The new package failed verification and was removed: {error}") from error

    def backup_name(self, prefix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.root / "backups" / f"{prefix}-{stamp}-{secrets.token_hex(3)}.tar.gz"

    def _begin(self) -> None:
        """Every locked operation: finish an interrupted transaction, drop stale snapshots and partial trees."""
        self.remove_partial_trees()
        self.recover()
        self.remove_partial_trees()
        for path in stale_snapshots(self.root, None):
            shutil.rmtree(path)

    def remove_partial_trees(self) -> None:
        """Remove what an interrupted :meth:`apply_tree` left next to ``data/`` and ``site/``.

        Only once that tree is in place: interrupted between its two renames,
        the leftovers are all there is until a recovery puts it back.
        """
        for name in ("data", "site"):
            if not (self.root / name).is_dir():
                continue
            for path in (*self.root.glob(f".restore-{name}-*"), *self.root.glob(f".previous-{name}-*")):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)

    # Backup

    def backup(self, destination: Path | None = None, *, prefix: str = "manual") -> Path:
        with maintenance_lock(self.root):
            self._begin()
            settings = self.settings()
            destination = Path(destination).absolute() if destination else self.backup_name(prefix)
            for directory in (self.root / "data", self.root / "site", self.root / "releases", self.root / "staging"):
                if destination.is_relative_to(directory):
                    raise ValueError("Write backup packages outside the data, site, staging and release directories.")

            def body(journal: dict[str, Any]) -> tuple[list[str], list[str]]:
                # Removed, not started again: see finish().
                self.system.remove_containers(journal["containers"])
                return self.finish(journal, settings)

            (unready, others), snapshot = self.guarded(settings, body)
            try:
                result = self.package_from(snapshot, settings, destination)
            finally:
                snapshot.remove()
            unready = sorted({*unready, *self.check_tenants(settings, others)})
            details: dict[str, Any] = {"unready_tenants": unready} if unready else {}
            try:
                self.prune_backups()
            except (OSError, ValueError) as error:
                details["pruning_warning"] = f"Backup pruning failed: {error}"
            self.event("backup", "complete", package=str(result), **details)
            return result

    # Update

    def update(self, *, automatic: bool = False, allow_divergent: bool = False,
               retry_failed: bool = False, allow_unpinned: bool = False) -> dict[str, Any]:
        with maintenance_lock(self.root):
            self._begin()
            settings = self.settings()
            if automatic and not self.policy()["enabled"]:
                return {"outcome": "disabled"}
            if automatic and not self.system.active(settings["service"]):
                return self.event("update", "skipped", reason="The application was intentionally stopped.")
            source = self.source()
            git = GitSource(self.root, source)
            sha, selected, forward = git.resolve(settings["revision"])
            # What was deployed: the release, or the branch (and whether it was the fallback).
            followed = ({"release": selected} if source["track"] == "tags"
                        else {"branch": selected, "used_fallback": selected != source["branch"]})
            if git.skipped_releases:
                # Anyone who can push tags can push one that does not verify: reported, never deployed.
                followed["skipped_releases"] = git.skipped_releases
                print(f"Warning: the update source has release tags above {selected} that are not signed by a key "
                      f"in the allowed signers file, so they were skipped: {', '.join(git.skipped_releases)}. "
                      "Check who pushed them.", file=sys.stderr, flush=True)
            if self.failed_revision(settings, git) is None:
                # A revision the platform has moved past (an updater from before 1.7 never removed its record
                # after a later update succeeded): no update can offer it again.
                (self.config_dir / FAILED_REVISION).unlink(missing_ok=True)
            if sha == settings["revision"]:
                shown = {key: value for key, value in followed.items() if key != "used_fallback"}
                failure = self.failed_convergence(settings) if automatic and not retry_failed else None
                if failure:
                    # Installing these units failed here before: every attempt stops the services and waits out
                    # the readiness timeout, so only an operator's update (or restart) tries again.
                    return self.event("update", "paused", revision=sha, **shown, units_converged=False,
                                      reason="Installing this release's systemd units failed before"
                                             f" ({str(failure.get('reason') or 'no reason recorded')[:500]}); "
                                             "automatic updates do not try again. Fix the cause, then run "
                                             "'update' (or 'restart').")
                converged = self.converge(settings) or {}
                return self.event("update", "current", revision=sha, **shown,
                                  units_converged=bool(converged.get("units")),
                                  **{key: value for key, value in converged.items() if key != "units"})
            if not forward and (automatic or not allow_divergent):
                return self.event("update", "paused", revision=sha,
                                  reason="The selected source diverges from the installed revision. Review it and "
                                         "use update --allow-divergent for an intentional switch.")
            failed = read_json(self.config_dir / FAILED_REVISION, {}) or {}
            if automatic and failed.get("revision") == sha and not retry_failed:
                cause = f" ({str(failed['reason'])[:500]})" if failed.get("reason") else ""
                return self.event("update", "paused", revision=sha,
                                  reason=f"This revision previously failed{cause}; a maintainer must retry it "
                                         "(update, or update --retry-failed) or select a newer revision.")
            candidate = {**settings, "revision": sha, "source_url": source["url"]}
            try:
                # Before stage(): building the tenant image needs Docker (with BuildKit), and a build that fails
                # because of the host would otherwise be recorded as this revision's failure. An invalid package
                # limit, unready-wikis policy or tenant quota setting is refused here too, rather than once the
                # release is built.
                max_files = self.package_limit()
                isolate = settings["mode"] == "hosting" and self.unready_policy() == "isolate"
                self.system.check_docker(settings)
                self.system.check_buildkit(settings)
                if settings["mode"] == "hosting":
                    self.system.tenant_quota_setting(settings)
                # And whatever the snapshot would refuse once the release is prepared (too many files, too little
                # free space, space promised to the hosted wikis): on a disk short of space, every new revision
                # would otherwise build a release and a tenant image, about a gigabyte, only to be refused.
                release = self.release(sha)
                preparing = (self.preparation_bytes(settings) if not (release / ".release.json").exists()
                             or self.prepared_by_older_updater(release) else 0)
                Snapshot.check(self.root, self.allowed_link(settings), max_files=max_files,
                               reserve=self.system.staging_reserve(settings, self.root / "staging"),
                               preparing=preparing)
            except Refused as error:
                self.event("update", "failed", revision=sha, reason=str(error))
                raise
            try:
                self.stage(candidate, allow_unpinned=allow_unpinned and not automatic, reprepare=True)
            except BaseException as error:
                self.record_failed(sha, "preparation", f"Release preparation failed: {error}")
                self.event("update", "failed", revision=sha,
                           reason="Release preparation failed; the running version was left in place.",
                           **self.discard(sha))
                raise
            if automatic and not self.policy()["enabled"]:
                return self.event("update", "cancelled", reason="Automatic updates were disabled while preparing.")
            features = self.features(sha)
            details: dict[str, Any] = {}
            if features.candidate_preflight:
                # What the new release itself needs from this server, asked before anything stops: the installed
                # controller cannot know what a newer release refuses once it runs.
                try:
                    answer = self.system.candidate_preflight(candidate, self.release(sha))
                except CandidateRefused as error:
                    self.record_failed(sha, "preflight", str(error))
                    self.event("update", "failed", revision=sha, reason=str(error), **self.discard(sha))
                    raise
                except CandidateUnavailable as error:
                    # Docker restarting, a slow host: not this revision's failure, so the next run asks again.
                    self.event("update", "failed", revision=sha, reason=str(error), **self.discard(sha))
                    raise
                # Its warnings, and its notes about a supported configuration (wiki storage without hard quotas).
                for key, label in (("warnings", "Warning"), ("notes", "Note")):
                    listed = answer.get(key)
                    messages = [item[:2000] for item in (listed if isinstance(listed, list) else ())
                                if isinstance(item, str)][:20]
                    if messages:
                        details["preflight_" + key] = messages
                        for message in messages:
                            print(f"{label}: {message}", file=sys.stderr, flush=True)

            still_quarantined: list[str] = []  # isolated wikis whose plugins the update quarantined first
            only_marked: list[str] = []  # isolated wikis whose quarantine is in force, plugins still switched on
            not_quarantined: list[str] = []  # isolated wikis whose plugins it could not quarantine

            def apply(journal: dict[str, Any]) -> tuple[list[str], list[str]]:
                self.system.remove_containers(journal["containers"])
                journal["containers_removed"] = True
                write_json(self.config_dir / "transaction.json", journal)
                self.switch(sha)
                environment = read_environment(self.config_dir / "app.env")
                if environment.get("BW_SOURCE_URL") == profile.source_link(settings["source_url"]):
                    environment["BW_SOURCE_URL"] = profile.source_link(candidate["source_url"])
                self.write_runtime(candidate, environment)
                self.install_units(candidate)
                self.save(candidate)
                journal["proxy"] = {}
                details.update(self.refresh_proxy(
                    candidate, journal["proxy"],
                    persist=lambda: write_json(self.config_dir / "transaction.json", journal)))
                # Required: every waited wiki serves, if need be with its own plugins quarantined, or (policy
                # "isolate") all but a few, which are isolated.
                others = self.finish(journal, candidate, required=True, quarantine=True, isolate=isolate)[1]
                isolated = journal.get("isolated") or {}
                if isolated:
                    details["isolated_tenants"] = sorted(isolated)
                # Reported as quarantined: the wikis that serve again thanks to it. An isolated one keeps its
                # quarantine too, which the warning about isolated wikis says.
                quarantined = journal.get("quarantined", [])
                still_quarantined[:] = tenant_names(item for item in quarantined if Path(item).name in isolated)
                marked = journal.get("quarantine_marked") or []
                only_marked[:] = tenant_names(marked)
                not_quarantined[:] = tenant_names(item for item in journal.get("quarantine_failed") or ()
                                                  if item not in marked)
                return others, [item for item in quarantined if Path(item).name not in isolated]

            prepared = read_json(self.release(sha) / ".release.json", {}) or {}
            if prepared.get("warnings"):
                details["image_warnings"] = prepared["warnings"]
            if prepared.get("unpinned"):
                details["dependencies_unpinned"] = True
            try:
                (others, quarantined), snapshot = self.guarded(settings, apply, candidate=sha)
            except Refused as error:
                # Nothing was stopped: not this revision's failure, so the next automatic run tries it again (and
                # prepares it again, unless the check before stage() refuses it first).
                self.event("update", "failed", revision=sha, reason=str(error), **self.discard(sha))
                raise
            except BaseException as error:
                self.record_failed(sha, "rollback", f"Rolled back: {str(error) or type(error).__name__}")
                self.event("update", "rolled_back", revision=sha, restored_revision=settings["revision"],
                           **self.discard(sha))
                raise
            (self.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
            (self.config_dir / FAILED_REVISION).unlink(missing_ok=True)
            (self.config_dir / FAILED_CONVERGENCE).unlink(missing_ok=True)  # the new release's units serve
            if settings["mode"] == "hosting" and features.project_quotas and features.quota_modes:
                try:
                    policy = self.system.tenant_storage(candidate)
                except (Refused, ValueError):
                    policy = None
                if policy is not None:
                    details.update(storage_details(policy))
                    # Said once: the new release's preflight usually printed the same just now.
                    print_storage(policy, printed=(*details.get("preflight_warnings", ()),
                                                   *details.get("preflight_notes", ())))
            if quarantined:
                details["quarantined_tenants"] = tenant_names(quarantined)
                self.event("update", "plugins_quarantined", revision=sha,
                           quarantined_tenants=details["quarantined_tenants"],
                           reason="These wikis served before the update and served under the new release only "
                                  "once their third-party plugins were quarantined. The portal's administrators "
                                  "can lift the quarantine once the plugins are fixed.")
            try:
                archive = self.package_from(snapshot, settings,
                                            self.backup_name("auto" if automatic else "before-update"))
                details["backup"] = str(archive)
                write_json(self.config_dir / "last-update.json", {
                    "backup": str(archive), "previous_revision": settings["revision"], "revision": sha,
                    **{key: details[key] for key in ("isolated_tenants", "quarantined_tenants") if key in details}})
            except (OSError, ValueError) as error:
                details["backup_warning"] = f"The rollback package could not be written: {error}"
            finally:
                snapshot.remove()
            self.prune_backups()
            kept = self.prune_releases({settings["revision"], sha})
            if settings["mode"] == "hosting":
                details["images_removed"] = self.system.prune_images(kept)
            unready = self.check_tenants(candidate, others)
            if unready:
                details["unready_tenants"] = unready
            if details.get("isolated_tenants"):
                print(f"Warning: these wikis served before the update but not under revision {sha[:12]}, so they "
                      f"were stopped and the other wikis kept the new release: {', '.join(details['isolated_tenants'])}"
                      ". Why each one failed is in config/last-readiness-failure.json and its container log. Fix the "
                      "cause (a full storage quota, for example), then start it again from the portal; 'rollback' "
                      "puts the whole platform back on the previous release."
                      + (" Their third-party plugins were quarantined first and stay quarantined until the portal's "
                         f"administrators lift it: {', '.join(still_quarantined)}." if still_quarantined else "")
                      + (" Their plugin quarantine is in force, but their plugins could not be switched off in their "
                         "database, so lifting it turns them on again (the reason is recorded with the isolation): "
                         f"{', '.join(only_marked)}." if only_marked else "")
                      + (" Their third-party plugins could not be quarantined either (the reason is recorded with "
                         f"the isolation): {', '.join(not_quarantined)}." if not_quarantined else ""),
                      file=sys.stderr, flush=True)
            return self.event("update", "complete", revision=sha, previous_revision=settings["revision"],
                              **followed, **details)

    def record_failed(self, revision: str, stage: str, reason: str) -> None:
        """Remember that *revision* failed and why: automatic updates skip it (no hourly downtime loop).

        Until a newer revision appears, or an operator runs ``update`` (or
        ``update --retry-failed``, which an automatic run also honours).
        *stage* is ``preparation``, ``preflight`` (the new release refused this
        server: nothing stopped) or ``rollback``. Controllers before 1.7
        wrote and read ``revision`` only.
        """
        write_json(self.config_dir / FAILED_REVISION,
                   {"revision": revision, "stage": stage, "reason": reason[:2000], "time": _now()})

    def discard(self, revision: str) -> dict[str, Any]:
        """Remove the release and tenant image a failed update prepared for *revision*, as far as possible.

        A failed attempt would otherwise leave them behind (about a gigabyte
        on a hosting server) for every revision it tried, until an update
        succeeded. Kept: the running release, the one ``rollback`` returns to
        (``previous_revision`` in ``config/last-update.json``), and everything
        while a transaction is recorded, since its recovery may need them.
        Returns what was removed (``discarded``) or kept (``cleanup_warning``)
        for the failure's event.
        """
        try:
            if (self.config_dir / "transaction.json").exists():
                return {}
            settings = self.settings()
            last = read_json(self.config_dir / "last-update.json", {}) or {}
            if revision in {settings["revision"], (self.root / "current").resolve().name,
                            last.get("previous_revision")}:
                return {}
        except (OSError, ValueError) as error:
            return {"cleanup_warning": f"What the failed update prepared for {revision[:12]} was kept: {error}"}
        discarded: list[str] = []
        kept: list[str] = []
        release = self.release(revision)
        if release.is_dir() and not release.is_symlink():
            try:
                shutil.rmtree(release)
                discarded.append(f"releases/{revision}")
            except OSError as error:
                kept.append(f"releases/{revision} ({error})")
        if settings["mode"] == "hosting":
            image = f"{TENANT_IMAGE}:{revision}"
            removed = self.system.remove_image(image)  # None: there is none (the build failed)
            if removed:
                discarded.append(image)
            elif removed is False:
                kept.append(f"{image} (Docker kept it)")
        details: dict[str, Any] = {}
        if discarded:
            details["discarded"] = discarded
            print(f"Removed what the failed update prepared: {', '.join(discarded)}.", file=sys.stderr, flush=True)
        if kept:
            details["cleanup_warning"] = f"What the failed update prepared could not all be removed: {', '.join(kept)}."
            print(f"Warning: {details['cleanup_warning']}", file=sys.stderr, flush=True)
        return details

    def failed_revision(self, settings: dict[str, Any], git: GitSource | None = None) -> dict[str, Any] | None:
        """The failed-revision record while it can still matter: None without one, or for a revision installed or
        contained in the installed one (an updater from before 1.7 never removed its record after a later update
        succeeded, and no update offers such a revision again).

        Asked of the local repository cache only (no fetch, no credentials); when it cannot tell, the record is kept.
        """
        failed = read_json(self.config_dir / FAILED_REVISION)
        if not isinstance(failed, dict) or not failed.get("revision"):
            return None
        revision, installed = str(failed["revision"]), settings["revision"]
        if revision == installed:
            return None
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision):
            try:
                git = git or GitSource(self.root, {**self.source(), "auth": "none"})
                if git.git_dir.is_dir() and git._ancestor(revision, installed):
                    return None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
        return failed

    def units_digest(self, settings: dict[str, Any]) -> str:
        """What identifies the systemd units this release installs on this server (see :meth:`failed_convergence`)."""
        texts = self.system.unit_texts(settings, self.services(settings), self.features(settings["revision"]))
        digest = hashlib.sha256()
        for path, text in sorted((str(path), text) for path, text in texts.items()):
            digest.update(path.encode() + b"\0" + text.encode() + b"\0")
        return digest.hexdigest()

    def failed_convergence(self, settings: dict[str, Any]) -> dict[str, Any] | None:
        """The record of a convergence that failed with exactly the units it would install now, or None."""
        failed = read_json(self.config_dir / FAILED_CONVERGENCE)
        if (not isinstance(failed, dict) or failed.get("revision") != settings["revision"]
                or failed.get("units") != self.units_digest(settings)):
            return None
        return failed

    def converge(self, settings: dict[str, Any]) -> dict[str, Any] | None:
        """Bring a 1.6 release installed by the 1.4 updater (or an older controller) to its own host setup.

        Rewrites outdated units (hosting: installs the runtime agent and drops
        the portal's Docker access), creates the agent's routes directory and
        re-renders the managed Caddyfile so wikis are routed straight to their
        containers, then restarts with readiness checks (every wiki that
        served before healthy and routed). On failure the units, ``app.env``
        and the Caddyfile are put back, and units that could not start are
        recorded (``config/failed-convergence.json``, with an event):
        automatic updates do not install them again (each attempt stops the
        services and waits out the readiness timeout) until an operator runs
        ``update`` or ``restart``, or a new revision is installed. Returns
        what changed, or None.
        """
        features = self.features(settings["revision"])
        if not features.hardened:
            return None
        services = self.services(settings)
        units_outdated = self.system.units_outdated(settings, services, features)
        proxy_text, warning = self.proxy_plan(settings)
        routes_missing = (settings["mode"] == "hosting" and features.runtime_agent
                          and not self.system.routes_directory(settings).is_dir())
        if not units_outdated and proxy_text is None and not routes_missing:
            return {"proxy_warning": warning} if warning else None
        names = [service.name for service in services]
        undo: dict[str, Any] = {}
        if not self.system.active(settings["service"]):
            if units_outdated:
                self.write_runtime(settings)
                self.install_units(settings)
            details = self.refresh_proxy(settings, undo)
            (self.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
            (self.config_dir / FAILED_CONVERGENCE).unlink(missing_ok=True)  # tried again at the next start
            return {"units": units_outdated, **details}
        # Before anything stops. Only restarted services need the storage check: the maintenance service, once
        # started again, restarts every running wiki whose storage quota is not verified.
        if units_outdated:
            self.preflight(settings)
        else:
            self.system.check_docker(settings)
        saved = self.system.save_units(settings, names)
        # Put back as it was: never grant the (internet-facing) portal the Docker socket because a convergence
        # failed (the restored units of a release with the runtime agent do not need it).
        docker_member = settings["mode"] == "hosting" and self.system.docker_member(settings["service"])
        environment = (self.config_dir / "app.env").read_bytes()
        digest = self.units_digest(settings) if units_outdated else None
        try:
            if units_outdated:
                self.system.stop(names)
            # Listed once the maintenance service is stopped: until then it can remove wikis.
            serving = self.system.serving_tenants(self.system.containers(settings))
            if units_outdated:
                self.write_runtime(settings)
                self.install_units(settings)
            details = self.refresh_proxy(settings, undo)
            self.system.start(names)
            if not self.system.readiness(settings, services, serving).ready:
                raise RuntimeError("The service failed its readiness checks with the converged configuration.")
        except BaseException as error:
            self.restore_proxy(undo)
            if units_outdated:
                self.system.stop(names)
                atomic_write(self.config_dir / "app.env", environment)
                self.system.restore_units(saved)
                self.system.docker_access(settings, docker_member)
                self.system.start([name for name in names if (self.system.unit_dir / (name + ".service")).exists()])
                if isinstance(error, Exception):  # not an operator's interruption
                    reason = (str(error) or type(error).__name__)[:2000]
                    write_json(self.config_dir / FAILED_CONVERGENCE, {
                        "revision": settings["revision"], "units": digest, "reason": reason, "time": _now()})
                    self.event("converge", "failed", revision=settings["revision"], reason=reason,
                               units_restored=True)
            raise
        (self.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
        (self.config_dir / FAILED_CONVERGENCE).unlink(missing_ok=True)
        self.event("converge", "complete", revision=settings["revision"])
        return {"units": units_outdated, **details}

    # Restore and recovery

    def restored_settings(self, extracted: Path, manifest: dict[str, Any], *, name: str | None = None,
                          domain: str | None = None, port: int | None = None) -> dict[str, Any]:
        saved = read_json(extracted / "config/installation.json")
        if (not saved or saved.get("product") != PRODUCT or saved.get("mode") not in profile.MODES
                or saved.get("revision") != manifest.get("revision")):
            raise ValueError("The package's installation metadata is inconsistent.")
        saved.update(root=str(self.root), installed=True)
        if name:
            saved["service"] = name
        profile.validate_service_name(saved.get("service", ""))
        valid_revision(saved["revision"])
        if domain is not None:
            saved["domain"] = profile.validate_domain(domain)
            if saved["mode"] == "hosting":
                saved["portal_domain"] = "portal." + saved["domain"] if saved["domain"] else ""
        if port is not None:
            saved["port"] = profile.validate_port(port)
        return saved

    def apply_tree(self, tree: Path, old_root: str, settings: dict[str, Any], *,
                   copier: Callable[[str, Path], None] | None = None, copy_source: bool = True,
                   preserve_policy: bool = True, public_changed: bool = False) -> None:
        """Replace ``data/`` and ``site/`` from an extracted package or a snapshot, then fix the configuration.

        An extracted package's trees are moved into place, not copied: it is
        staged on the same volume and discarded afterwards.
        """
        for name in ("data", "site"):
            temporary = self.root / f".restore-{name}-{secrets.token_hex(4)}"
            try:
                if copier is not None:
                    copier(name, temporary)
                elif (tree / name).exists():
                    try:
                        os.replace(tree / name, temporary)
                    except OSError as error:
                        if error.errno != errno.EXDEV:
                            raise
                        shutil.copytree(tree / name, temporary)
                else:
                    temporary.mkdir(mode=0o700)
            except BaseException:
                # A full disk must not leave a partial copy behind for every later attempt.
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            previous = self.root / f".previous-{name}-{secrets.token_hex(4)}"
            if (self.root / name).exists():
                os.replace(self.root / name, previous)
            os.replace(temporary, self.root / name)
            if previous.exists():
                shutil.rmtree(previous)
        atomic_write(self.root / "data" / MAINTENANCE_MARKER, "Restoring\n")
        self.tenant_maintenance(settings, True)
        if copy_source:
            for filename in ("source.json", "repo.token", "repo.key", "repo.known_hosts", "repo.allowed_signers"):
                file = tree / "config" / filename
                if file.exists():
                    atomic_write(self.config_dir / filename, file.read_bytes())
                else:
                    (self.config_dir / filename).unlink(missing_ok=True)
        values = read_environment(tree / "config/app.env")
        values = {key: str(self.root) + value[len(old_root):] if value == old_root or value.startswith(old_root + "/")
                  else value for key, value in values.items()}
        if settings["mode"] == "hosting":
            values.update(BASE_DOMAIN=settings["domain"], PORTAL_DOMAIN=settings["portal_domain"])
            prefix = "HOSTING"
        else:
            values["BW_SYSTEMD_SERVICE"] = settings["service"] + ".service"
            prefix = "BW"
        values[prefix + "_PORT"] = str(settings["port"])
        if public_changed:
            public = bool(settings.get("domain"))
            values[prefix + "_PROXY_MODE"] = "1" if public else "0"
            if prefix == "BW":
                values["BW_PREFERRED_URL_SCHEME"] = "https" if public else "http"
            else:
                values["HOSTING_MODE"] = "subdomain" if public else "port"
                values["HOSTING_PUBLIC_SCHEME"] = "https" if public else "http"
        self.write_runtime(settings, values)
        if not preserve_policy:
            saved_policy = read_json(tree / "config/updates.json", DEFAULT_POLICY.copy())
            write_json(self.config_dir / "updates.json", {**DEFAULT_POLICY, **saved_policy, "enabled": False})
        self.save(settings)

    def recover(self) -> bool:
        """Finish an interrupted transaction (written by 1.4 or 1.6): put the recorded state back, once.

        When data, release and units are back the journal says so (phase
        ``restored``): a later recovery only starts them again and never undoes
        what the operator changed in the meantime. Wikis that do not serve
        again are reported, not waited for indefinitely. If the application
        itself does not start, whatever the error, the transaction still ends
        (nothing is left to put back) and maintenance mode stays on, as after
        a failed ``start``.
        """
        journal = read_json(self.config_dir / "transaction.json")
        if not journal:
            return False
        settings = journal["settings"]
        names = list(dict.fromkeys([*self.names(settings), *journal.get("services", [])]))
        candidate = journal.get("candidate")
        if candidate and self.release(candidate).is_dir():
            names += [name for name in self.names({**settings, "revision": candidate}) if name not in names]
        self.system.stop(names)
        restored = journal.get("phase") == "restored"
        from_snapshot = not restored and journal.get("phase") == "snapshotted" and bool(journal.get("snapshot"))
        from_package = not restored and not from_snapshot and bool(journal.get("backup"))
        try:
            current = self.system.containers(settings)
            self.system.stop_containers(current)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            if restored or from_snapshot or from_package or journal.get("containers"):
                raise
            # Interrupted before it listed (so before it stopped) any wiki, and nothing to put back: Docker is
            # not needed to bring the platform back, and the portal starts the wikis again once Docker answers.
            log.warning("Docker could not be reached; the tenant containers were left as they are: %s", error)
            current = []
        # Removed whether or not anything is put back, never started again as they are (see finish()): the
        # portal starts the wikis running in the (restored) database again.
        self.system.remove_containers(current)
        if from_snapshot:
            snapshot = Snapshot.open(journal["snapshot"], self.root)
            owner = self.system.identity(settings["service"])

            def copier(name: str, destination: Path) -> None:
                snapshot.copy_tree(name, destination, owner if name == "data" else None)

            self.apply_tree(snapshot.path, str(self.root), settings, copier=copier,
                            copy_source=journal.get("restore_source", False))
            restored = True
        elif from_package:
            # Written by the 1.4 updater (at most 100,000 files): put back whatever the setting says now.
            try:
                max_files = max(self.package_limit(), DEFAULT_MAX_FILES)
            except InvalidPackageLimit as error:
                log.warning("%s The default applies to this recovery.", error)
                max_files = DEFAULT_MAX_FILES
            with read_package(Path(journal["backup"]), PRODUCT, self.root / "staging",
                              max_files=max_files) as (extracted, manifest):
                self.apply_tree(extracted, manifest["old_root"], settings,
                                copy_source=journal.get("restore_source", False))
            restored = True
        if restored and journal.get("phase") != "restored":
            self.switch(settings["revision"])
            self.install_units(settings)
            journal["phase"] = "restored"
            write_json(self.config_dir / "transaction.json", journal)
        if restored and journal.get("snapshot"):
            # Nothing is put back from it any more, and nothing may start while it shares the restored files'
            # inodes (see Snapshot.copy_tree). Not opened: one whose removal was interrupted has lost its index.
            path = Path(journal["snapshot"])
            if path.parent == self.root / "staging" and path.name.startswith("snapshot-"):
                Snapshot(path, self.root).remove()
        self.restore_proxy(journal.get("proxy"))
        try:
            unready, others = self.finish(journal, settings)
        except Exception as error:  # noqa: BLE001 - a journal kept for it would fail every later command
            (self.config_dir / "transaction.json").unlink(missing_ok=True)
            self.event("recover", "failed", restored_revision=settings["revision"], data_restored=restored,
                       reason=str(error))
            raise RuntimeError(f"The previous state was put back, but it did not start: {error} Maintenance "
                               "mode remains active; inspect the service journal and "
                               "config/last-readiness-failure.json, then run start.") from error
        unready = sorted({*unready, *self.check_tenants(settings, others)})
        self.event("recover", "complete", restored_revision=settings["revision"], data_restored=restored,
                   **({"unready_tenants": unready} if unready else {}))
        return True

    def abandon(self) -> dict[str, Any]:
        """Drop an interrupted transaction without putting its recorded state back (``recover --abandon``).

        Only for a transaction :meth:`recover` cannot finish, for example
        because its snapshot or package is gone or unreadable, or the journal
        itself is. Release, units and data stay as they are and maintenance
        mode stays on. The snapshot is kept until the next backup, update or
        restore deletes it; check the installation, then run ``start`` or
        restore a package.
        """
        path = self.config_dir / "transaction.json"
        try:
            journal = read_json(path)
        except ValueError:  # unreadable: exactly when recover cannot run
            journal = {}
        if journal is None:
            return {"outcome": "nothing to abandon"}
        if not isinstance(journal, dict) or not journal:
            journal = {"phase": "unreadable"}
        path.unlink(missing_ok=True)
        return self.event("recover", "abandoned", phase=journal.get("phase"), candidate=journal.get("candidate"),
                          snapshot=journal.get("snapshot"))

    def restore_source(self, revision: str, extracted: Path, manifest: dict[str, Any], *,
                       trust_source: str | None = None) -> Path | None:
        """Where a restore takes the code it deploys from; never a package's bundled source unchecked.

        That source runs as the service and, through the installed command, as
        root. Returns the archive to stage, or None for the release this server
        already prepared or the revision taken from this server's own update
        source. When that source requires signatures, the revision must carry
        one updates accept: its own, or a signed release tag naming it
        (:meth:`GitSource.verify_restorable`). Only the operator's explicit
        *trust_source* deploys the bundled source, and never on a server
        whose update source requires signatures: that takes turning them off
        first, which the refusal explains.
        """
        if (self.release(revision) / ".release.json").exists():
            return None
        source = read_json(self.config_dir / "source.json")
        signed = bool(source) and source.get("signing", "none") == "ssh"
        unsigned_steps = ("To deploy it anyway, turn signature checks off with 'source set --clear-signatures' "
                          "(add --allow-unsigned while automatic updates are on), run the restore again with "
                          f"--trust-source {revision}, then turn them back on with 'source set "
                          "--require-signatures ALLOWED_SIGNERS': a restore keeps this server's update source "
                          "settings. Nothing was changed.")
        if source:
            git = GitSource(self.root, source)
            if self._source_has(git, source, revision):
                try:
                    git.verify_restorable(revision)
                except (RuntimeError, ValueError) as error:
                    raise ValueError(f"{error} This server's update source requires signed revisions, so it "
                                     f"was not deployed. {unsigned_steps}") from None
                return None
            if signed:
                raise ValueError(f"Revision {revision} is not available from this server's update source, which "
                                 "requires signed revisions, so the source bundled in the package was not "
                                 f"deployed. {unsigned_steps}")
        if trust_source == revision:
            return extracted / "source.tar.gz"
        recorded = read_json(extracted / "config/source.json") or {}
        raise ValueError(f"This package deploys source revision {revision} (recorded source: "
                         f"{recorded.get('url') or 'unknown'}, branch {recorded.get('branch') or 'unknown'}; package "
                         f"created {manifest.get('created_at') or 'at an unknown time'}). It could not be checked "
                         "against this server's update source, and the restored code runs on this server with root "
                         "privileges for maintenance. If you made this package or trust whoever did, run the restore "
                         f"again with --trust-source {revision}. Nothing was changed.")

    @staticmethod
    def _source_has(git: GitSource, source: dict[str, Any], revision: str) -> bool:
        """Whether *revision* is a commit this server has fetched from (or can fetch from) its update source.

        Its branches are searched, then its release tags: a release may be
        reachable from a tag only (a maintenance branch deleted since).
        """
        def present() -> bool:
            return git.git("cat-file", "-e", revision + "^{commit}", check=False).returncode == 0

        try:
            git.initialize()
            if present():
                return True
            url = valid_url(source["url"], allow_local=Path(source["url"]).is_absolute())
            for branch in dict.fromkeys(filter(None, (source.get("branch"), source.get("fallback_branch")))):
                git.git("fetch", "--quiet", "--no-tags", url, "refs/heads/" + valid_branch(branch), check=False)
                if present():
                    return True
            for name, commit in git.release_tags(url):
                if commit == revision:
                    git.git("fetch", "--quiet", "--no-tags", url, "refs/tags/" + name, check=False)
                    return present()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        return False

    def restore(self, package: Path, *, new: bool = False, name: str | None = None, domain: str | None = None,
                port: int | None = None, trust_source: str | None = None,
                allow_unpinned: bool = False) -> dict[str, Any]:
        self.layout()
        with maintenance_lock(self.root):
            if not new:
                self._begin()
            existing = read_json(self.config_dir / "installation.json")
            if new and existing:
                raise ValueError("Use restore on an existing installation, or choose an empty --root.")
            with read_package(Path(package), PRODUCT, self.root / "staging",
                              max_files=self.package_limit()) as (extracted, manifest):
                restored = self.restored_settings(extracted, manifest, name=name, domain=domain, port=port)
                if existing and restored["mode"] != existing["mode"]:
                    raise ValueError("A restore cannot change deployment mode. Use a separate installation root.")
                if existing:
                    restored["service"] = existing["service"]
                # Before anything changes: which code may run here at all.
                archive = self.restore_source(restored["revision"], extracted, manifest, trust_source=trust_source)
                # That was decided with this server's update source and signature trust: they stay.
                # Only a server without any (a new one) takes the package's, credentials included.
                details: dict[str, Any] = {}
                keep_source = (self.config_dir / "source.json").is_file()
                if keep_source:
                    details.update(self._kept_source(extracted))
                self.system.account(restored)
                self.stage(restored, archive=archive, allow_unpinned=allow_unpinned)
                if not existing:
                    self.system.preflight(restored, self.services(restored))

                def apply(journal: dict[str, Any] | None) -> list[str] | None:
                    if journal:
                        # A restore that replaces the source settings must roll them back too.
                        journal["restore_source"] = not keep_source
                        write_json(self.config_dir / "transaction.json", journal)
                        self.system.remove_containers(journal["containers"])
                    self.apply_tree(extracted, manifest["old_root"], restored, preserve_policy=False,
                                    public_changed=domain is not None, copy_source=not keep_source)
                    self.switch(restored["revision"])
                    self.install_units(restored)
                    self.system.install_timer(restored, self.policy())
                    self._disable_remote_schedule(restored)
                    if journal:
                        return self.finish(journal, restored, required=True)[1]  # every waited wiki serves
                    services = self.services(restored)
                    self.system.start([service.name for service in services])
                    if not self.system.readiness(restored, services).ready:
                        raise RuntimeError("The restored service failed its health checks. Maintenance mode remains active.")
                    self.tenant_maintenance(restored, False)
                    (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)
                    return None  # no wiki served here before: every one running in the database is checked

                if existing:
                    others, snapshot = self.guarded(self.settings(), apply, target=restored["revision"])
                    try:
                        details["previous_package"] = str(
                            self.package_from(snapshot, existing, self.backup_name("before-restore")))
                    except (OSError, ValueError) as error:
                        details["backup_warning"] = f"The pre-restore package could not be written: {error}"
                    finally:
                        snapshot.remove()
                else:
                    others = apply(None)
                unready = self.check_tenants(restored, others)
                if unready:
                    details["unready_tenants"] = unready
            return self.event("restore", "complete", revision=restored["revision"], automatic_updates=False, **details)

    def _kept_source(self, extracted: Path) -> dict[str, str]:
        """``source_warning`` when the package's update source settings differ from the ones this server keeps."""
        keys = ("url", "track", "branch", "fallback_branch", "signing")
        package = read_json(extracted / "config/source.json") or {}
        current = read_json(self.config_dir / "source.json") or {}
        if not package or all(package.get(key) == current.get(key) for key in keys):
            return {}
        return {"source_warning": (
            f"The package's update source ({package.get('url') or 'unknown'}, signatures "
            f"{package.get('signing') or 'none'}) differs from this server's ({current.get('url')}, signatures "
            f"{current.get('signing') or 'none'}), which was kept with its credentials and allowed signers. "
            "Use 'source set' to change it.")}

    def _disable_remote_schedule(self, settings: dict[str, Any]) -> None:
        from .backups.store import Store

        remote = Store(self.config_dir / "remote-backup", PRODUCT)
        if remote.status()["configured"]:
            self.system.install_backup_timer(settings, remote.set_schedule(False))

    # Policy, pruning, status, removal

    def require_signed_updates(self, signing: str, *, allow_unsigned: bool = False) -> None:
        """Refuse automatic updates of revisions nobody signed, unless the operator explicitly accepts them.

        Automatic updates run what they fetch as root, unattended: anyone who
        can push to the source (a stolen maintainer account or token) would
        otherwise own every server that enabled them.
        """
        if signing != "ssh" and not allow_unsigned:
            raise ValueError("Automatic updates install new code as root without anyone reviewing it, so they "
                             "require signed releases: add --require-signatures ALLOWED_SIGNERS (the maintainers' "
                             "SSH keys, see docs/operations.md). To accept unsigned revisions anyway, add "
                             "--allow-unsigned.")

    def set_updates(self, enabled: bool, *, interval: int | None = None, keep: int | None = None,
                    allow_unsigned: bool = False) -> dict[str, Any]:
        settings = self.settings()
        policy = self.policy()
        if enabled:
            signing = self.source().get("signing", "none")
            self.require_signed_updates(signing, allow_unsigned=allow_unsigned)
            policy["unsigned"] = signing != "ssh"
        policy["enabled"] = bool(enabled)
        if interval is not None:
            if not 5 <= interval <= 10080:
                raise ValueError("Choose a check interval between 5 minutes and one week.")
            policy["interval_minutes"] = interval
        if keep is not None:
            if not 2 <= keep <= 30:
                raise ValueError("Keep between 2 and 30 automatic backups.")
            policy["keep_backups"] = keep
        # A separate file, so a running updater cannot overwrite a later opt-out.
        write_json(self.config_dir / "updates.json", policy)
        self.system.install_timer(settings, policy)
        source = self.source()
        return self.event("updates", "enabled" if enabled else "disabled", source=source["url"],
                          track=source["track"], branch=source["branch"], fallback_branch=source.get("fallback_branch"),
                          signing=source.get("signing", "none"),
                          interval_minutes=policy["interval_minutes"], keep_backups=policy["keep_backups"])

    def prune_backups(self) -> list[str]:
        """Keep the newest ``keep_backups`` of each kind of package made automatically, and the rollback target."""
        keep = self.policy()["keep_backups"]
        protected = (read_json(self.config_dir / "last-update.json", {}) or {}).get("backup")
        removed = []
        for prefix in PRUNED_PREFIXES:
            files = sorted((self.root / "backups").glob(prefix + "-*.tar.gz"), key=lambda path: path.name, reverse=True)
            for path in files[keep:]:
                if str(path) != protected and path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed.append(path.name)
        return removed

    def prune_releases(self, preserve: set[str]) -> set[str]:
        releases = sorted((path for path in (self.root / "releases").iterdir()
                           if path.is_dir() and not path.is_symlink() and re.fullmatch(r"[a-f0-9]{40,64}", path.name)),
                          key=lambda path: path.stat().st_mtime, reverse=True)
        kept = set(preserve) | {path.name for path in releases[: self.policy()["keep_backups"]]}
        for path in releases:
            if path.name not in kept:
                shutil.rmtree(path)
        return kept

    def status(self) -> dict[str, Any]:
        from .backups.store import Store

        settings = self.settings()
        features = self.features(settings["revision"])
        image = (self.system.image_created(f"{TENANT_IMAGE}:{settings['revision']}")
                 if settings["mode"] == "hosting" else None)
        source, policy = self.source(), self.policy()
        output = {
            "product": PRODUCT, "mode": settings["mode"], "revision": settings["revision"], "root": str(self.root),
            **({"tenant_image_built": image} if image else {}),
            "service": settings["service"], "installed": settings.get("installed", False), "source": source,
            "updates": policy, "maintenance": (self.root / "data" / MAINTENANCE_MARKER).exists(),
            "recovery_pending": bool(read_json(self.config_dir / "transaction.json")),
            "units_current": not self.system.units_outdated(settings, self.services(settings), features),
            "runtime_agent": features.runtime_agent and settings["mode"] == "hosting",
            "last_operation": read_json(self.config_dir / "status.json"),
            "last_update": read_json(self.config_dir / "last-update.json"),
            "environment_file": str(self.config_dir / "app.env"),
            "backups": sorted(path.name for path in (self.root / "backups").glob("*.tar.gz")),
            "remote_backups": Store(self.config_dir / "remote-backup", PRODUCT).status(),
            **self.package_status(settings),
        }
        if policy["enabled"] and source.get("signing", "none") != "ssh":
            output["warnings"] = ["Automatic updates install unsigned revisions as root: whoever can push to the "
                                  "update source can run code on this server. Run 'updates enable "
                                  "--require-signatures ALLOWED_SIGNERS'.", *output.get("warnings", [])]
        warnings = output.setdefault("warnings", [])
        failed = self.failed_revision(settings)
        if failed:
            output["failed_revision"] = failed
            warnings.append(f"Automatic updates skip revision {str(failed['revision'])[:12]}, which failed"
                            + (f" ({failed['stage']})" if failed.get("stage") else "")
                            + (f": {str(failed['reason'])[:500]}" if failed.get("reason") else "")
                            + ". Once the cause is fixed, run 'update' (or 'update --retry-failed') to try it again;"
                              " a newer revision is tried automatically.")
        unconverged = self.failed_convergence(settings) if not output["units_current"] else None
        if unconverged:
            output["failed_convergence"] = unconverged
            warnings.append(f"Installing this release's systemd units failed ({str(unconverged.get('time'))[:19]}: "
                            f"{str(unconverged.get('reason'))[:500]}); the previous units were put back and "
                            "automatic updates do not try again. Look at the services' journal "
                            f"(journalctl -u '{settings['service']}*'), fix the cause, then run 'update' (or 'restart').")
        if settings["mode"] == "hosting" and features.project_quotas:
            try:
                storage = self.agent_storage(settings)
                output["tenant_storage"] = storage.to_dict()
                warning, loud = storage.warning(), storage.severity in ("warning", "error")
            except InvalidTenantQuotas as error:
                output["tenant_storage"] = {"setting": None, "hard_quotas": False, "refused": True, "lost": False,
                                            "reason": str(error)}
                warning, loud = str(error), True
            # Refused or lost: a warning. Storage that never had hard quotas (or off): a note, except under units
            # an older controller wrote, where the warning below says why (not the storage, perhaps).
            if warning and loud:
                warnings.append(warning)
            elif warning and self.system.agent_unit_quotas(settings):
                output["notes"] = [warning]
            if not output["units_current"]:
                warnings.append("The services run under systemd units an older controller wrote (an update by an "
                                "earlier release's updater, for example), under which the runtime agent cannot use "
                                "hard storage quotas: run 'update' (or 'restart') to install this release's units.")
            plugins = read_environment(self.config_dir / "app.env").get("HOSTING_ALLOW_TENANT_PLUGINS", "")
            if plugins.strip().lower() in TRUE_VALUES and not output["tenant_storage"]["hard_quotas"]:
                warnings.append("HOSTING_ALLOW_TENANT_PLUGINS=1 lets wiki owners run code of their own with write "
                                "access to their wiki's files. Without hard storage quotas nothing stops such code "
                                "from filling the disk, at disk speed, before the portal's next storage check. "
                                "Enforce hard quotas, or set HOSTING_ALLOW_TENANT_PLUGINS=0.")
        if not warnings:
            del output["warnings"]
        return output

    def agent_storage(self, settings: dict[str, Any]) -> tenant_quotas.Policy:
        """What the runtime agent decides about hard quotas now (``status``, ``config get tenant-quotas``).

        As :meth:`.System.tenant_storage` sees the storage, except that an
        agent still under the units an older controller wrote cannot use
        quotas, whatever the storage supports.
        """
        features = self.features(settings["revision"])
        problem = None if self.system.agent_unit_quotas(settings) else (
            "the runtime agent runs under systemd units an older controller wrote, which do not let it use quotas "
            "(run 'update' or 'restart' to install this release's units)")
        return self.system.tenant_storage(settings, configured=None if features.quota_modes else "required",
                                          agent_problem=problem)

    def package_status(self, settings: dict[str, Any]) -> dict[str, Any]:
        """How many files a package would hold now, the limit, and a warning when backups and updates near it."""
        try:
            limit = self.package_limit()
            files = len(snapshot_sources(self.root, self.allowed_link(settings))) + 1  # and the release's source
        except (OSError, ValueError) as error:
            return {"package_files": None, "package_file_limit": None,
                    "warnings": [f"Backups and updates would fail: {error}"]}
        output: dict[str, Any] = {"package_files": files, "package_file_limit": limit}
        if files > limit * 0.8:
            output["warnings"] = [
                f"The installation holds {files:,} files and a package may hold {limit:,}: beyond that every "
                "backup and update is refused. Raise BANANA_PACKAGE_MAX_FILES in config/app.env, or remove files."]
        return output

    def uninstall(self, *, purge: bool = False, confirm: str = "") -> dict[str, Any]:
        from .backups.files import lock as backup_lock
        from .backups.store import Store

        remote = Store(self.config_dir / "remote-backup", PRODUCT)
        with maintenance_lock(self.root):
            configured = remote.status()["configured"]
            with backup_lock(remote.root) if configured else nullcontext():
                self._begin()
                settings = self.settings()
                if purge and confirm != settings["service"]:
                    raise ValueError("For permanent deletion, pass --purge --confirm SERVICE_NAME.")
                self.set_updates(False)
                if configured:
                    self.system.install_backup_timer(settings, remote.set_schedule(False))
                names = self.names(settings)
                self.system.stop(names)
                containers = self.system.containers(settings)
                self.system.stop_containers(containers)
                self.system.remove_containers(containers)
                self.system.uninstall(settings, names)
                settings["installed"] = False
                self.save(settings)
                self.event("uninstall", "complete", data_preserved=not purge)
        if purge:
            shutil.rmtree(self.root)
        return {"outcome": "uninstalled", "data_preserved": not purge}
