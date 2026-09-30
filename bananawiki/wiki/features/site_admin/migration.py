"""Whole-site export and import (moving a wiki to another server).

Archive format
--------------
The unified BananaWiki archive of 1.4, which the hosting portal also reads::

    manifest.json        {"format_version": 1, "source": "standalone", ...}
    bananawiki.db        consistent online copy of the database
    uploads/ attachments/ chat_attachments/ kanban_attachments/ custom_page_files/
    favicons/            only ``custom_*`` icons
    translations/        uploaded interface languages (1.6 only)

Imports also accept the two older 1.4 shapes: everything nested under one
``<slug>/`` folder (hosting), and ``site_export.json`` with ``assets/<kind>/``
folders and no database file (early standalone exports; the JSON dump is
turned into a 1.4 database and upgraded like any other).

Safety (1.4 audit C2)
---------------------
An import only ever writes the database and the data folders above. Code
(``config.py``, plugins), the secret key and anything else in the archive is
ignored. Nothing changes until the whole archive has been unpacked into a
staging folder and the database has passed an integrity check, carries the
BananaWiki application id and a schema version this release can upgrade,
upgrades cleanly, and contains a finished setup, an administrator and a home
page. The live database is then replaced through SQLite's backup API (safe
with other workers' open connections) while maintenance mode is on, after a
copy of the current database and files was put aside in
``<instance>/backups/pre-import-<time>/``. Any failure restores that copy.
Login sessions never travel in either direction.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from base64 import b64decode
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from .... import __version__
from ....core.i18n import valid_code
from ....core.sqlite import (
    DatabaseUnavailable,
    apply_migrations,
    column_names,
    dict_row,
    execute_script,
    quote_identifier,
    schema_version,
    table_exists,
)
from ... import migrations, settings, storage
from ...i18n import catalog
from . import languages

log = logging.getLogger("bananawiki.site_admin.migration")

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
RAW_DB = "bananawiki.db"
JSON_DUMP = "site_export.json"
ASSET_FOLDERS = ("uploads", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files", "favicons")
LANGUAGE_FOLDER = "translations"
MAX_MEMBERS = 50_000
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_RATIO = 200
EXPORT_PREFIX = "bw-site-export-"
IMPORT_PREFIX = "bw-site-import-"
STALE_SECONDS = 24 * 3600
# Tables never carried over: live credentials and regenerable or per-process state.
VOLATILE_TABLES = ("user_sessions", "job_runs", "tts_generations", "rate_limit_hits")
_STORED_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "zip", "gz", "mp3", "mp4", "webm", "ogg", "m4a"})


class MigrationError(ValueError):
    """The export or import was refused; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def _cfg():
    return current_app.config["BW"]


def _database():
    return current_app.extensions["bananawiki.database"]


def work_root() -> Path:
    root = Path(_cfg().folders.exports)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def cleanup_stale() -> int:
    """Remove export and import folders older than a day (background job)."""
    removed = 0
    cutoff = time.time() - STALE_SECONDS
    for entry in work_root().iterdir():
        if entry.name.startswith((EXPORT_PREFIX, IMPORT_PREFIX)) and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


# ── Export ────────────────────────────────────────────────────────────────────


def _asset_files(folder: str) -> list[tuple[Path, str]]:
    root = Path(getattr(_cfg().folders, folder))
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name.startswith(".upload-"):
            continue
        relative = path.relative_to(root).as_posix()
        if folder == "favicons" and not path.name.startswith("custom_"):
            continue
        found.append((path, f"{folder}/{relative}"))
    return found


def _language_files() -> list[tuple[Path, str]]:
    folder = catalog().custom_dir
    if not folder or not folder.is_dir():
        return []
    return [(path, f"{LANGUAGE_FOLDER}/{path.name}") for path in sorted(folder.glob("*.json"))
            if path.is_file() and valid_code(path.stem) == path.stem]


def _strip_volatile(conn: sqlite3.Connection) -> None:
    for table in VOLATILE_TABLES:
        if table_exists(conn, table):
            conn.execute(f"DELETE FROM {quote_identifier(table)}")


def export_archive() -> tuple[Path, str]:
    """Build the archive in a private folder under the exports directory; return (path, download name)."""
    workdir = Path(tempfile.mkdtemp(prefix=EXPORT_PREFIX, dir=work_root()))
    try:
        snapshot = _database().backup_to(workdir / "snapshot.db")
        conn = sqlite3.connect(str(snapshot), isolation_level=None)
        try:
            _strip_volatile(conn)
            conn.execute("VACUUM")  # drop the freed pages that held session digests
            app_id, version = schema_version(conn)
        finally:
            conn.close()
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        name = f"site_export_{stamp}.zip"
        manifest = {
            "format_version": FORMAT_VERSION, "source": "standalone", "exported_at": datetime.now(UTC).isoformat(),
            "has_raw_db": True, "has_site_export_json": False, "bananawiki_version": __version__,
            "application_id": app_id, "schema_version": version,
        }
        path = workdir / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
            archive.write(snapshot, RAW_DB)
            files = [item for folder in ASSET_FOLDERS for item in _asset_files(folder)] + _language_files()
            for source, arcname in files:
                stored = source.suffix.lower().lstrip(".") in _STORED_EXTENSIONS
                archive.write(source, arcname, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        snapshot.unlink()
        return path, name
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


# ── Import: reading the archive ──────────────────────────────────────────────


@dataclass
class Staged:
    """An archive unpacked and checked, ready to replace the live site."""

    workdir: Path
    database: Path
    folders: dict[str, Path] = field(default_factory=dict)
    languages: list[Path] = field(default_factory=list)
    skipped_members: int = 0
    skipped_languages: int = 0

    def discard(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _check_members(archive: zipfile.ZipFile, archive_size: int) -> list[zipfile.ZipInfo]:
    members = [info for info in archive.infolist() if not info.is_dir()]
    if len(members) > MAX_MEMBERS:
        raise MigrationError("site_admin.migration.error.too_many_files", limit=MAX_MEMBERS)
    total = sum(info.file_size for info in members)
    if total > 4 * _cfg().max_import_size:
        raise MigrationError("site_admin.migration.error.too_large_unpacked")
    for info in members:
        if info.file_size > 10 * 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise MigrationError("site_admin.migration.error.suspicious")
    free = shutil.disk_usage(work_root()).free
    if free < total + archive_size + 64 * 1024 * 1024:
        raise MigrationError("site_admin.migration.error.disk_space")
    return members


def _layout(names: set[str]) -> str:
    """The prefix the payload lives under ("" or "<slug>/" for 1.4 hosting archives)."""
    if MANIFEST in names or RAW_DB in names or JSON_DUMP in names or any(n.startswith("assets/") for n in names):
        return ""
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(tops) == 1:
        prefix = next(iter(tops)) + "/"
        if prefix + RAW_DB in names:
            return prefix
    return ""


def _normalise(name: str, prefix: str) -> PurePosixPath | None:
    """The member's path inside the unified layout, or None when it is unsafe."""
    name = name.replace("\\", "/")
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    if name.startswith("assets/"):
        name = name[len("assets/"):]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _extract(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, open(target, "wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)


def _read_manifest(archive: zipfile.ZipFile, names: set[str]) -> None:
    if MANIFEST not in names:
        return
    try:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MigrationError("site_admin.migration.error.manifest") from None
    version = manifest.get("format_version") if isinstance(manifest, dict) else None
    if not isinstance(version, int) or version < 1:
        raise MigrationError("site_admin.migration.error.manifest")
    if version > FORMAT_VERSION:
        raise MigrationError("site_admin.migration.error.newer_format")


def stage(upload: FileStorage | None) -> Staged:
    """Save, unpack and validate an uploaded archive without touching the live site."""
    if upload is None or not upload.filename:
        raise MigrationError("site_admin.migration.error.no_file")
    workdir = Path(tempfile.mkdtemp(prefix=IMPORT_PREFIX, dir=work_root()))
    try:
        archive_path = workdir / "upload.zip"
        upload.save(str(archive_path))
        size = archive_path.stat().st_size
        if size > _cfg().max_import_size:
            raise MigrationError("site_admin.migration.error.too_large",
                                 limit_mb=_cfg().max_import_size // (1024 * 1024))
        try:
            archive = zipfile.ZipFile(archive_path)
        except (zipfile.BadZipFile, OSError):
            raise MigrationError("site_admin.migration.error.not_zip") from None
        with archive:
            staged = _unpack(archive, workdir, size)
        archive_path.unlink()
        _prepare_database(staged.database)
        return staged
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def _unpack(archive: zipfile.ZipFile, workdir: Path, size: int) -> Staged:
    members = _check_members(archive, size)
    names = {info.filename for info in members}
    _read_manifest(archive, names)
    prefix = _layout(names)
    staged = Staged(workdir=workdir, database=workdir / "site.db")
    json_member = None
    for info in members:
        path = _normalise(info.filename, prefix)
        if path is None or _is_symlink(info):
            staged.skipped_members += 1
            continue
        top = path.parts[0]
        if len(path.parts) == 1 and top == RAW_DB:
            _extract(archive, info, staged.database)
        elif len(path.parts) == 1 and top == JSON_DUMP:
            json_member = info
        elif top in ASSET_FOLDERS and len(path.parts) > 1:
            if top == "favicons" and (len(path.parts) != 2 or not path.name.startswith("custom_")):
                staged.skipped_members += 1
                continue
            target = workdir / "files" / path
            _extract(archive, info, target)
            staged.folders[top] = workdir / "files" / top
        elif top == LANGUAGE_FOLDER and len(path.parts) == 2 and path.suffix == ".json":
            _stage_language(archive, info, path, staged)
        else:
            staged.skipped_members += 1  # code, secrets, logs, plugins: never imported
    if not staged.database.exists():
        if json_member is None:
            raise MigrationError("site_admin.migration.error.no_database")
        if json_member.file_size > MAX_JSON_BYTES:
            raise MigrationError("site_admin.migration.error.too_large_unpacked")
        _database_from_json(archive.read(json_member), staged.database)
    return staged


def _stage_language(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: PurePosixPath, staged: Staged) -> None:
    code = valid_code(path.stem)
    try:
        if code != path.stem or info.file_size > languages.MAX_FILE_BYTES:
            raise languages.LanguageError("site_admin.languages.error.unknown")
        parsed_code, data, _ignored = languages.validate_pack(json.loads(archive.read(info).decode("utf-8")))
        if parsed_code != code:
            raise languages.LanguageError("site_admin.languages.error.meta")
    except (languages.LanguageError, UnicodeDecodeError, ValueError):
        staged.skipped_languages += 1
        return
    target = staged.workdir / LANGUAGE_FOLDER / f"{code}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    staged.languages.append(target)


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__b64__"}:
        return b64decode(value["__b64__"])
    return value


def _database_from_json(raw: bytes, target: Path) -> None:
    """Turn a 1.4 ``site_export.json`` dump into a 1.4 (version 3) database."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MigrationError("site_admin.migration.error.json") from None
    if not isinstance(data, dict):
        raise MigrationError("site_admin.migration.error.json")
    conn = sqlite3.connect(str(target), isolation_level=None)
    try:
        execute_script(conn, (Path(migrations.__file__).with_name("baseline_v3.sql")).read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        for table, rows in data.items():
            if table.startswith("_") or table in VOLATILE_TABLES or not isinstance(rows, list):
                continue
            try:
                quoted = quote_identifier(table)
            except ValueError:
                continue
            if not table_exists(conn, table):
                continue
            columns = column_names(conn, table)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values = {key: _decode(value) for key, value in row.items() if key in columns}
                if values:
                    names = ", ".join(quote_identifier(key) for key in values)
                    marks = ", ".join("?" for _ in values)
                    conn.execute(f"INSERT OR REPLACE INTO {quoted} ({names}) VALUES ({marks})", tuple(values.values()))
        conn.execute("INSERT OR IGNORE INTO site_settings (id) VALUES (1)")
        conn.execute("INSERT OR IGNORE INTO cleanup_lease (id) VALUES (1)")
        conn.execute("INSERT OR IGNORE INTO federation_identity (id, wiki_id) VALUES (1, ?)", (str(uuid.uuid4()),))
        conn.execute(f"PRAGMA application_id={int(migrations.APPLICATION_ID)}")
        conn.execute(f"PRAGMA user_version={int(migrations.BASELINE)}")
        conn.execute("COMMIT")
    except sqlite3.Error:
        raise MigrationError("site_admin.migration.error.json") from None
    finally:
        conn.close()


# ── Import: checking and preparing the database ──────────────────────────────


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = dict_row
    return conn


def _prepare_database(path: Path) -> None:
    """Validate, upgrade and clean the staged database (raises MigrationError)."""
    try:
        conn = _open(path)
    except sqlite3.Error:
        raise MigrationError("site_admin.migration.error.not_database") from None
    try:
        _check_identity(conn)
        try:
            apply_migrations(conn, migrations.APPLICATION_ID, migrations.MIGRATIONS, baseline=migrations.BASELINE)
        except (DatabaseUnavailable, sqlite3.Error) as error:
            log.warning("Site import: the archive's database could not be upgraded: %s", error)
            raise MigrationError("site_admin.migration.error.upgrade") from None
        _check_content(conn)
        _strip_volatile(conn)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(f"PRAGMA page_size={_live_page_size()}")
        conn.execute("VACUUM")
    finally:
        conn.close()


def _check_identity(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        app_id, version = schema_version(conn)
    except sqlite3.DatabaseError:
        raise MigrationError("site_admin.migration.error.not_database") from None
    if not row or next(iter(row.values())) != "ok":
        raise MigrationError("site_admin.migration.error.damaged")
    if app_id != migrations.APPLICATION_ID:
        raise MigrationError("site_admin.migration.error.other_application")
    if version < migrations.BASELINE:
        raise MigrationError("site_admin.migration.error.too_old")
    if version > migrations.LATEST:
        raise MigrationError("site_admin.migration.error.too_new")


def _check_content(conn: sqlite3.Connection) -> None:
    """A replacement must leave a usable wiki behind (1.4 import checks)."""
    try:
        setup = conn.execute("SELECT setup_done FROM site_settings WHERE id = 1").fetchone()
        admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role IN ('admin', 'owner')").fetchone()
        home = conn.execute("SELECT COUNT(*) AS n FROM pages WHERE is_home = 1").fetchone()
    except sqlite3.Error:
        raise MigrationError("site_admin.migration.error.incomplete") from None
    if not setup or not setup["setup_done"] or not admins["n"] or not home["n"]:
        raise MigrationError("site_admin.migration.error.incomplete")


def _live_page_size() -> int:
    conn = _database().connect(readonly=True)
    try:
        return int(next(iter(conn.execute("PRAGMA page_size").fetchone().values())))
    finally:
        conn.close()


# ── Import: replacing the live site ──────────────────────────────────────────


def _custom_favicons(folder: Path) -> list[Path]:
    return [path for path in folder.iterdir() if path.name.startswith("custom_")] if folder.is_dir() else []


def _folder_entries(name: str) -> list[Path]:
    folder = Path(getattr(_cfg().folders, name))
    if name == "favicons":
        return _custom_favicons(folder)
    return list(folder.iterdir()) if folder.is_dir() else []


def _move_all(entries: list[Path], destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in entries:
        shutil.move(str(entry), str(destination / entry.name))


def _language_dir() -> Path:
    folder = catalog().custom_dir
    assert folder is not None
    return folder


def _swap_files(staged: Staged, backup: Path) -> None:
    """Put the current files aside in *backup* and move the staged ones into place."""
    for name in ASSET_FOLDERS:
        _move_all(_folder_entries(name), backup / "files" / name)
        source = staged.folders.get(name)
        if source is not None:
            _move_all(list(source.iterdir()), storage.folder_path(name))
    _move_all(list(_language_dir().glob("*.json")) if _language_dir().is_dir() else [], backup / LANGUAGE_FOLDER)
    _move_all(staged.languages, _language_dir())


def _restore_files(backup: Path) -> None:
    for name in ASSET_FOLDERS:
        _move_all(_folder_entries(name), backup / "discarded" / name)
        kept = backup / "files" / name
        if kept.is_dir():
            _move_all(list(kept.iterdir()), storage.folder_path(name))
    _move_all(list(_language_dir().glob("*.json")) if _language_dir().is_dir() else [], backup / "discarded" / "l")
    kept = backup / LANGUAGE_FOLDER
    if kept.is_dir():
        _move_all(list(kept.iterdir()), _language_dir())


def _copy_database(source: Path) -> None:
    """Replace the live database's content with *source* (SQLite backup API)."""
    src = sqlite3.connect(str(source))
    dst = _database().connect()
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _refresh_caches() -> None:
    from flask import g, has_request_context

    settings.invalidate()
    current_app.extensions.pop("bananawiki.settings_columns", None)
    catalog().reload()
    storage._usage_cache.clear()  # noqa: SLF001 - usage changed wholesale
    if has_request_context():
        g.pop("_plugin_states", None)


def apply(staged: Staged) -> Path:
    """Replace the live database and files with *staged*; return the backup folder."""
    backup = Path(_cfg().instance_dir) / "backups" / f"pre-import-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    backup.mkdir(mode=0o700, parents=True)
    previous_maintenance = settings.get("maintenance_mode", 0)
    settings.update({"maintenance_mode": 1})
    _database().backup_to(backup / RAW_DB)
    try:
        _swap_files(staged, backup)
        _copy_database(staged.database)
    except BaseException:
        log.exception("Site import failed; restoring the previous site from %s", backup)
        _restore_files(backup)
        _copy_database(backup / RAW_DB)
        _refresh_caches()
        settings.update({"maintenance_mode": previous_maintenance})
        raise
    finally:
        staged.discard()
    _refresh_caches()
    log.warning("Site import replaced the database and files; the previous site is kept in %s", backup)
    return backup


def import_allowed() -> bool:
    return bool(_cfg().allow_site_import)
