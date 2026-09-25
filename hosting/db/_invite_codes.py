"""Invite-code CRUD for hosting platform signup gating.

Invite codes are managed by hosting admins and are required during
signup when :func:`get_signup_mode` returns ``'invite'``.

Schema (see ``_schema.py``)::

    hosting_invite_codes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        code         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_by   TEXT NOT NULL,   -- account id of issuing admin
        created_at   TEXT NOT NULL,   -- UTC ISO
        note         TEXT NOT NULL DEFAULT '',
        max_uses     INTEGER NOT NULL DEFAULT 1,
        current_uses INTEGER NOT NULL DEFAULT 0,
        expires_at   TEXT,            -- UTC ISO or NULL for never
        last_used_by TEXT,            -- account id of latest redeemer
        last_used_at TEXT             -- UTC ISO of latest redemption
    )

The redemption helper :func:`redeem_invite_code` is intentionally
atomic and avoids TOCTOU races by combining the "still has uses left
and not expired" check into a single conditional UPDATE.
"""

import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from ._connection import get_hosting_db_context


_ALPHABET = string.ascii_uppercase + string.digits  # no lowercase or O/0/1/I ambiguity


def _generate_code(length: int = 12) -> str:
    """Generate a random uppercase alphanumeric invite code."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _normalize_custom_code(code: str) -> str:
    candidate = (code or "").strip().upper()
    if not candidate or len(candidate) > 32 or not candidate.isalnum():
        raise ValueError("custom invite code must be 1-32 alphanumeric characters")
    return candidate


def create_invite_code(
    *,
    created_by: str,
    note: str = "",
    max_uses: int = 1,
    expires_at: Optional[str] = None,
    code: Optional[str] = None,
) -> dict:
    """Insert a new invite code and return it as a plain dict.

    ``code`` is generated when not supplied; explicit codes are
    normalised to upper-case so the unique-COLLATE-NOCASE index still
    treats user input consistently.  Re-issues on collision up to a
    few attempts before giving up.
    """
    if max_uses < 1:
        raise ValueError("max_uses must be at least 1")

    note = (note or "").strip()
    now = datetime.now(timezone.utc).isoformat()

    with get_hosting_db_context() as conn:
        for _ in range(5):
            candidate = _normalize_custom_code(code) if code is not None else _generate_code()
            try:
                cur = conn.execute(
                    "INSERT INTO hosting_invite_codes "
                    "(code, created_by, created_at, note, max_uses, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (candidate, created_by, now, note, int(max_uses), expires_at),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM hosting_invite_codes WHERE id=?",
                    (cur.lastrowid,),
                ).fetchone()
                return dict(row)
            except Exception:
                if code is not None:
                    # Explicit code collided: propagate, do not silently
                    # rewrite the admin's choice.
                    raise
                # Auto-generated collision: retry with a fresh code.
                continue
        raise RuntimeError("Failed to generate a unique invite code after 5 attempts")


def list_invite_codes(*, include_exhausted: bool = True) -> list:
    """Return all invite codes ordered by creation time (newest first)."""
    with get_hosting_db_context() as conn:
        if include_exhausted:
            rows = conn.execute(
                "SELECT * FROM hosting_invite_codes ORDER BY datetime(created_at) DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hosting_invite_codes "
                "WHERE current_uses < max_uses "
                "ORDER BY datetime(created_at) DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_invite_code_by_code(code: str):
    """Return the invite-code row for ``code`` (case-insensitive)."""
    if not code:
        return None
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM hosting_invite_codes WHERE code = ? COLLATE NOCASE",
            (code.strip(),),
        ).fetchone()


def delete_invite_code(invite_id: int) -> bool:
    """Delete an invite code.  Returns ``True`` when a row was removed."""
    with get_hosting_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM hosting_invite_codes WHERE id = ?", (int(invite_id),)
        )
        conn.commit()
        return cur.rowcount > 0


def _redeem_invite_in_transaction(conn, code, account_id):
    if not code or not code.strip():
        return False, "Invite code is required."
    row = conn.execute(
        "SELECT * FROM hosting_invite_codes WHERE code=? COLLATE NOCASE",
        (code.strip(),),
    ).fetchone()
    if row is None:
        return False, "Invalid invite code."
    now = datetime.now(timezone.utc)
    if row["expires_at"]:
        try:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                return False, "This invite code has expired."
        except (TypeError, ValueError):
            return False, "Invalid invite expiry."
    changed = conn.execute(
        "UPDATE hosting_invite_codes SET current_uses=current_uses+1, "
        "last_used_by=?, last_used_at=? WHERE id=? AND current_uses<max_uses",
        (account_id, now.isoformat(), row["id"]),
    )
    if changed.rowcount != 1:
        return False, "This invite code has been fully used."
    return True, ""


def redeem_invite_code(code: str, *, account_id: str) -> tuple:
    """Validate and consume one invite use in a single write transaction."""
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = _redeem_invite_in_transaction(conn, code, account_id)
        if result[0]:
            conn.commit()
        return result
