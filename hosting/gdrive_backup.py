"""
BananaWiki Hosting Platform -- Google Drive Backup Module

Automated nightly backups of the hosting platform to Google Drive.

Features:
  - Nightly backup at 3:00 AM (configurable via hosting_settings)
  - 7-day rolling retention (copies older than 7 days are auto-deleted)
  - Manual backup trigger from the admin settings panel
  - Service account authentication via JSON credentials file

Configuration is managed via Admin -> Settings in the hosting portal.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time

from .db._connection import get_hosting_db_context
from .db._settings import get_hosting_settings

logger = logging.getLogger("hosting.gdrive_backup")

# Constants

CREDENTIALS_PATH_DEFAULT = os.path.join(
    os.path.dirname(__file__), "data", ".gdrive_credentials.json"
)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_BACKUP_MIME_TYPE = "application/octet-stream"
_BACKUP_FILENAME_PREFIX = "bananawiki_backup_"

# Background scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()

# Settings helpers


def _read_settings() -> dict:
    """Return Google Drive backup settings with safe defaults.

    Reads from the ``hosting_settings`` table and returns a plain dict
    with all gdrive-related keys populated (using defaults when the
    column is missing or ``NULL``).
    """
    defaults = {
        "gdrive_backup_enabled": 0,
        "gdrive_credentials_path": CREDENTIALS_PATH_DEFAULT,
        "gdrive_folder_id": "",
        "gdrive_retention_days": 7,
        "gdrive_backup_time": "03:00",
        "last_gdrive_backup_at": 0.0,
    }
    try:
        row = get_hosting_settings()
    except Exception:
        logger.debug("Could not read hosting_settings; using defaults")
        return defaults

    if row is None:
        return defaults

    result = {}
    for key, default in defaults.items():
        try:
            value = row[key]
        except (KeyError, IndexError):
            value = None
        if value is None:
            result[key] = default
        else:
            # Coerce to the same type as the default value.
            try:
                result[key] = type(default)(value)
            except (TypeError, ValueError):
                result[key] = default
    return result


def _update_last_backup_timestamp() -> None:
    """Persist the current Unix timestamp as last_gdrive_backup_at."""
    try:
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE hosting_settings SET last_gdrive_backup_at = ? WHERE id = 1",
                (time.time(),),
            )
            conn.commit()
    except Exception:
        logger.debug("Could not update last_gdrive_backup_at", exc_info=True)


# TTS cache cleanup (mirrors hosting.instance_manager._drop_tts_cache_rows)


def _drop_tts_cache_rows_from_backup(db_path: str) -> None:
    """Remove disposable TTS cache metadata from a copied SQLite DB.

    This is the same operation performed by the instance export pipeline
    (``instance_manager._drop_tts_cache_rows``) but duplicated here to
    avoid a heavy circular import of the full instance_manager module.
    """
    if not db_path or not os.path.isfile(db_path):
        return
    try:
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
    except Exception:
        logger.debug(
            "Could not clean TTS cache from %s", db_path, exc_info=True
        )


# Google Drive API helpers


def _get_drive_service():
    """Create an authenticated Google Drive API service.

    Uses the service account JSON credentials file whose path is stored
    in ``gdrive_credentials_path`` (hosting_settings).

    Returns:
        A ``googleapiclient.discovery.Resource`` for the Drive v3 API.

    Raises:
        FileNotFoundError: If the credentials file does not exist.
        Exception: On authentication or service-build failures.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    settings = _read_settings()
    creds_path = settings["gdrive_credentials_path"]

    if not os.path.isfile(creds_path):
        raise FileNotFoundError(
            f"Service account credentials not found: {creds_path}"
        )

    credentials = service_account.Credentials.from_service_account_file(
        creds_path, scopes=_SCOPES
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return service


# Public API


def is_configured() -> bool:
    """Check whether Google Drive backup is properly configured.

    Returns ``True`` when all three conditions are met:

    1. ``gdrive_backup_enabled`` is turned on (``1``).
    2. The service account credentials file exists on disk.
    3. A Google Drive folder ID is set (non-empty string).
    """
    try:
        settings = _read_settings()
    except Exception:
        return False

    if not settings.get("gdrive_backup_enabled"):
        return False

    creds_path = settings.get("gdrive_credentials_path", "")
    if not creds_path or not os.path.isfile(creds_path):
        return False

    folder_id = settings.get("gdrive_folder_id", "")
    if not folder_id or not folder_id.strip():
        return False

    return True


def test_connection() -> tuple[bool, str]:
    """Test the Google Drive API connection using stored credentials.

    Attempts to read the metadata of the configured backup folder.

    Returns:
        ``(True, "Connection successful")`` on success, or
        ``(False, error_description)`` on failure.
    """
    try:
        settings = _read_settings()

        if not settings.get("gdrive_backup_enabled"):
            return (False, "Google Drive backup is not enabled")

        folder_id = settings.get("gdrive_folder_id", "").strip()
        if not folder_id:
            return (False, "No Google Drive folder ID configured")

        service = _get_drive_service()

        # Verify we can access the target folder.
        folder_meta = (
            service.files()
            .get(fileId=folder_id, fields="id, name, mimeType")
            .execute()
        )
        name = folder_meta.get("name", folder_id)
        return (True, f"Connection successful. Folder: {name}")

    except FileNotFoundError as exc:
        return (False, str(exc))
    except Exception as exc:
        logger.error("Google Drive connection test failed", exc_info=True)
        return (False, f"Connection failed: {exc}")


def create_backup(reason: str = "scheduled") -> tuple[bool, str]:
    """Create a ZIP backup of the platform and upload it to Google Drive.

    The archive includes:
      - ``hosting.db`` (consistent copy via ``VACUUM INTO``)
      - The ``secret_key`` file
      - All instance data directories (each instance's database + assets)

    Args:
        reason: A short label stored in the log (e.g. ``"scheduled"``,
            ``"manual"``).

    Returns:
        ``(True, file_name)`` on success, or ``(False, error_message)``
        on failure.
    """
    if not is_configured():
        return (False, "Google Drive backup is not configured")

    settings = _read_settings()
    folder_id = settings["gdrive_folder_id"].strip()

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    zip_name = f"{_BACKUP_FILENAME_PREFIX}{timestamp}.zip"

    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="bw_gdrive_backup_")
        zip_path = os.path.join(tmp_dir, zip_name)

        logger.info(
            "Creating Google Drive backup (%s): %s", reason, zip_name
        )

        from .backups import write_backup
        from .backup_crypto import encrypt_backup
        write_backup(zip_path)
        encrypted_path = encrypt_backup(zip_path)
        os.unlink(zip_path)
        zip_path = encrypted_path
        zip_name += ".bwenc"

        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        logger.info(
            "Backup archive ready: %s (%.1f MB)", zip_name, zip_size_mb
        )

        # Upload to Google Drive ----
        from googleapiclient.http import MediaFileUpload

        service = _get_drive_service()
        file_metadata = {
            "name": zip_name,
            "parents": [folder_id],
            "description": f"BananaWiki hosting backup ({reason})",
        }
        media = MediaFileUpload(
            zip_path, mimetype=_BACKUP_MIME_TYPE, resumable=True
        )

        request = service.files().create(
            body=file_metadata, media_body=media, fields="id, name, size"
        )

        # Execute the resumable upload.
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.debug(
                    "Upload progress: %.0f%%", status.progress() * 100
                )

        uploaded_name = response.get("name", zip_name)
        logger.info(
            "Backup uploaded to Google Drive: %s (id=%s)",
            uploaded_name,
            response.get("id", "?"),
        )

        _update_last_backup_timestamp()
        return (True, uploaded_name)

    except Exception as exc:
        logger.error(
            "Google Drive backup failed (%s): %s", reason, exc, exc_info=True
        )
        return (False, f"Backup failed: {exc}")

    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def cleanup_old_backups(retention_days: int = 7) -> int:
    """Delete backups older than *retention_days* from Google Drive.

    Only files whose name starts with the backup prefix
    (``bananawiki_backup_``) inside the configured folder are
    considered.

    Returns:
        The number of files deleted.
    """
    if not is_configured():
        return 0

    settings = _read_settings()
    folder_id = settings["gdrive_folder_id"].strip()

    cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        days=retention_days
    )
    cutoff_rfc3339 = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    deleted = 0
    try:
        service = _get_drive_service()
        page_token = None

        while True:
            query = (
                f"'{folder_id}' in parents"
                f" and name contains '{_BACKUP_FILENAME_PREFIX}'"
                f" and createdTime < '{cutoff_rfc3339}'"
                f" and trashed = false"
            )
            resp = (
                service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, createdTime)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )

            for f in resp.get("files", []):
                try:
                    service.files().delete(fileId=f["id"]).execute()
                    deleted += 1
                    logger.info(
                        "Deleted old backup: %s (%s)", f["name"], f["id"]
                    )
                except Exception:
                    logger.warning(
                        "Failed to delete old backup: %s (%s)",
                        f.get("name"),
                        f.get("id"),
                        exc_info=True,
                    )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except Exception:
        logger.error(
            "Error during old backup cleanup", exc_info=True
        )

    if deleted:
        logger.info("Cleaned up %d old backup(s)", deleted)
    return deleted


def list_backups() -> list[dict]:
    """List all backups in the configured Google Drive folder.

    Returns:
        A list of dicts with keys ``name``, ``id``, ``size``, and
        ``created_time``.  Returns an empty list on error or when
        backups are not configured.
    """
    if not is_configured():
        return []

    settings = _read_settings()
    folder_id = settings["gdrive_folder_id"].strip()

    results: list[dict] = []
    try:
        service = _get_drive_service()
        page_token = None

        while True:
            query = (
                f"'{folder_id}' in parents"
                f" and name contains '{_BACKUP_FILENAME_PREFIX}'"
                f" and trashed = false"
            )
            resp = (
                service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, size, createdTime)",
                    pageSize=100,
                    orderBy="createdTime desc",
                    pageToken=page_token,
                )
                .execute()
            )

            for f in resp.get("files", []):
                results.append(
                    {
                        "name": f.get("name", ""),
                        "id": f.get("id", ""),
                        "size": int(f.get("size", 0)),
                        "created_time": f.get("createdTime", ""),
                    }
                )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except Exception:
        logger.error("Failed to list Google Drive backups", exc_info=True)

    return results


# Scheduler


def _seconds_until(target_hour: int, target_minute: int) -> float:
    """Return the number of seconds from now until the next occurrence of
    *target_hour*:*target_minute* (local time).

    If the target time has already passed today, the next occurrence is
    tomorrow.
    """
    now = datetime.datetime.now()
    target = now.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def _parse_backup_time(time_str: str) -> tuple[int, int]:
    """Parse an ``"HH:MM"`` string into ``(hour, minute)``.

    Falls back to ``(3, 0)`` on any parse error.
    """
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
    except (ValueError, IndexError):
        pass
    return (3, 0)


def _run_scheduled_backup() -> None:
    """Execute a scheduled backup, clean up old copies, then reschedule."""
    logger.info("Starting scheduled Google Drive backup")
    try:
        ok, msg = create_backup(reason="scheduled")
        if ok:
            logger.info("Scheduled backup completed: %s", msg)
        else:
            logger.error("Scheduled backup failed: %s", msg)
    except Exception:
        logger.error("Unhandled error in scheduled backup", exc_info=True)

    # Retention cleanup
    try:
        settings = _read_settings()
        retention = settings.get("gdrive_retention_days", 7)
        cleaned = cleanup_old_backups(retention_days=retention)
        if cleaned:
            logger.info(
                "Post-backup cleanup removed %d old backup(s)", cleaned
            )
    except Exception:
        logger.error("Error during post-backup cleanup", exc_info=True)

    # Schedule the next run.
    _schedule_next_run()


def _schedule_next_run() -> None:
    """Schedule the next nightly backup timer."""
    global _scheduler_timer

    settings = _read_settings()
    hour, minute = _parse_backup_time(settings.get("gdrive_backup_time", "03:00"))
    delay = _seconds_until(hour, minute)

    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
        _scheduler_timer = threading.Timer(delay, _run_scheduled_backup)
        _scheduler_timer.daemon = True
        _scheduler_timer.start()

    next_run = datetime.datetime.now() + datetime.timedelta(seconds=delay)
    logger.info(
        "Next Google Drive backup scheduled at %s (in %.0f seconds)",
        next_run.strftime("%Y-%m-%d %H:%M:%S"),
        delay,
    )


def start_backup_scheduler() -> None:
    """Start the nightly Google Drive backup scheduler.

    Launches a daemon background thread that fires at the configured
    backup time (default 3:00 AM local) every day.  After each backup
    completes, ``cleanup_old_backups`` is called to enforce the rolling
    retention window.

    If Google Drive backup is not configured, the scheduler still starts
    but will log a warning and skip the actual upload when it fires.  This
    allows the scheduler to pick up configuration changes made after
    application startup without a restart.
    """
    if not is_configured():
        logger.info(
            "Google Drive backup is not configured; "
            "scheduler will start but backups will be skipped until configured"
        )

    _schedule_next_run()
    logger.info("Google Drive backup scheduler started")
