"""Conversation exports: a plain-text transcript, zipped with the attachments when there are any.

Everything is written to a temporary file (never built in memory), read from
the database in batches. Deleted messages appear as a placeholder. Sender IP
addresses are moderation data and are only included for administrators.
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from typing import IO, Any

from flask import Response, send_file

from ....core.timeutil import utcnow
from ... import storage
from ...i18n import t
from ...templating import format_datetime
from . import store
from .store import Store

ATTACHMENT_LIMIT = 100 * 1024 * 1024
_UNSAFE = re.compile(r"[^\w-]+")


def safe_stem(text: str) -> str:
    return _UNSAFE.sub("_", text or "").strip("_")[:60] or "chat"


def _write_transcript(out: IO[str], target: Store, parent_id: int, header: list[str], *,
                      include_ip: bool) -> list[dict[str, Any]]:
    """Write the transcript; return the attachments it lists."""
    out.write("\n".join(header) + "\n" + "=" * 72 + "\n\n")
    attachments: list[dict[str, Any]] = []
    for row in store.iterate(target, parent_id):
        when = format_datetime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        if row["is_system"]:
            out.write(f"[{when}] * {store.system_text(row['content'])}\n\n")
            continue
        sender = row["sender_name"] or t("common.unknown_user")
        if include_ip:
            sender = f"{sender} ({row['ip_address'] or '-'})"
        out.write(f"[{when}] {sender}\n")
        text = t("chat.message.deleted") if row["is_deleted"] else row["content"]
        out.write("".join(f"  {line}\n" for line in (text or "").splitlines() or [""]))
        for item in row["attachments"]:
            out.write(f"  + {item['original_name']} ({item['file_size']} B)\n")
            attachments.append(item)
        out.write("\n")
    return attachments


def export(target: Store, parent_id: int, *, title: str, header: list[str], include_ip: bool) -> Response:
    """Build the export for one conversation and send it as a download."""
    stamp = utcnow().strftime("%Y%m%d_%H%M%S")
    stem = f"{safe_stem(title)}_{stamp}"
    transcript = tempfile.TemporaryFile()  # noqa: SIM115 - handed to send_file
    text = io.TextIOWrapper(transcript, encoding="utf-8", newline="\n", write_through=True)
    attachments = _write_transcript(text, target, parent_id, header, include_ip=include_ip)
    text.detach()
    transcript.seek(0)
    if not attachments:
        return send_file(transcript, mimetype="text/plain; charset=utf-8", as_attachment=True,
                         download_name=f"{stem}.txt")
    archive = tempfile.TemporaryFile()  # noqa: SIM115 - handed to send_file
    total = sum(int(item["file_size"] or 0) for item in attachments)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        with bundle.open(f"{stem}.txt", "w") as entry:
            while chunk := transcript.read(1024 * 1024):
                entry.write(chunk)
        if total > ATTACHMENT_LIMIT:
            bundle.writestr("README.txt", t("chat.export.too_large", size=total // (1024 * 1024)) + "\n")
        else:
            for item in attachments:
                path = storage.resolve(store.FOLDER, item["filename"], blob_id=item["blob_id"])
                if path is not None:
                    bundle.write(path, f"attachments/{item['id']}_{safe_stem(item['original_name'])}"
                                       f"{_suffix(item['original_name'])}")
    transcript.close()
    archive.seek(0)
    return send_file(archive, mimetype="application/zip", as_attachment=True, download_name=f"{stem}.zip")


def _suffix(name: str) -> str:
    ext = storage.extension(name)
    return f".{ext}" if ext.isalnum() else ""
