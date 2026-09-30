"""Invite codes for ``signup_mode = 'invite'`` (``hosting_invite_codes``)."""

from __future__ import annotations

import secrets
import sqlite3
import string
from typing import Any

from ..core.timeutil import is_past, now_sql
from .db import db
from .errors import ServiceError

_ALPHABET = string.ascii_uppercase + string.digits


def create(created_by: str, *, note: str = "", max_uses: int = 1, expires_at: str | None = None,
           code: str | None = None) -> dict[str, Any]:
    if not 1 <= max_uses <= 1000:
        raise ServiceError("hosting.invites.invalid_uses")
    if code:
        code = code.strip().upper()
        if len(code) > 32 or not code.isalnum():
            raise ServiceError("hosting.invites.invalid_code")
    for _attempt in range(5):
        candidate = code or "".join(secrets.choice(_ALPHABET) for _ in range(12))
        try:
            invite_id = db.insert("hosting_invite_codes", {
                "code": candidate, "created_by": created_by, "created_at": now_sql(),
                "note": (note or "").strip()[:200], "max_uses": int(max_uses), "expires_at": expires_at,
            })
        except sqlite3.IntegrityError as error:
            if code:
                raise ServiceError("hosting.invites.code_taken") from error
            continue
        return db.one("SELECT * FROM hosting_invite_codes WHERE id = ?", (invite_id,))  # type: ignore[return-value]
    raise ServiceError("hosting.invites.generate_failed")


def list_all() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM hosting_invite_codes ORDER BY created_at DESC, id DESC")


def delete(invite_id: int) -> bool:
    return db.execute("DELETE FROM hosting_invite_codes WHERE id = ?", (invite_id,)).rowcount > 0


def redeem(code: str, account_id: str) -> None:
    """Consume one use inside the caller's transaction, or raise :class:`ServiceError`."""
    code = (code or "").strip()
    if not code:
        raise ServiceError("hosting.invites.required")
    row = db.one("SELECT * FROM hosting_invite_codes WHERE code = ? COLLATE NOCASE", (code,))
    if row is None:
        raise ServiceError("hosting.invites.invalid")
    if row["expires_at"] and is_past(row["expires_at"]):
        raise ServiceError("hosting.invites.expired")
    used = db.execute(
        "UPDATE hosting_invite_codes SET current_uses = current_uses + 1, last_used_by = ?, last_used_at = ? "
        "WHERE id = ? AND current_uses < max_uses", (account_id, now_sql(), row["id"]),
    ).rowcount
    if used != 1:
        raise ServiceError("hosting.invites.exhausted")
