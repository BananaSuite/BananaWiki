"""Central binary blob storage: stores file content in the database.

Phase 1 adds DB-resident copies of all attachment types while keeping the
existing on-disk copies for backward compatibility (dual-write). Reads check
the DB blob first and fall back to disk if the blob is absent (e.g. files
uploaded before Phase 1).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def store_blob(filename: str, content: bytes, mime_type: str = "application/octet-stream") -> int:
    """Store binary content in the file_blobs table.

    Returns the new blob row ID.
    """
    file_size = len(content)
    sha256 = hashlib.sha256(content).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO file_blobs (filename, content, mime_type, file_size, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (filename, content, mime_type, file_size, sha256, created_at),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_blob_content(blob_id: int) -> bytes | None:
    """Return the raw bytes for a blob, or None if not found."""
    if not blob_id:
        return None
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT content FROM file_blobs WHERE id=?", (blob_id,)
        ).fetchone()
    if not row:
        return None
    content = row["content"]
    if content is None:
        return None
    return bytes(content)


@retry_on_busy
def get_blob(blob_id: int):
    """Return the full blob metadata row (no content), or None if not found."""
    if not blob_id:
        return None
    with get_db_context() as conn:
        return conn.execute(
            "SELECT id, filename, mime_type, file_size, sha256, created_at "
            "FROM file_blobs WHERE id=?",
            (blob_id,),
        ).fetchone()


def delete_blob(blob_id: int) -> bool:
    """Delete a blob by ID.  Returns True if a row was deleted."""
    if not blob_id:
        return False
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM file_blobs WHERE id=?", (blob_id,))
        conn.commit()
        return cur.rowcount > 0
