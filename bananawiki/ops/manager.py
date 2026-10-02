"""Install, update, back up, restore, recover and remove one managed deployment.

Safety properties (all inherited from 1.4 and kept):

* every operation holds ``config/maintenance.lock`` (``flock``) and first
  finishes any interrupted transaction (``config/transaction.json``);
* releases are staged next to the running one, sealed read-only, and switched
  by an atomic symlink replacement;
* before anything changes, the data is captured; if the new release fails its
  readiness checks the data, the release and the units are put back;
* update sources are fetched with a hardened Git, fast-forward only unless the
  operator explicitly allows otherwise, optionally signature-verified.

New in 1.6: data is captured by an online :class:`~.snapshot.Snapshot` (short
downtime) and the portable package is written after the service is back, then
verified as a restore would before it is used;
``before-update-*`` packages, releases and tenant images are pruned; hosting
installs get the runtime agent instead of Docker group membership; units
installed by the 1.4 updater are converged to the hardened 1.6 units.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import tarfile
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import DEFAULT_SOURCE_URL, PRODUCT, caddy, profile
from .files import (
    MAINTENANCE_MARKER,
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
from .profile import ReleaseFeatures, Service
from .snapshot import Snapshot, stale_snapshots
from .source import GitSource, credential_host, valid_branch, valid_revision, valid_url
from .system import System

DEFAULT_POLICY = {"enabled": False, "interval_minutes": 60, "keep_backups": 3}
PRUNED_PREFIXES = ("auto", "before-update")
PROXY_ROLLBACK = "Caddyfile.rollback"

log = logging.getLogger("bananawiki.ops")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Manager:
    def __init__(self, root: str | os.PathLike[str], *, system: System | None = None):
        self.root = absolute_path(root)
        self.config_dir = self.root / "config"
        self.system = system or System(log_dir=self.config_dir)
        self.product = PRODUCT

    # Configuration --------------------------------------------------------

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

    def source(self) -> dict[str, Any]:
        data = read_json(self.config_dir / "source.json")
        if not data:
            raise ValueError("No update source is configured.")
        valid_branch(data["branch"])
        if data.get("fallback_branch"):
            valid_branch(data["fallback_branch"])
        return data

    def event(self, operation: str, outcome: str, **details: Any) -> dict[str, Any]:
        value = {"time": _now(), "operation": operation, "outcome": outcome, **details}
        write_json(self.config_dir / "status.json", value)
        append_jsonl(self.config_dir / "history.jsonl", value)
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
                         signers_file: Path | None = None, clear_signatures: bool = False) -> dict[str, Any]:
        old = read_json(self.config_dir / "source.json", {
            "url": DEFAULT_SOURCE_URL, "branch": "main", "fallback_branch": None, "auth": "none", "signing": "none",
        })
        new = dict(old)
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

    # Releases -------------------------------------------------------------

    def release(self, revision: str) -> Path:
        return self.root / "releases" / valid_revision(revision)

    def features(self, revision: str) -> ReleaseFeatures:
        return ReleaseFeatures.of(self.release(revision))

    def services(self, settings: dict[str, Any]) -> list[Service]:
        return profile.services(settings, self.features(settings["revision"]))

    def names(self, settings: dict[str, Any]) -> list[str]:
        return [service.name for service in self.services(settings)]

    def stage(self, settings: dict[str, Any], *, archive: Path | None = None) -> Path:
        revision = settings["revision"]
        release = self.release(revision)
        if (release / ".release.json").exists():
            return release
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
            self.system.prepare_release(settings, release, ReleaseFeatures.of(release))
            shutil.copyfile(archive_file, release / ".source.tar.gz")
            write_json(release / ".release.json", {"revision": revision, "product": PRODUCT})
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

    # Reverse proxy ----------------------------------------------------------

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

    # Install --------------------------------------------------------------

    def install(self, checkout: Path, *, mode: str, name: str | None = None, domain: str = "",
                portal_domain: str = "", port: int | None = None, source_options: dict[str, Any] | None = None,
                reuse_data: bool = False) -> dict[str, Any]:
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
            source = self.configure_source(**(source_options or {}))
            settings = profile.new_settings(self.root, mode=mode, service=name, domain=domain,
                                            portal_domain=portal_domain, port=port, source_url=source["url"])
            self.system.account(settings)
            settings["revision"] = GitSource(self.root, source).seed(checkout)
            self.system.preflight(settings, profile.services(settings, ReleaseFeatures(True, True)))
            self.stage(settings)
            self.save(settings)
            self.switch(settings["revision"])
            self.write_runtime(settings)
            write_json(self.config_dir / "updates.json", DEFAULT_POLICY.copy())
            self.install_units(settings)
            self.system.install_timer(settings, self.policy())
            atomic_write(self.root / "data" / MAINTENANCE_MARKER, "Installing\n")
            services = self.services(settings)
            self.system.start([service.name for service in services])
            if not self.system.healthy(settings, services):
                self.event("install", "failed", revision=settings["revision"],
                           reason="Initial health checks failed; maintenance mode remains active.")
                raise RuntimeError("Initial health checks failed. Inspect the service journal, correct the "
                                   "configuration, and run install --reuse-data.")
            settings["installed"] = True
            self.save(settings)
            (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)
            return self.event("install", "complete", revision=settings["revision"], automatic_updates=False)

    # Transactions ---------------------------------------------------------

    def journal_state(self, settings: dict[str, Any]) -> dict[str, Any]:
        names = self.names(settings)
        return {"settings": settings, "services": names, "active": [name for name in names if self.system.active(name)],
                "containers": self.system.containers(settings), "backup": None, "snapshot": None,
                "candidate": None, "phase": "preparing"}

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
        write_json(self.config_dir / "transaction.json", journal)
        atomic_write(self.root / "data" / MAINTENANCE_MARKER, "Maintenance in progress\n")
        self.tenant_maintenance(journal["settings"], True)
        self.system.stop(journal["services"])
        self.system.stop_containers(journal["containers"])

    def finish(self, journal: dict[str, Any], settings: dict[str, Any], *, old_containers: bool = False) -> None:
        if old_containers:
            self.system.resume_containers(journal["containers"])
        services = self.services(settings)
        names = [service.name for service in services]
        self.system.start(names)
        if not self.system.healthy(settings, services, journal["containers"]):
            raise RuntimeError("The application or a previously running wiki failed its readiness checks.")
        known = journal.get("services") or names
        self.system.stop([name for name in names if name in known and name not in journal["active"]])
        self.tenant_maintenance(settings, False)
        (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)
        (self.config_dir / "transaction.json").unlink(missing_ok=True)

    def allowed_link(self, settings: dict[str, Any]) -> Callable[[Path], bool] | None:
        if settings["mode"] != "hosting":
            return None
        data = self.root / "data"
        return lambda path: profile.inside_tenant(path, data)

    def guarded(self, settings: dict[str, Any], body: Callable[[dict[str, Any]], Any], *,
                candidate: str | None = None) -> tuple[Any, Snapshot]:
        """Snapshot (live), quiesce, refresh, run *body*; on any failure put everything back.

        Returns ``(body result, snapshot)``; the caller writes the package from
        the snapshot once the service is back and then removes it.
        """
        snapshot = Snapshot.create(self.root, self.allowed_link(settings))
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
        verification does not need space for both.
        """
        source_archive = self.release(settings["revision"]) / ".source.tar.gz"
        inputs = [*snapshot.inputs(), (source_archive, "source.tar.gz")]
        try:
            package = write_package(destination, {
                "product": PRODUCT, "mode": settings["mode"], "revision": settings["revision"],
                "created_at": _now(), "old_root": str(self.root), "model_weights_excluded": False,
                "excluded_paths": [],
            }, inputs)
        finally:
            snapshot.remove()
        self.verify_package(package)
        return package

    def verify_package(self, package: Path) -> None:
        """Refuse (and delete) a package that a restore would refuse."""
        try:
            with read_package(package, PRODUCT, self.root / "staging"):
                pass
        except Exception as error:
            package.unlink(missing_ok=True)
            raise ValueError(f"The new package failed verification and was removed: {error}") from error

    def backup_name(self, prefix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.root / "backups" / f"{prefix}-{stamp}-{secrets.token_hex(3)}.tar.gz"

    def _begin(self) -> None:
        """Every locked operation: finish an interrupted transaction, drop stale snapshots."""
        self.recover()
        for path in stale_snapshots(self.root, None):
            shutil.rmtree(path)

    # Backup ---------------------------------------------------------------

    def backup(self, destination: Path | None = None, *, prefix: str = "manual") -> Path:
        with maintenance_lock(self.root):
            self._begin()
            settings = self.settings()
            destination = Path(destination).absolute() if destination else self.backup_name(prefix)
            for directory in (self.root / "data", self.root / "site", self.root / "releases", self.root / "staging"):
                if destination.is_relative_to(directory):
                    raise ValueError("Write backup packages outside the data, site, staging and release directories.")
            _, snapshot = self.guarded(settings, lambda journal: self.finish(journal, settings, old_containers=True))
            try:
                result = self.package_from(snapshot, settings, destination)
            finally:
                snapshot.remove()
            self.event("backup", "complete", package=str(result))
            return result

    # Update ---------------------------------------------------------------

    def update(self, *, automatic: bool = False, allow_divergent: bool = False,
               retry_failed: bool = False) -> dict[str, Any]:
        with maintenance_lock(self.root):
            self._begin()
            settings = self.settings()
            if automatic and not self.policy()["enabled"]:
                return {"outcome": "disabled"}
            if automatic and not self.system.active(settings["service"]):
                return self.event("update", "skipped", reason="The application was intentionally stopped.")
            source = self.source()
            sha, selected, forward = GitSource(self.root, source).resolve(settings["revision"])
            if sha == settings["revision"]:
                converged = self.converge(settings) or {}
                return self.event("update", "current", revision=sha, branch=selected,
                                  units_converged=bool(converged.get("units")),
                                  **{key: value for key, value in converged.items() if key != "units"})
            if not forward and (automatic or not allow_divergent):
                return self.event("update", "paused", revision=sha,
                                  reason="The selected source diverges from the installed revision. Review it and "
                                         "use update --allow-divergent for an intentional switch.")
            failed = read_json(self.config_dir / "failed-revision.json", {})
            if automatic and failed.get("revision") == sha and not retry_failed:
                return self.event("update", "paused", revision=sha,
                                  reason="This revision previously failed; a maintainer must retry it or select a "
                                         "newer revision.")
            candidate = {**settings, "revision": sha, "source_url": source["url"]}
            try:
                self.stage(candidate)
            except BaseException:
                write_json(self.config_dir / "failed-revision.json", {"revision": sha})
                self.event("update", "failed", revision=sha,
                           reason="Release preparation failed; the running version was left in place.")
                raise
            if automatic and not self.policy()["enabled"]:
                return self.event("update", "cancelled", reason="Automatic updates were disabled while preparing.")

            def apply(journal: dict[str, Any]) -> None:
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
                self.finish(journal, candidate)

            details: dict[str, Any] = {}
            try:
                _, snapshot = self.guarded(settings, apply, candidate=sha)
            except BaseException:
                write_json(self.config_dir / "failed-revision.json", {"revision": sha})
                self.event("update", "rolled_back", revision=sha, restored_revision=settings["revision"])
                raise
            (self.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
            try:
                archive = self.package_from(snapshot, settings,
                                            self.backup_name("auto" if automatic else "before-update"))
                details["backup"] = str(archive)
                write_json(self.config_dir / "last-update.json", {
                    "backup": str(archive), "previous_revision": settings["revision"], "revision": sha})
            except (OSError, ValueError) as error:
                details["backup_warning"] = f"The rollback package could not be written: {error}"
            finally:
                snapshot.remove()
            self.prune_backups()
            kept = self.prune_releases({settings["revision"], sha})
            if settings["mode"] == "hosting":
                details["images_removed"] = self.system.prune_images(kept)
            return self.event("update", "complete", revision=sha, previous_revision=settings["revision"],
                              branch=selected, used_fallback=selected != source["branch"], **details)

    def converge(self, settings: dict[str, Any]) -> dict[str, Any] | None:
        """Bring a 1.6 release installed by the 1.4 updater (or an older controller) to its own host setup.

        Rewrites outdated units (hosting: installs the runtime agent and drops
        the portal's Docker access), creates the agent's routes directory and
        re-renders the managed Caddyfile so wikis are routed straight to their
        containers, then restarts with readiness checks (every running wiki
        healthy and routed). On failure the units, ``app.env`` and the
        Caddyfile are put back. Returns what changed, or None.
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
            return {"units": units_outdated, **details}
        saved = self.system.save_units(settings, names)
        environment = (self.config_dir / "app.env").read_bytes()
        try:
            if units_outdated:
                self.system.stop(names)
                self.write_runtime(settings)
                self.install_units(settings)
            details = self.refresh_proxy(settings, undo)
            self.system.start(names)
            if not self.system.healthy(settings, services):
                raise RuntimeError("The service failed its readiness checks with the converged configuration.")
        except BaseException:
            self.restore_proxy(undo)
            if units_outdated:
                self.system.stop(names)
                atomic_write(self.config_dir / "app.env", environment)
                self.system.restore_units(saved)
                self.system.docker_access(settings, True)
                self.system.start([name for name in names if (self.system.unit_dir / (name + ".service")).exists()])
            raise
        (self.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
        self.event("converge", "complete", revision=settings["revision"])
        return {"units": units_outdated, **details}

    # Restore and recovery -------------------------------------------------

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
        """Replace ``data/`` and ``site/`` from an extracted package or a snapshot, then fix the configuration."""
        for name in ("data", "site"):
            temporary = self.root / f".restore-{name}-{secrets.token_hex(4)}"
            if copier is not None:
                copier(name, temporary)
            elif (tree / name).exists():
                shutil.copytree(tree / name, temporary)
            else:
                temporary.mkdir(mode=0o700)
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
        """Finish an interrupted transaction (written by 1.4 or 1.6): put the recorded state back."""
        journal = read_json(self.config_dir / "transaction.json")
        if not journal:
            return False
        settings = journal["settings"]
        names = list(dict.fromkeys([*self.names(settings), *journal.get("services", [])]))
        candidate = journal.get("candidate")
        if candidate and self.release(candidate).is_dir():
            names += [name for name in self.names({**settings, "revision": candidate}) if name not in names]
        self.system.stop(names)
        current = self.system.containers(settings)
        self.system.stop_containers(current)
        restored = False
        if journal.get("phase") == "snapshotted" and journal.get("snapshot"):
            self.system.remove_containers(current)
            snapshot = Snapshot.open(journal["snapshot"], self.root)
            self.apply_tree(snapshot.path, str(self.root), settings, copier=snapshot.copy_tree,
                            copy_source=journal.get("restore_source", False))
            restored = True
        elif journal.get("backup"):
            self.system.remove_containers(current)
            with read_package(Path(journal["backup"]), PRODUCT, self.root / "staging") as (extracted, manifest):
                self.apply_tree(extracted, manifest["old_root"], settings,
                                copy_source=journal.get("restore_source", False))
            restored = True
        if restored:
            self.switch(settings["revision"])
            self.install_units(settings)
        self.restore_proxy(journal.get("proxy"))
        if restored:
            self.finish(journal, settings)
        else:
            self.finish(journal, settings, old_containers=True)
        self.event("recover", "complete", restored_revision=settings["revision"], data_restored=restored)
        return True

    def restore(self, package: Path, *, new: bool = False, name: str | None = None, domain: str | None = None,
                port: int | None = None) -> dict[str, Any]:
        self.layout()
        with maintenance_lock(self.root):
            if not new:
                self._begin()
            existing = read_json(self.config_dir / "installation.json")
            if new and existing:
                raise ValueError("Use restore on an existing installation, or choose an empty --root.")
            with read_package(Path(package), PRODUCT, self.root / "staging") as (extracted, manifest):
                restored = self.restored_settings(extracted, manifest, name=name, domain=domain, port=port)
                if existing and restored["mode"] != existing["mode"]:
                    raise ValueError("A restore cannot change deployment mode. Use a separate installation root.")
                if existing:
                    restored["service"] = existing["service"]
                self.system.account(restored)
                self.stage(restored, archive=extracted / "source.tar.gz")
                if not existing:
                    self.system.preflight(restored, self.services(restored))

                def apply(journal: dict[str, Any] | None) -> None:
                    if journal:
                        # Updates preserve the operator's source settings, but
                        # a restore replaces them and must roll them back too.
                        journal["restore_source"] = True
                        write_json(self.config_dir / "transaction.json", journal)
                        self.system.remove_containers(journal["containers"])
                    self.apply_tree(extracted, manifest["old_root"], restored, preserve_policy=False,
                                    public_changed=domain is not None)
                    self.switch(restored["revision"])
                    self.install_units(restored)
                    self.system.install_timer(restored, self.policy())
                    self._disable_remote_schedule(restored)
                    if journal:
                        self.finish(journal, restored)
                        return
                    services = self.services(restored)
                    self.system.start([service.name for service in services])
                    if not self.system.healthy(restored, services):
                        raise RuntimeError("The restored service failed its health checks. Maintenance mode remains active.")
                    self.tenant_maintenance(restored, False)
                    (self.root / "data" / MAINTENANCE_MARKER).unlink(missing_ok=True)

                details: dict[str, Any] = {}
                if existing:
                    _, snapshot = self.guarded(self.settings(), apply)
                    try:
                        details["previous_package"] = str(
                            self.package_from(snapshot, existing, self.backup_name("before-restore")))
                    except (OSError, ValueError) as error:
                        details["backup_warning"] = f"The pre-restore package could not be written: {error}"
                    finally:
                        snapshot.remove()
                else:
                    apply(None)
            return self.event("restore", "complete", revision=restored["revision"], automatic_updates=False, **details)

    def _disable_remote_schedule(self, settings: dict[str, Any]) -> None:
        from .backups.store import Store

        remote = Store(self.config_dir / "remote-backup", PRODUCT)
        if remote.status()["configured"]:
            self.system.install_backup_timer(settings, remote.set_schedule(False))

    # Policy, pruning, status, removal ---------------------------------------

    def set_updates(self, enabled: bool, *, interval: int | None = None, keep: int | None = None) -> dict[str, Any]:
        settings = self.settings()
        policy = self.policy()
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
                          branch=source["branch"], fallback_branch=source.get("fallback_branch"),
                          interval_minutes=policy["interval_minutes"], keep_backups=policy["keep_backups"])

    def prune_backups(self) -> list[str]:
        """Keep the newest ``keep_backups`` automatic and pre-update packages, and the rollback target."""
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
        return {
            "product": PRODUCT, "mode": settings["mode"], "revision": settings["revision"], "root": str(self.root),
            "service": settings["service"], "installed": settings.get("installed", False), "source": self.source(),
            "updates": self.policy(), "maintenance": (self.root / "data" / MAINTENANCE_MARKER).exists(),
            "recovery_pending": bool(read_json(self.config_dir / "transaction.json")),
            "units_current": not self.system.units_outdated(settings, self.services(settings), features),
            "runtime_agent": features.runtime_agent and settings["mode"] == "hosting",
            "last_operation": read_json(self.config_dir / "status.json"),
            "last_update": read_json(self.config_dir / "last-update.json"),
            "environment_file": str(self.config_dir / "app.env"),
            "backups": sorted(path.name for path in (self.root / "backups").glob("*.tar.gz")),
            "remote_backups": Store(self.config_dir / "remote-backup", PRODUCT).status(),
        }

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
