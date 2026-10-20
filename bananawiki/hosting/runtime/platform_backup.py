"""Whole-platform backups: the encrypted export and the restore onto a fresh installation.

The export holds a consistent copy of
``hosting.db``, the portal's secret key and every tenant directory, with
each tenant database copied inside its own container, and the quarantine
marker of every wiki whose plugins are quarantined (nothing in
``hosting.db`` records it). It is written straight into
:class:`~.crypto.EncryptedOutput`, so no plaintext archive exists on disk at
any point. One wiki that cannot be copied does not stop the backup of all
the others: ``backup_manifest.json``,
written last, lists the wikis it holds and those it could not save.

The restore accepts the
encrypted ``.bwenc`` files and plain 1.4 ``.zip`` parts, checks every member
before changing anything, and only restores onto an installation that has no
wikis yet: replacing live tenants needs them offline, which a web request
cannot guarantee.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ... import __version__
from ...core import zipcheck
from ...core.json import loads as safe_json_loads
from ...core.logformat import one_line
from ...ops.project_quota import ADOPTION_HEADROOM_BYTES
from ..config import HostingConfig
from . import FAILURE_CODES, RuntimeFailure, archives, crypto, tenantfs
from .plugin_safety import INSTANCE_ID, PluginState

log = logging.getLogger("bananawiki.hosting.runtime.platform")

BACKUP_PREFIX = "bananawiki_hosting_backup_"
PLATFORM_TABLES = frozenset({"accounts", "instances", "hosting_settings"})
DATABASE_NAMES = frozenset({"hosting.db", "hosting_db/hosting.db", "hosting/hosting.db"})
SECRET_NAMES = frozenset({"secret_key", ".secret_key", "hosting_config/.secret_key", "hosting/.secret_key"})
BACKUP_KEY_NAME = "hosting/.backup_encryption_key"
MANIFEST_NAME = "backup_manifest.json"
# Besides its database copy, a wiki's files may add its storage limit to a
# backup, plus a tenth of it and this much: the quota counts allocated
# blocks, the budget sizes as files report them.
BUDGET_SLACK_BYTES = 64 * 1024 ** 2
# What the portal keeps in a wiki's platform state while the runtime agent
# holds it above its plan: the plan and the hard limit it was adopted at.
OVER_PLAN_NAME = "over-plan.json"
# Where a backup holds the plugin quarantine marker of a wiki: platform_state/<instance id>/plugin_quarantine.
STATE_PREFIX = "platform_state"
QUARANTINE_NAME = "plugin_quarantine"

Snapshot = Callable[[str], archives.DatabaseCopy | None]


class WikiFault(RuntimeFailure):
    """What a ``snapshot`` raises when the wiki's own sandbox ran and refused the copy (a damaged database, one
    replaced with a link): the wiki's doing, never taken for a platform fault."""


def byte_budget(limit: int | None) -> int | None:
    """What a wiki's files, and its database copy apart, may add to a backup or an export: its storage *limit*
    (bytes; None: no limit, and no budget) and some slack."""
    return None if limit is None else limit + limit // 10 + BUDGET_SLACK_BYTES


def held_limit(state_dir: str, instance_id: str, limit: int | None) -> int | None:
    """The storage limit a wiki's archives are held to: its plan *limit* (bytes; None: no limit), or the larger
    hard limit the runtime agent adopted it at above that plan, as the portal recorded it in the wiki's
    platform state.

    Such a wiki serves holding more than its plan, and its quota bounds what
    it can allocate at the adopted limit, as the plan bounds the others': a
    budget from the plan alone would leave its genuine database out of every
    platform backup and refuse its owner's download.
    """
    if limit is None or not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
        return limit
    try:
        recorded = json.loads((Path(state_dir) / instance_id / OVER_PLAN_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return limit
    held = recorded.get("byte_limit") if isinstance(recorded, dict) else None
    return max(limit, held) if type(held) is int else limit


def tenant_names(instances_dir: str) -> list[str]:
    base = Path(instances_dir)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir()
                  if tenantfs.DATA_DIR_NAME.fullmatch(entry.name) and tenantfs.is_tenant_dir(entry))


def quarantined_wikis(state_dir: str) -> list[str]:
    """The instance ids of the wikis whose plugins are quarantined (:class:`~.plugin_safety.PluginState`)."""
    base = Path(state_dir)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir()
                  if INSTANCE_ID.fullmatch(entry.name) and PluginState(state_dir, entry.name).quarantined())


def _snapshot_hosting_db(cfg: HostingConfig, target: Path) -> None:
    source = sqlite3.connect(f"{Path(cfg.database_path).absolute().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        copy = sqlite3.connect(str(target))
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def _storage_limits(cfg: HostingConfig, database: Path) -> dict[str, int | None]:
    """Each wiki's storage limit in bytes by data directory, read from the copy of ``hosting.db``.

    None stands for no limit: an administrator's wiki, an apex wiki or a
    limit of 0, which only the runtime's own ceiling bounds. A wiki without
    a limit of its own has the default one, like a wiki the copy does not
    name. A wiki the runtime agent holds above its plan has the hard limit
    it was adopted at (:func:`held_limit`).
    """
    try:
        # The backup's own copy: a read-only connection could not remove the
        # WAL files it creates on closing, which would stay as plaintext.
        conn = sqlite3.connect(str(database))
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            rows = conn.execute("SELECT i.id, i.subdomain, i.domain_mode, i.storage_limit_mb, a.is_admin "
                                "FROM instances i LEFT JOIN accounts a ON a.id = i.account_id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as error:
        log.warning("The platform backup applies the default storage limit to every wiki: %s", error)
        return {}
    limits: dict[str, int | None] = {}
    for instance_id, slug, mode, limit, admin in rows:
        if not isinstance(slug, str) or mode not in ("hosting", "apex"):
            continue
        name = slug + ("__apex" if mode == "apex" else "")
        if mode == "apex" or admin or limit == 0:
            limits[name] = None
        elif limit is None:
            limits[name] = held_limit(cfg.platform_state_dir, instance_id, cfg.limits.storage_limit_mb * 1024 ** 2)
        elif type(limit) is int and limit > 0:
            limits[name] = held_limit(cfg.platform_state_dir, instance_id, limit * 1024 ** 2)
    return limits


@dataclass(frozen=True)
class Exported:
    path: Path
    started: datetime
    # One {"tenant", "code", "detail"} per wiki the archive does not hold in full.
    skipped: tuple[dict[str, str], ...] = ()


class _Destination:
    """The encrypted stream, noting a failed write: that ends the whole backup, whichever wiki was being copied."""

    def __init__(self, output: crypto.EncryptedOutput):
        self._output = output
        self.failed = False

    def write(self, data: bytes) -> int:
        try:
            return self._output.write(data)
        except BaseException:
            self.failed = True
            raise

    def flush(self) -> None:
        try:
            self._output.flush()
        except BaseException:
            self.failed = True
            raise


def export(cfg: HostingConfig, destination_dir: Path, key: bytes, snapshot: Snapshot,
           release: Callable[[str, str], None], available: Callable[[], bool] | None = None) -> Exported:
    """Write the encrypted backup into *destination_dir*.

    ``snapshot(tenant)`` returns a fresh database copy (inside the tenant
    directory, with the size and SHA-256 its task reported), or None when
    the tenant has no database yet, and raises :class:`WikiFault` when the
    wiki's sandbox refused the copy; ``release(tenant, path)`` removes it
    again; ``available()`` tells whether the runtime that makes the
    snapshots still answers.

    * A wiki that fails is skipped and reported with a code (``db_unsafe``,
      ``too_large``, ``no_space``, ``failed``, or the runtime's own code).
      What was already written of it stays in the archive, and its files
      are still archived when only its database copy failed.
    * Besides its database copy, a wiki's files add at most its storage
      limit plus some slack, counted at their apparent size; a file with
      several hard links goes in once.
    * The whole backup fails only with ``unavailable`` (the runtime stopped
      answering after a wiki failed), or when the runtime could not make
      the database copy of any wiki. ``hosting.db``, the secret key and the
      output are all or nothing; their errors are raised as
      :class:`RuntimeFailure`.
    """
    try:
        return _export(cfg, Path(destination_dir), key, snapshot, release, available)
    except OSError as error:
        code = "no_space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "failed"
        raise RuntimeFailure(code, f"platform backup: {error.strerror or error}") from None
    except sqlite3.Error as error:
        raise RuntimeFailure("failed", f"platform backup: hosting.db could not be copied: {error}") from None


def _export(cfg: HostingConfig, destination_dir: Path, key: bytes, snapshot: Snapshot,
            release: Callable[[str, str], None], available: Callable[[], bool] | None) -> Exported:
    started = datetime.now(UTC)
    output = crypto.EncryptedOutput(destination_dir / f"{BACKUP_PREFIX}{started:%Y-%m-%d_%H-%M-%S}.zip.bwenc", key)
    destination = _Destination(output)
    saved: list[str] = []
    skipped: list[dict[str, str]] = []
    # Wikis whose database copy the runtime could not make: a sign of a platform fault.
    unsaved: list[str] = []
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:  # type: ignore[arg-type]
            database = destination_dir / ".hosting-snapshot.db"
            try:
                _snapshot_hosting_db(cfg, database)
                limits = _storage_limits(cfg, database)
                archive.write(database, "hosting.db")
            finally:
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{database}{suffix}").unlink(missing_ok=True)
            archive.writestr("secret_key", cfg.secret_key)
            for instance_id in quarantined_wikis(cfg.platform_state_dir):
                # The only thing keeping the wiki's plugin code off: a restore must not lift it.
                with contextlib.suppress(FileNotFoundError):  # lifted meanwhile
                    archive.write(PluginState(cfg.platform_state_dir, instance_id).marker,
                                  f"{STATE_PREFIX}/{instance_id}/{QUARANTINE_NAME}")
            for tenant in tenant_names(cfg.instances_dir):
                copied = False
                try:
                    copy, fault = _database_copy(snapshot, tenant)
                    copied = True
                    _add_tenant(archive, cfg, tenant, copy, fault, release,
                                limit=limits.get(tenant, cfg.limits.storage_limit_mb * 1024 ** 2))
                except Exception as error:  # noqa: BLE001 - whatever one wiki holds must not stop the others
                    if destination.failed:
                        raise
                    if available is not None and not available():
                        # Not this wiki's fault, and the next ones would fail alike: retry the whole backup.
                        raise RuntimeFailure("unavailable", f"platform backup stopped at {tenant}: the runtime "
                                                            "does not answer") from error
                    log.error("The platform backup does not hold %s in full: %s", tenant, one_line(error),
                              exc_info=not isinstance(error, (RuntimeFailure, OSError)))
                    skipped.append(_skipped(tenant, error))
                    # A wiki whose sandbox refused the copy (a damaged database), or whose own files stood in
                    # the way once it was made (a copy it removed, names a restore refuses), failed by itself.
                    if not copied and not isinstance(error, WikiFault):
                        unsaved.append(tenant)
                else:
                    saved.append(tenant)
            if skipped and len(unsaved) == len(skipped) and not saved:
                # Most likely the platform's fault (the agent, Docker) rather than every wiki's.
                codes = {item["code"] for item in skipped}
                raise RuntimeFailure(codes.pop() if len(codes) == 1 else "failed",
                                     f"platform backup: no wiki could be saved ({skipped[0]['tenant']}: "
                                     f"{skipped[0]['detail']})"[:300])
            manifest = {"format_version": 1, "source": "hosting-platform", "bananawiki_version": __version__,
                        "started_at": started.isoformat(timespec="seconds"), "tenants": saved, "skipped": skipped}
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
        path = output.finish()
    except BaseException:
        # A storage error can repeat while closing: the original error is the one to report.
        with contextlib.suppress(OSError):
            output.abort()
        raise
    return Exported(path, started, tuple(skipped))


def _skipped(tenant: str, error: Exception) -> dict[str, str]:
    if isinstance(error, RuntimeFailure):
        code, detail = error.code, str(error.detail)
    elif isinstance(error, OSError):
        code = "no_space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "failed"
        detail = str(error.strerror or error)
    else:
        code, detail = "failed", f"{type(error).__name__}: {error}"
    # Tenant file names may hold bytes that are not UTF-8 (surrogates): no page can show those.
    return {"tenant": tenant, "code": code, "detail": detail.encode("utf-8", "backslashreplace").decode()[:300]}


def _database_copy(snapshot: Snapshot, tenant: str) -> tuple[archives.DatabaseCopy | None, RuntimeFailure | None]:
    """The wiki's database copy, or why its sandbox refused to make one (a database the wiki damaged, or replaced
    with a link or special file): the rest of its files, which nothing else could replace, are kept, then the
    wiki is reported as not held in full."""
    try:
        return snapshot(tenant), None
    except RuntimeFailure as error:
        if not isinstance(error, WikiFault) and error.code != "db_unsafe":
            raise
        return None, error


def _special(dir_fd: int, name: str) -> bool:
    """Whether *name* exists in *dir_fd* as a link or a special file (neither a folder nor a regular file)."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return False
    return not (stat.S_ISDIR(mode) or stat.S_ISREG(mode))


def _replaced_folders(root: Path) -> list[str]:
    """The storage folders the wiki replaced with a link or a special file.

    The walk skips those silently and a restore prepares empty folders in
    their place, so the files the wiki keeps there would be missing. A
    regular file there is one of the files the archive leaves out.
    """
    root_fd = tenantfs.open_dir(root)
    try:
        if _special(root_fd, "storage"):
            return ["storage"]
        try:
            storage_fd = os.open("storage", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=root_fd)
        except OSError:
            # Missing (a restore prepares it), a regular file, or swapped for a link just now.
            return ["storage"] if _special(root_fd, "storage") else []
        try:
            return [f"storage/{name}" for name in tenantfs.ASSET_FOLDERS if _special(storage_fd, name)]
        finally:
            os.close(storage_fd)
    finally:
        os.close(root_fd)


def _add_tenant(archive: zipfile.ZipFile, cfg: HostingConfig, tenant: str, copy: archives.DatabaseCopy | None,
                fault: RuntimeFailure | None, release: Callable[[str, str], None], *, limit: int | None) -> None:
    """Archive one wiki: its database copy, then its files within its byte budget.

    One wiki must not make the whole backup fail for want of space, and a
    sparse file or many hard links to one file cost its storage quota next
    to nothing: besides its database copy, its files may add at most its
    storage *limit* (None: no limit) and some slack to the archive, each
    file once, whatever number of hard links it has. The database copy
    goes in only as its task reported it, and within the same budget.
    """
    root = tenantfs.tenant_path(cfg.instances_dir, tenant)
    reserved: tuple[str, ...] = ()
    budget = byte_budget(limit)
    links: list[str] = []
    try:
        if copy:
            reserved = (f"instances/{tenant}/bananawiki.db",)
            try:
                fd, info = archives.open_copy(root, copy, max_bytes=budget)
            except (RuntimeFailure, OSError) as error:
                # The wiki removed the copy its sandbox made, replaced it or
                # extended it (to a sparse file of any length): its own doing,
                # and its files are still worth saving.
                fault = error if isinstance(error, RuntimeFailure) else RuntimeFailure(
                    "failed", f"the database copy: {error.strerror or error}")
            else:
                try:
                    archives.write_copy(archive, fd, info, copy, reserved[0], cfg.archives)
                except RuntimeFailure as error:
                    # It shrank or changed while it was copied; the member is closed
                    # properly. A failed write to the backup is an OSError instead.
                    fault = error
        # The wiki can turn its database into a folder meanwhile: nothing may
        # then be archived below the copy's name, or no restore would accept it.
        # Nor may a file stand where the restore prepares the wiki's folders.
        prefix = f"instances/{tenant}/"
        layout = (f"{prefix}storage", f"{prefix}external_plugins",
                  *(f"{prefix}storage/{name}" for name in tenantfs.ASSET_FOLDERS))
        refused = archives.add_tenant_tree(archive, root, prefix, cfg.archives, portable=False, reserved=reserved,
                                           directories=layout, budget=budget, links=links)
        replaced = _replaced_folders(root)
    finally:
        if copy:
            # Never the wiki's verdict: maintenance removes a copy left behind.
            try:
                release(tenant, copy.path)
            except (RuntimeFailure, OSError) as error:
                log.warning("The backup copy of %s's database was not removed: %s", tenant, error)
    if fault is not None:
        raise fault
    if replaced:
        # Like a database turned into a link: the wiki's files there are not in the archive.
        raise RuntimeFailure("db_unsafe", f"replaced by a link or a special file: {', '.join(replaced)}")
    if refused:
        # The rest of the wiki is in the archive, but a backup without these files is not a complete one.
        raise RuntimeFailure("failed", archives.refused_detail(refused))
    if links:
        # A restore would hold each file under one of its names only.
        raise RuntimeFailure("failed", f"{len(links)} further hard link(s) to files already saved left out: "
                                       f"{', '.join(links[:5])}"[:300])


# Restore


def _staged_name(name: str) -> PurePosixPath | None:
    """Where a backup member goes in the staging tree, or None to ignore it."""
    # The rule backups are written by (archives.storable_name): writer and reader cannot drift apart.
    if archives.unsafe_name(name):
        raise RuntimeFailure("archive_invalid", "the backup contains an unsafe path")
    parts = PurePosixPath(name).parts
    if name in DATABASE_NAMES:
        return PurePosixPath("hosting.db")
    if name in SECRET_NAMES:
        return PurePosixPath("secret_key")
    if name == BACKUP_KEY_NAME:
        return PurePosixPath("backup_key")
    if len(parts) == 3 and parts[0] == STATE_PREFIX and parts[2] == QUARANTINE_NAME:
        if not INSTANCE_ID.fullmatch(parts[1]):
            raise RuntimeFailure("archive_invalid", "the backup names a plugin quarantine of an invalid wiki")
        return PurePosixPath(name)
    if len(parts) >= 4 and parts[:2] == ("hosting", "instances"):
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] == "instances":
        tenantfs.tenant_path("/", parts[1])
        return PurePosixPath(*parts)
    return None


def _check_database(path: Path) -> None:
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeFailure("archive_invalid", "the hosting database in the backup is damaged")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not tables >= PLATFORM_TABLES:
            raise RuntimeFailure("archive_invalid", "the backup holds no hosting platform database")
    except sqlite3.DatabaseError:
        raise RuntimeFailure("archive_invalid", "the hosting database in the backup is not readable") from None
    finally:
        conn.close()


def _installation_empty(cfg: HostingConfig) -> bool:
    conn = sqlite3.connect(f"{Path(cfg.database_path).absolute().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute("SELECT 1 FROM instances LIMIT 1").fetchone() is None
    finally:
        conn.close()


def check_restore_target(cfg: HostingConfig) -> None:
    """A web restore may only write into a fresh platform, before touching containers."""
    if not _installation_empty(cfg) or tenant_names(cfg.instances_dir):
        raise RuntimeFailure("data_exists", "restore onto a fresh installation without wikis or tenant data")


def _incomplete(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> list[str]:
    """The wikis a backup's manifest says it does not hold in full, as ``tenant (code)``."""
    if entry.file_size > archives.MAX_MANIFEST_BYTES:
        return []
    try:
        with archive.open(entry) as source:
            manifest = safe_json_loads(source.read(archives.MAX_MANIFEST_BYTES + 1))
    except (zipfile.BadZipFile, EOFError, ValueError, RuntimeError, zlib.error, OSError):
        # Only a report: the members themselves are checked like any others.
        return []
    skipped = manifest.get("skipped") if isinstance(manifest, dict) else None
    return [f"{item['tenant']} ({item.get('code')})" for item in (skipped if isinstance(skipped, list) else ())
            if isinstance(item, dict) and isinstance(item.get("tenant"), str)
            and tenantfs.DATA_DIR_NAME.fullmatch(item["tenant"]) and item.get("code") in FAILURE_CODES]


def _stage(parts: Sequence[Path], stage: Path, cfg: HostingConfig, key: Callable[[], bytes]) -> list[str]:
    """Check and unpack every part into *stage*; returns the wikis the backup's manifest says are incomplete."""
    count = total = 0
    seen: set[str] = set()
    folders: set[str] = set()
    incomplete: list[str] = []
    for index, part in enumerate(parts):
        path = Path(part)
        if path.stat().st_size > cfg.archives.import_max_bytes:
            raise RuntimeFailure("too_large", "a backup part exceeds the configured upload limit")
        if crypto.is_encrypted(path):
            if shutil.disk_usage(stage).free < path.stat().st_size + cfg.archives.import_min_free_bytes:
                raise RuntimeFailure("no_space", "not enough free space to decrypt the backup")
            path = crypto.decrypt_file(path, stage / f".part-{index}.zip", key())
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise RuntimeFailure("archive_invalid", "a backup part is not a ZIP archive") from None
        with archive:
            for entry in archive.infolist():
                count += 1
                total += entry.file_size
                if count > cfg.archives.import_max_members or total > cfg.archives.import_max_extracted_bytes:
                    raise RuntimeFailure("too_large", "the backup exceeds the configured extraction limits")
                if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise RuntimeFailure("archive_invalid", "the backup contains links or special files")
                if not zipcheck.readable_member(entry):
                    raise RuntimeFailure("archive_invalid", "the backup contains encrypted or unsupported members")
                if entry.filename == MANIFEST_NAME:
                    incomplete += _incomplete(archive, entry)
                    continue
                relative = None if entry.is_dir() else _staged_name(entry.filename)
                if relative is None:
                    continue
                if str(relative) in seen:
                    raise RuntimeFailure("archive_invalid", "the backup contains duplicate files")
                parents = [str(parent) for parent in relative.parents if parent.parts]
                if str(relative) in folders or any(parent in seen for parent in parents):
                    # Earlier backups can hold this: a wiki made its database a folder while it was copied.
                    raise RuntimeFailure("archive_invalid",
                                         f"the backup uses {relative} as both a file and a folder"[:300])
                if shutil.disk_usage(stage).free < entry.file_size + cfg.archives.import_min_free_bytes:
                    raise RuntimeFailure("no_space", "not enough free space to restore the backup")
                seen.add(str(relative))
                folders.update(parents)
                target = stage / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(entry) as source, open(target, "xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(source, output, 1024 * 1024)
        if path != Path(part):
            path.unlink()
    return incomplete


def _pending_copy(source: Path, destination: Path, mode: int = 0o600) -> Path:
    """A synced copy of *source* next to *destination*, for :func:`os.replace` to put in its place."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(dir=destination.parent, prefix=".restore-")
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as handle:
            shutil.copyfileobj(handle, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(pending, mode)
    except BaseException:
        Path(pending).unlink(missing_ok=True)
        raise
    return Path(pending)


def _install_file(source: Path, destination: Path, mode: int = 0o600) -> None:
    pending = _pending_copy(source, destination, mode)
    try:
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)


def _restored_limit(tenant_dir: Path, limit: int) -> int:
    """*limit*, or what the staged wiki *tenant_dir* takes once restored (in whole 4 KiB blocks) and the runtime
    agent's adoption headroom, rounded up to a MiB, when that is more."""
    used = 4096 + sum(-(-entry.stat().st_size // 4096) * 4096 if entry.is_file() else 4096
                      for entry in tenant_dir.rglob("*"))
    return limit if used <= limit else -(-(used + ADOPTION_HEADROOM_BYTES) // 1024 ** 2) * 1024 ** 2


def _refuse_links(root: Path, target: Path) -> None:
    """Never write through a link left in an existing tenant directory."""
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeFailure("invalid", f"a restore target inside {root.name} is a link")


def _install_tenants(staged: Path, cfg: HostingConfig, *,
                     prepare_tenant: Callable[[str, int], None] | None = None,
                     storage_limits: dict[str, int] | None = None) -> list[Path]:
    """Install every staged wiki; returns their new directories, or removes them all again on a failure.

    Each wiki's storage quota is prepared on its empty directory, at its
    limit from *storage_limits* (bytes by data directory, 0: the runtime
    agent's own ceiling). A wiki the agent held above its plan holds more
    than that limit, and the agent can only adopt data that is already
    there: its quota is prepared for what it restores, and its first start
    adopts it above its plan as on any other host.
    """
    base = Path(cfg.instances_dir)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for tenant_dir in sorted(staged.iterdir()):
            root = tenantfs.tenant_path(base, tenant_dir.name)
            # Restore requires a fresh target. Never adopt or clean a directory
            # that existed before this operation.
            root.mkdir(mode=0o700)
            created.append(root)
            if prepare_tenant is not None:
                limit = (storage_limits or {}).get(tenant_dir.name, cfg.limits.storage_limit_mb * 1024 ** 2)
                prepare_tenant(tenant_dir.name, limit and _restored_limit(tenant_dir, limit))
            for source in sorted(tenant_dir.rglob("*")):
                if not source.is_file():
                    continue
                target = root / source.relative_to(tenant_dir)
                _refuse_links(root, target)
                _install_file(source, target)
            try:
                tenantfs.ensure_layout(root)
            except RuntimeFailure as error:
                # Older backups can hold a file where a folder belongs: say whose.
                raise RuntimeFailure(error.code, f"{tenant_dir.name}: {error.detail}"[:300]) from None
    except BaseException:
        # Any failure, a refused quota included, removes what this restore
        # created, so the target stays fresh and the restore can be retried.
        for root in reversed(created):
            shutil.rmtree(root)
        raise
    return created


def _install_quarantines(staged: Path, cfg: HostingConfig) -> list[Path]:
    """Put back the plugin quarantine of every wiki the backup holds one for; returns the markers it set."""
    created: list[Path] = []
    try:
        for marker in sorted(staged.glob(f"*/{QUARANTINE_NAME}")):
            state = PluginState(cfg.platform_state_dir, marker.parent.name)
            if not state.quarantined():
                state.set_quarantine(True)
                created.append(state.marker)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return created


def _restore_limits(cfg: HostingConfig, database: Path) -> dict[str, int]:
    """Each wiki's storage limit in bytes by data directory, read from the staged ``hosting.db``, as the portal
    asks the runtime agent for it: 0 (the agent's own ceiling) for an administrator's wiki, an apex wiki or a
    limit of 0, the default limit for a wiki without one of its own."""
    limits: dict[str, int] = {}
    try:
        # The encrypted platform database has already been validated. Read
        # only quota policy; tenant SQLite files remain sandbox-only.
        with contextlib.closing(sqlite3.connect(f"{database.absolute().as_uri()}?mode=ro", uri=True)) as policy:
            policy.execute("PRAGMA trusted_schema=OFF")
            for slug, mode, limit, admin in policy.execute(
                    "SELECT i.subdomain, i.domain_mode, i.storage_limit_mb, a.is_admin "
                    "FROM instances i LEFT JOIN accounts a ON a.id = i.account_id"):
                if (not isinstance(slug, str) or mode not in {"hosting", "apex"}
                        or limit is not None and (type(limit) is not int or limit < 0)):
                    raise RuntimeFailure("archive_invalid", "the backup has an invalid tenant storage policy")
                name = slug + ("__apex" if mode == "apex" else "")
                tenantfs.tenant_path(cfg.instances_dir, name)
                if mode == "apex" or admin:
                    limits[name] = 0
                else:
                    limits[name] = (cfg.limits.storage_limit_mb if limit is None else limit) * 1024 ** 2
    except sqlite3.DatabaseError:
        raise RuntimeFailure("archive_invalid", "the backup tenant storage policy is not readable") from None
    return limits


def _write_database(database: Path, cfg: HostingConfig) -> None:
    source = sqlite3.connect(str(database))
    try:
        target = sqlite3.connect(cfg.database_path, timeout=20)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _install(stage: Path, cfg: HostingConfig, secret_key_path: str, *,
             prepare_tenant: Callable[[str, int], None] | None, storage_limits: dict[str, int]) -> None:
    """Install the staged platform, leaving the fresh installation as it was when any step fails.

    The keys are first copied next to the ones they replace, then the
    plugin quarantines, the wikis and ``hosting.db`` are installed, and only
    then do the keys take the place of the current ones: a failure on the
    way (a full disk, say) must leave a target a retry accepts, not wikis
    on disk without the database that names them.
    """
    keys = [(stage / name, Path(path)) for name, path in (("secret_key", secret_key_path),
                                                          ("backup_key", cfg.backup_key_path))]
    pending: list[tuple[Path, Path]] = []
    markers: list[Path] = []
    created: list[Path] = []
    try:
        for source, destination in keys:
            if source.is_file():
                pending.append((_pending_copy(source, destination), destination))
        try:
            if (stage / STATE_PREFIX).is_dir():
                markers = _install_quarantines(stage / STATE_PREFIX, cfg)
            if (stage / "instances").is_dir():
                created = _install_tenants(stage / "instances", cfg, prepare_tenant=prepare_tenant,
                                           storage_limits=storage_limits)
            _write_database(stage / "hosting.db", cfg)
        except BaseException:
            for root in reversed(created):
                shutil.rmtree(root)
            for marker in markers:
                marker.unlink(missing_ok=True)
            raise
        for path, destination in pending:
            os.replace(path, destination)
    finally:
        for path, _destination in pending:
            path.unlink(missing_ok=True)


def restore(cfg: HostingConfig, parts: Sequence[Path], key: Callable[[], bytes], secret_key_path: str, *,
            prepare_tenant: Callable[[str, int], None] | None = None) -> None:
    """Validate every part, then install the platform (:func:`_install`); storage and SQLite errors are raised
    as :class:`RuntimeFailure`."""
    check_restore_target(cfg)
    data_dir = Path(cfg.database_path).parent
    with tempfile.TemporaryDirectory(prefix="restore-", dir=data_dir) as temporary:
        stage = Path(temporary)
        try:
            incomplete = _stage(parts, stage, cfg, key)
        except (zipfile.BadZipFile, EOFError, ValueError, RuntimeError, NotImplementedError):
            raise RuntimeFailure("archive_invalid", "a backup part is damaged or unsupported") from None
        except OSError as error:
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                raise RuntimeFailure("no_space", "not enough free space to restore the backup") from None
            raise RuntimeFailure("archive_invalid", "a backup part cannot be read") from None
        database = stage / "hosting.db"
        if not database.is_file():
            raise RuntimeFailure("archive_invalid", "the backup holds no hosting database")
        _check_database(database)
        storage_limits = _restore_limits(cfg, database) if prepare_tenant is not None else {}
        secret = stage / "secret_key"
        if secret.is_file() and not 32 <= len(secret.read_bytes().strip()) <= 4096:
            raise RuntimeFailure("archive_invalid", "the backup holds an invalid session key")
        backup_key = stage / "backup_key"
        if backup_key.is_file() and backup_key.stat().st_size != 32:
            raise RuntimeFailure("archive_invalid", "the backup encryption key must be 32 bytes")
        try:
            _install(stage, cfg, secret_key_path, prepare_tenant=prepare_tenant, storage_limits=storage_limits)
        except OSError as error:
            code = "no_space" if error.errno in (errno.ENOSPC, errno.EDQUOT) else "failed"
            raise RuntimeFailure(code, f"platform restore: {error.strerror or error}") from None
        except sqlite3.Error as error:
            raise RuntimeFailure("failed", f"platform restore: hosting.db could not be written: {error}") from None
    if incomplete:
        log.warning("The restored backup did not hold these wikis in full: %s", ", ".join(incomplete[:50]))
    log.warning("The hosting platform was restored from a backup; restart the portal.")
