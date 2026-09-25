"""Validate imported archives and create portable hosted wiki exports."""

import json
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
import archive_format  # noqa: E402  (import after sys.path tweak)
from . import config
from .db import (
    get_instance,
)
from .instance_paths import (
    _instance_dir_for,
    _original_subdomain_from_archived,
)
from .instance_environment import (
    iter_tenant_files,
    open_tenant_dir,
    snapshot_tenant_db,
)

logger = logging.getLogger(__name__)


_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES = 128 * 1024 * 1024


def _hosting_import_work_dir():
    """Return the temp root used for import staging and reconstruction."""
    root = getattr(
        config,
        "HOSTING_IMPORT_TEMP_DIR",
        os.path.join(tempfile.gettempdir(), "bananawiki-hosting-imports"),
    )
    os.makedirs(root, exist_ok=True)
    return root


def _hosting_import_limit(name, default, *, minimum=1):
    try:
        value = int(getattr(config, name, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), value)


def _zip_member_is_symlink(info):
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _validate_import_zip_members(zf, *, staging_parent, data_parent):
    """Validate a hosting import ZIP before extracting it to disk."""
    infos = zf.infolist()
    max_members = _hosting_import_limit(
        "HOSTING_IMPORT_MAX_MEMBERS",
        250000,
    )
    if len(infos) > max_members:
        return None, f"Archive contains too many files ({max_members} limit)."

    max_extracted = _hosting_import_limit(
        "HOSTING_IMPORT_MAX_EXTRACTED_BYTES",
        getattr(config, "HOSTING_IMPORT_MAX_BYTES", 10 * 1024 * 1024 * 1024) * 3,
    )
    min_free = _hosting_import_limit(
        "HOSTING_IMPORT_MIN_FREE_BYTES",
        512 * 1024 * 1024,
        minimum=0,
    )
    total_size = 0
    seen = set()
    for info in infos:
        name = info.filename
        norm = os.path.normpath(name)
        if (
            not name
            or norm in ("", ".")
            or norm.startswith("..")
            or os.path.isabs(norm)
        ):
            return None, "Archive contains unsafe paths."
        if _zip_member_is_symlink(info):
            return None, "Archive contains symbolic links, which are not allowed."
        key = norm.rstrip(os.sep)
        if key in seen and not info.is_dir():
            return None, "Archive contains duplicate file paths."
        seen.add(key)
        if info.file_size < 0:
            return None, "Archive contains invalid file sizes."
        total_size += int(info.file_size)
        if total_size > max_extracted:
            max_gb = max_extracted / (1024 * 1024 * 1024)
            return None, (
                f"Archive expands beyond the configured {max_gb:.1f} GB import limit."
            )

    try:
        staging_dev = os.stat(staging_parent).st_dev
        data_dev = os.stat(data_parent).st_dev
        staging_free = shutil.disk_usage(staging_parent).free
        data_free = shutil.disk_usage(data_parent).free
    except OSError:
        return None, "Could not check available disk space for the import."

    if staging_dev == data_dev:
        required = (total_size * 2) + min_free
        if staging_free < required:
            return None, "Not enough free disk space to safely import this archive."
    else:
        if staging_free < total_size + min_free:
            return None, "Not enough free disk space to extract this archive."
        if data_free < total_size + min_free:
            return None, "Not enough free disk space to seed the imported wiki."

    return total_size, None


def _drop_tts_cache_rows(db_path):
    """Remove disposable TTS cache metadata from a copied SQLite DB."""
    if not db_path or not os.path.isfile(db_path):
        return
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "tts_generations" in tables:
            conn.execute("DELETE FROM tts_generations")
            conn.commit()
    finally:
        conn.close()


def _hosting_export_compress_level():
    try:
        level = int(getattr(config, "HOSTING_EXPORT_COMPRESS_LEVEL", 1))
    except (TypeError, ValueError):
        level = 1
    return max(0, min(level, 9))


def _hosting_export_store_threshold_bytes():
    try:
        threshold = int(
            getattr(config, "HOSTING_EXPORT_STORE_FILE_BYTES", 64 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        threshold = 64 * 1024 * 1024
    return max(0, threshold)


def _hosting_export_store_extensions():
    raw = getattr(config, "HOSTING_EXPORT_STORE_EXTENSIONS", "")
    if not raw:
        return frozenset()
    return frozenset(
        ext.strip().lower()
        for ext in str(raw).split(",")
        if ext.strip().startswith(".")
    )


def _archive_compress_type(path):
    """Return a ZIP compression type for *path* tuned for large exports."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    ext = os.path.splitext(path)[1].lower()
    if ext in _hosting_export_store_extensions():
        return zipfile.ZIP_STORED
    threshold = _hosting_export_store_threshold_bytes()
    if threshold > 0 and size >= threshold:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _archive_write_file(zf, path, arcname):
    compress_type = _archive_compress_type(path)
    compresslevel = (
        None
        if compress_type == zipfile.ZIP_STORED
        else _hosting_export_compress_level()
    )
    zf.write(
        path,
        arcname,
        compress_type=compress_type,
        compresslevel=compresslevel,
    )


def _zip_date_time(mtime):
    """Return *mtime* as a ZIP timestamp, clamped to the years ZIP can store.

    The tenant sets its own file times, and zipfile refuses a year before
    1980 and cannot encode one after 2107, which would otherwise fail the
    whole export.
    """
    try:
        stamp = datetime.fromtimestamp(mtime).timetuple()[:6]
    except (OverflowError, OSError, ValueError):
        stamp = (1980, 1, 1, 0, 0, 0)
    return min(max(stamp, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))


def _archive_write_fd(zf, fd, st, arcname):
    """Add an already opened tenant file to *zf* under *arcname*.

    ``zipfile.write`` takes a path and follows a link found there, so tenant
    files are opened by :func:`iter_tenant_files` and streamed from the
    descriptor instead.  Uses the same compression rules as
    :func:`_archive_write_file`.  Takes ownership of *fd*.
    """
    with os.fdopen(fd, "rb") as src:
        ext = os.path.splitext(arcname)[1].lower()
        threshold = _hosting_export_store_threshold_bytes()
        if ext in _hosting_export_store_extensions() or (
            threshold > 0 and st.st_size >= threshold
        ):
            compress_type = zipfile.ZIP_STORED
        else:
            compress_type = zipfile.ZIP_DEFLATED
        info = zipfile.ZipInfo(arcname, date_time=_zip_date_time(st.st_mtime))
        info.external_attr = (stat.S_IMODE(st.st_mode) | stat.S_IFREG) << 16
        info.compress_type = compress_type
        if compress_type != zipfile.ZIP_STORED:
            # The attribute is compress_level from Python 3.13; 3.12 only
            # has the older _compresslevel name.
            if hasattr(zipfile.ZipInfo, "compress_level"):
                info.compress_level = _hosting_export_compress_level()
            else:
                info._compresslevel = _hosting_export_compress_level()
        # Lets zipfile pick ZIP64 up front for large files.
        info.file_size = st.st_size
        with zf.open(info, "w") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)


def build_instance_archive(instance_id, output_dir):
    """Create a unified ZIP archive of an instance's data directory.

    Works for instances in any status (``running``, ``stopped`` or
    ``terminated``).  For ``running`` instances the live SQLite database
    is copied via SQLite's online backup API to avoid capturing a partial
    write.

    The archive follows the unified BananaWiki archive format (see
    :mod:`archive_format`): a ``manifest.json`` at the root, the raw
    ``bananawiki.db`` SQLite snapshot, a ``site_export.json`` logical
    dump derived from the same snapshot, and flat asset directories
    (``uploads/``, ``attachments/``, …) at the root.  This means the
    archive can be imported by either the hosting platform *or* a
    standalone BananaWiki install's Site Migration page.

    Returns ``(archive_path, filename, None)`` on success or
    ``(None, None, reason)`` on failure.  The caller owns the archive
    file and is responsible for cleaning it up after streaming it to
    the client.
    """
    import zipfile

    inst = get_instance(instance_id)
    if inst is None:
        return None, None, "Instance not found."

    data_dir = _instance_dir_for(inst)
    if not os.path.isdir(data_dir):
        return None, None, "No data is available for this instance."

    original = _original_subdomain_from_archived(inst["subdomain"]) or inst["subdomain"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_slug = "".join(c for c in original if c.isalnum() or c in "-_") or "instance"
    filename = f"bananawiki-{safe_slug}-{timestamp}.zip"

    os.makedirs(output_dir, exist_ok=True)
    archive_path = os.path.join(output_dir, filename)

    # Snapshot the SQLite DB consistently via the online backup API so
    # the archive is restorable.  For ``terminated`` instances the live
    # data dir is already quiesced and a plain copy would do, but using
    # the same path for both keeps the layout uniform.  The data dir is
    # tenant-writable and the tenant may be running, so the copy goes through
    # snapshot_tenant_db, which refuses a planted link even mid-copy.
    consistent_snapshot = None
    live_db = os.path.join(data_dir, "bananawiki.db")
    if os.path.lexists(live_db):
        snap_path = None
        try:
            snap_fd, snap_path = tempfile.mkstemp(
                prefix="bwh-snap-",
                suffix=".db",
                dir=output_dir,
            )
            os.close(snap_fd)
            os.unlink(snap_path)
            snapshot_tenant_db(data_dir, live_db, snap_path)
            _drop_tts_cache_rows(snap_path)
            consistent_snapshot = snap_path
        except Exception:
            logger.exception(
                "Failed to create a consistent SQLite snapshot for instance %s",
                instance_id,
            )
            if snap_path:
                try:
                    os.unlink(snap_path)
                except OSError:
                    pass
            return None, None, "Could not verify a consistent database snapshot. No archive was created; the instance data is preserved."
    else:
        return None, None, "The instance database is missing. Preserve its files and restore a verified backup."

    # Derive ``site_export.json`` from the snapshot so the archive can
    # also be imported via the standalone wiki's Site Migration page.
    site_export_bytes = None
    has_site_export_json = False
    try:
        from db import export_site_data as _db_export_site_data

        snapshot_for_json = consistent_snapshot
        if snapshot_for_json:
            snapshot_size = os.path.getsize(snapshot_for_json)
            if snapshot_size <= _SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES:
                payload = _db_export_site_data(db_path=snapshot_for_json)
                site_export_bytes = json.dumps(
                    payload,
                    separators=(",", ":"),
                ).encode("utf-8")
                has_site_export_json = True
            else:
                logger.info(
                    "Skipping site_export.json for instance %s because DB snapshot "
                    "is %.1f MiB; raw SQLite DB remains included in the archive.",
                    instance_id,
                    snapshot_size / (1024 * 1024),
                )
    except Exception:
        logger.exception(
            "Failed to derive site_export.json for instance %s; archive will still "
            "be importable on the hosting platform via the raw SQLite DB.",
            instance_id,
        )

    manifest = archive_format.build_manifest(
        source=archive_format.SOURCE_HOSTING,
        original_subdomain=original,
        has_raw_db=consistent_snapshot is not None,
        has_site_export_json=has_site_export_json,
    )

    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=_hosting_export_compress_level(),
            allowZip64=True,
        ) as zf:
            # 1. Manifest at the archive root.
            archive_format.write_manifest_to_zip(zf, manifest)

            # 2. Raw SQLite snapshot at the archive root.  Never the live
            #    file: only the checked snapshot taken above.
            if consistent_snapshot:
                try:
                    _archive_write_file(
                        zf,
                        consistent_snapshot,
                        archive_format.RAW_DB_FILENAME,
                    )
                except OSError:
                    logger.exception(
                        "Failed to write snapshot DB to archive for %s",
                        instance_id,
                    )

            # 3. Logical JSON dump at the archive root.
            if site_export_bytes is not None:
                zf.writestr(
                    archive_format.SITE_EXPORT_FILENAME,
                    site_export_bytes,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=_hosting_export_compress_level(),
                )

            # 4. Everything else from the data dir, with secrets and
            #    volatile / sidecar files filtered out.  Asset dirs end
            #    up flat under the archive root (``uploads/foo.png``,
            #    not ``<slug>/uploads/foo.png``).
            #
            #    The data dir is tenant-writable and the archive goes back
            #    to the tenant, so a link followed here could put the
            #    platform database, the portal's keys or another tenant's
            #    files into it.  iter_tenant_files walks with directory
            #    descriptors, does not enter linked directories, opens each
            #    file with O_NOFOLLOW and yields regular files only.
            root_fd = open_tenant_dir(data_dir, "")
            try:
                for arcname, fd, st in iter_tenant_files(root_fd, skip_hidden_and_tts=True):
                    fname = os.path.basename(arcname)
                    # Skip volatile runtime files, secrets, and the
                    # raw DB / sidecars (already handled above).
                    if (
                        fname in archive_format.VOLATILE_FILENAMES
                        or fname in archive_format.SECRET_FILENAMES
                        or arcname == archive_format.RAW_DB_FILENAME
                    ):
                        os.close(fd)
                        continue
                    try:
                        _archive_write_fd(zf, fd, st, arcname)
                    except OSError:
                        logger.exception(
                            "Skipping unreadable file %s while building archive for %s",
                            arcname,
                            instance_id,
                        )
            finally:
                os.close(root_fd)
    except Exception:
        logger.exception("Failed to build archive for instance %s", instance_id)
        try:
            os.unlink(archive_path)
        except OSError:
            pass
        if consistent_snapshot:
            try:
                os.unlink(consistent_snapshot)
            except OSError:
                pass
        return None, None, "Failed to build the archive."
    finally:
        if consistent_snapshot:
            try:
                os.unlink(consistent_snapshot)
            except OSError:
                pass

    return archive_path, filename, None
