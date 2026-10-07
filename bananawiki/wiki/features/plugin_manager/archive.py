"""Installing ``.bwplugin`` archives and removing installed plugins.

An archive is a ZIP holding ``plugin.json`` and ``__init__.py`` either at
its root or inside one top-level folder. Before anything is written it must
pass :func:`inspect`: no absolute paths, traversal, links or special files,
no encrypted or duplicate members, no compiled code, and size, count and
compression-ratio limits against ZIP bombs. The files are unpacked into a
hidden staging folder, the plugin is registered disabled, and only then does
the folder get its real name. No plugin code runs during installation.
"""

from __future__ import annotations

import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from ....core.json import loads as safe_json_loads
from ....core.timeutil import now_sql
from ... import plugins_external, registry
from ...db import db
from ...plugins_external import Manifest, ManifestError

MIB = 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * MIB
MAX_UNCOMPRESSED = 50 * MIB
MAX_FILE_SIZE = 10 * MIB
MAX_FILES = 5_000
MAX_RATIO = 200
FORBIDDEN_SUFFIXES = (".so", ".pyd", ".dll", ".dylib", ".exe", ".pyc", ".pyo")
_DRIVE = re.compile(r"^[A-Za-z]:")

Member = tuple[zipfile.ZipInfo, tuple[str, ...]]


def _member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename
    if not raw or "\x00" in raw:
        raise ManifestError("plugin_manager.zip.bad_path")
    name = raw.replace("\\", "/")
    if name.startswith("/") or _DRIVE.match(name):
        raise ManifestError("plugin_manager.zip.absolute_path")
    parts = tuple(part for part in name.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise ManifestError("plugin_manager.zip.traversal")
    return parts


def _check_member(info: zipfile.ZipInfo, parts: tuple[str, ...]) -> None:
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
        raise ManifestError("plugin_manager.zip.link")
    if info.flag_bits & 0x1:
        raise ManifestError("plugin_manager.zip.encrypted")
    if parts[-1].lower().endswith(FORBIDDEN_SUFFIXES):
        raise ManifestError("plugin_manager.zip.compiled", name=parts[-1])
    if info.file_size > MAX_FILE_SIZE:
        raise ManifestError("plugin_manager.zip.file_too_large", limit=MAX_FILE_SIZE // MIB)
    if (info.file_size and not info.compress_size) or (
            info.compress_size and info.file_size / info.compress_size > MAX_RATIO):
        raise ManifestError("plugin_manager.zip.ratio")


def inspect(path: str | Path) -> tuple[Manifest, tuple[str, ...], list[Member]]:
    """Validate an archive; return its manifest, the folder prefix and the members to unpack."""
    path = Path(path)
    if path.stat().st_size > MAX_ARCHIVE_BYTES or not zipfile.is_zipfile(path):
        raise ManifestError("plugin_manager.zip.not_zip")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise ManifestError("plugin_manager.zip.too_many_files", limit=MAX_FILES)
            members: list[Member] = []
            seen: set[str] = set()
            total = 0
            for info in infos:
                parts = _member_parts(info)
                if not parts:
                    if info.is_dir():
                        continue
                    raise ManifestError("plugin_manager.zip.bad_path")
                folded = "/".join(parts).casefold()
                if folded in seen:
                    raise ManifestError("plugin_manager.zip.duplicate")
                seen.add(folded)
                _check_member(info, parts)
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    raise ManifestError("plugin_manager.zip.too_large", limit=MAX_UNCOMPRESSED // MIB)
                members.append((info, parts))
            manifests = [(i, p) for i, p in members if p[-1] == "plugin.json" and len(p) <= 2 and not i.is_dir()]
            if len(manifests) != 1:
                raise ManifestError("plugin_manager.zip.manifest_count")
            manifest_info, manifest_parts = manifests[0]
            prefix = manifest_parts[:-1]
            if any(p[:len(prefix)] != prefix or len(p) == len(prefix) and not i.is_dir() for i, p in members):
                raise ManifestError("plugin_manager.zip.one_folder")
            if not any(p == (*prefix, "__init__.py") for _i, p in members):
                raise ManifestError("plugins.error.no_package")
            try:
                data = safe_json_loads(archive.read(manifest_info).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise ManifestError("plugins.error.manifest_invalid_json") from error
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError) as error:
        raise ManifestError("plugin_manager.zip.not_zip") from error
    return plugins_external.parse_manifest(data), prefix, members


def _check_new_id(manifest: Manifest, rt: plugins_external.Runtime) -> None:
    folded = manifest.id.casefold()
    if rt.denied(manifest.id):
        raise ManifestError("plugins.error.denylisted")
    if folded in plugins_external.reserved_ids(registry.registry().features):
        raise ManifestError("plugins.error.id_reserved", id=manifest.id)
    taken = {pid.casefold() for pid in db.column("SELECT id FROM plugins")}
    if rt.directory.is_dir():
        taken |= {entry.name.casefold() for entry in rt.directory.iterdir()}
    if folded in taken:
        raise ManifestError("plugin_manager.zip.id_taken", id=manifest.id)


def _extract(archive_path: Path, prefix: tuple[str, ...], members: list[Member], target: Path) -> None:
    written = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info, parts in members:
            relative = parts[len(prefix):]
            if not relative:
                continue
            destination = target.joinpath(*relative)
            if target.resolve() not in destination.resolve().parents:
                raise ManifestError("plugin_manager.zip.traversal")
            if info.is_dir():
                destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("xb") as sink:
                while chunk := source.read(65536):
                    written += len(chunk)
                    if written > MAX_UNCOMPRESSED:
                        raise ManifestError("plugin_manager.zip.too_large", limit=MAX_UNCOMPRESSED // MIB)
                    sink.write(chunk)
            destination.chmod(0o644)


def install(archive_path: str | Path) -> Manifest:
    """Unpack a validated archive and register the plugin, disabled."""
    rt = plugins_external.runtime()
    if not rt.allowed:
        raise ManifestError("plugin_manager.lock.external_off")
    archive_path = Path(archive_path)
    manifest, prefix, members = inspect(archive_path)
    _check_new_id(manifest, rt)
    rt.directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = rt.directory / manifest.id
    staging = Path(tempfile.mkdtemp(prefix=f"{plugins_external.STAGING_PREFIX}{manifest.id}-", dir=rt.directory))
    registered = False
    try:
        _extract(archive_path, prefix, members, staging)
        cursor = db.execute(
            "INSERT OR IGNORE INTO plugins (id, name, version, author, description, builtin, enabled, installed_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
            (manifest.id, manifest.name, manifest.version, manifest.author, manifest.description, now_sql()),
        )
        registered = cursor.rowcount == 1
        if not registered or target.exists():
            raise ManifestError("plugin_manager.zip.id_taken", id=manifest.id)
        staging.chmod(0o755)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if registered:
            db.execute("DELETE FROM plugins WHERE id = ? AND builtin = 0", (manifest.id,))
        raise
    rt.plugins[manifest.id] = plugins_external.PluginState(id=manifest.id, path=target, manifest=manifest)
    return manifest


# ── Removal ──────────────────────────────────────────────────────────────────


def _owner(table: str, known: set[str]) -> str | None:
    """The id with the longest ``<id>__`` prefix that *table* starts with."""
    owners = [pid for pid in known if table.startswith(f"{pid}__")]
    return max(owners, key=len) if owners else None


def data_tables(plugin_id: str) -> list[str]:
    """Tables that belong to *plugin_id*: named exactly ``<plugin_id>__…`` (case included)."""
    known = set(db.column("SELECT id FROM plugins")) | set(registry.registry().features) | {plugin_id}
    known |= plugins_external.RESERVED_NAMES | plugins_external.LEGACY_BUILTIN_IDS
    tables = db.column("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    return [name for name in tables if _owner(name, known) == plugin_id]


def uninstall(plugin_id: str, drop: list[str]) -> list[str]:
    """Delete the plugin's folder and row, and drop the listed tables that still belong to it."""
    rt = plugins_external.runtime()
    state = rt.plugins.get(plugin_id) or rt.invalid.get(plugin_id)
    dropped = [name for name in data_tables(plugin_id) if name in set(drop)]
    with db.transaction():
        for name in dropped:
            db.execute('DROP TABLE IF EXISTS "{}"'.format(name.replace('"', '""')))
        db.execute("DELETE FROM plugins WHERE id = ? AND builtin = 0", (plugin_id,))
    if state is not None and state.path.is_dir() and not state.path.is_symlink() \
            and state.path.parent.resolve() == rt.directory.resolve():
        shutil.rmtree(state.path)
    rt.plugins.pop(plugin_id, None)
    rt.invalid.pop(plugin_id, None)
    return dropped


def sdk_bundle() -> bytes:
    """A starter kit: the authoring guide and the example plugin."""
    import io

    root = Path(__file__).parent
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(root / "PLUGINS.md", "bananawiki-plugin-kit/PLUGINS.md")
        example = root / "examples" / "hello_plugin"
        for path in sorted(example.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, f"bananawiki-plugin-kit/hello_plugin/{path.relative_to(example).as_posix()}")
    return buffer.getvalue()
