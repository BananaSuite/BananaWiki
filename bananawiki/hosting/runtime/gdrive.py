"""Encrypted platform backups to Google Drive (1.4 ``hosting/gdrive_backup.py``).

Settings live in ``hosting_settings`` (``gdrive_backup_enabled``,
``gdrive_credentials_path``, ``gdrive_folder_id``, ``gdrive_retention_days``,
``gdrive_backup_time`` as ``HH:MM`` server time). Authentication uses a
service-account JSON file stored with mode 0600. The Google client libraries
are optional: without them every call raises ``not_configured``.

Only ``.bwenc`` files (the encrypted ``BWBACKUP1`` format) are uploaded, and
retention only ever deletes files in the configured folder whose names start
with a backup prefix (1.6 or 1.4).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.runtime.gdrive")

SCOPES = ("https://www.googleapis.com/auth/drive.file",)
# 1.6 names, and the names 1.4 uploaded (retention covers both).
PREFIXES = ("bananawiki_hosting_backup_", "bananawiki_backup_")
MIME_TYPE = "application/octet-stream"
FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
CREDENTIALS_MAX = 64 * 1024
TOKEN_URI = "https://oauth2.googleapis.com/token"
UNIVERSE = "googleapis.com"


@dataclass(frozen=True)
class DriveSettings:
    enabled: bool
    credentials_path: str
    folder_id: str
    retention_days: int
    backup_time: tuple[int, int]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DriveSettings:
        try:
            retention = max(1, min(3650, int(row.get("gdrive_retention_days") or 7)))
        except (TypeError, ValueError):
            retention = 7
        return cls(enabled=bool(row.get("gdrive_backup_enabled")),
                   credentials_path=str(row.get("gdrive_credentials_path") or ""),
                   folder_id=str(row.get("gdrive_folder_id") or "").strip(),
                   retention_days=retention, backup_time=parse_time(str(row.get("gdrive_backup_time") or "")))

    def check(self) -> None:
        if not self.enabled:
            raise RuntimeFailure("not_configured", "Google Drive backups are switched off")
        if not FOLDER_ID.fullmatch(self.folder_id):
            raise RuntimeFailure("not_configured", "no valid Google Drive folder id")
        path = Path(self.credentials_path)
        if not self.credentials_path or path.is_symlink() or not path.is_file():
            raise RuntimeFailure("not_configured", "the service account credentials are missing")


def parse_time(value: str) -> tuple[int, int]:
    """``HH:MM`` (default 03:00)."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if match and int(match[1]) < 24 and int(match[2]) < 60:
        return int(match[1]), int(match[2])
    return 3, 0


def due(settings: DriveSettings, last_run: datetime | None, now: datetime) -> bool:
    """Whether today's scheduled backup (server local time) has not run yet."""
    hour, minute = settings.backup_time
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        scheduled -= timedelta(days=1)
    return last_run is None or last_run < scheduled


def validate_credentials(content: bytes) -> None:
    if len(content) > CREDENTIALS_MAX:
        raise RuntimeFailure("invalid", "the credentials file is too large")
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeFailure("invalid", "the credentials file is not JSON") from None
    if not isinstance(data, dict) or data.get("type") != "service_account" or not all(
        isinstance(data.get(key), str) and data[key] for key in ("client_email", "private_key")
    ):
        raise RuntimeFailure("invalid", "this is not a Google service account key")
    # google-auth posts a signed assertion to the key file's token_uri: a crafted
    # file would make the portal call any address, including internal services.
    if data.get("token_uri", TOKEN_URI) != TOKEN_URI or data.get("universe_domain", UNIVERSE) != UNIVERSE:
        raise RuntimeFailure("invalid", "the credentials file does not use Google's token endpoint")


def store_credentials(content: bytes, path: Path) -> Path:
    validate_credentials(content)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(dir=path.parent, prefix=".gdrive-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(pending, 0o600)
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)
    return path


def service(settings: DriveSettings) -> Any:
    """An authenticated Drive v3 client (the key file is re-checked: 1.4 stored it unchecked)."""
    settings.check()
    with open(settings.credentials_path, "rb") as handle:
        validate_credentials(handle.read(CREDENTIALS_MAX + 1))
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeFailure("not_configured", "google-api-python-client is not installed") from None
    try:
        credentials = service_account.Credentials.from_service_account_file(settings.credentials_path,
                                                                            scopes=list(SCOPES))
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except (OSError, ValueError) as error:
        raise RuntimeFailure("not_configured", f"unusable credentials: {type(error).__name__}") from None


def test(client: Any, settings: DriveSettings) -> str:
    try:
        folder = client.files().get(fileId=settings.folder_id, fields="id, name").execute()
    except Exception as error:  # noqa: BLE001 - googleapiclient raises many error types
        raise RuntimeFailure("failed", f"Google Drive refused the request: {type(error).__name__}") from None
    return str(folder.get("name") or settings.folder_id)


def upload(client: Any, settings: DriveSettings, path: Path) -> str:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), mimetype=MIME_TYPE, resumable=True)
    request = client.files().create(body={"name": path.name, "parents": [settings.folder_id],
                                          "description": "BananaWiki hosting backup (encrypted)"},
                                    media_body=media, fields="id, name")
    try:
        response = None
        while response is None:
            _status, response = request.next_chunk()
    except Exception as error:  # noqa: BLE001
        raise RuntimeFailure("failed", f"the upload failed: {type(error).__name__}") from None
    return str(response.get("name") or path.name)


def prune(client: Any, settings: DriveSettings, now: datetime | None = None) -> int:
    """Delete this platform's backups older than the retention period; returns how many."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.retention_days)
    query = (f"'{settings.folder_id}' in parents and name contains 'bananawiki_' and "
             f"createdTime < '{cutoff.strftime('%Y-%m-%dT%H:%M:%S')}' and trashed = false")
    deleted = 0
    token = None
    try:
        while True:
            page = client.files().list(q=query, fields="nextPageToken, files(id, name)", pageSize=100,
                                       pageToken=token).execute()
            for item in page.get("files", []):
                if str(item.get("name", "")).startswith(PREFIXES):
                    client.files().delete(fileId=item["id"]).execute()
                    deleted += 1
            token = page.get("nextPageToken")
            if not token:
                return deleted
    except Exception as error:  # noqa: BLE001
        log.warning("Pruning old Google Drive backups failed: %s", type(error).__name__)
        return deleted
