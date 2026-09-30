"""Redeeming invite codes at sign-up (codes are created in the administration area)."""

from __future__ import annotations

from typing import Any

from ....core.timeutil import is_past, now_sql
from ...db import db

ASSIGNABLE_ROLES = ("user", "editor", "admin")


def normalize(code: str | None) -> str:
    """Codes are compared case-insensitively and people often add spaces."""
    return "".join((code or "").split()).upper()[:64]


def _candidates(code: str) -> list[str]:
    # Generated codes look like ABCD-1234; accept them typed without the hyphen,
    # while an 8-character custom code such as BANANA42 still matches itself.
    candidates = [code]
    if len(code) == 8 and "-" not in code:
        candidates.append(f"{code[:4]}-{code[4:]}")
    return candidates


def _usable(row: dict[str, Any]) -> bool:
    if row["deleted"] or row["creator_suspended"] is None or row["creator_suspended"]:
        return False
    if row["max_uses"] > 0 and row["use_count"] >= row["max_uses"]:
        return False
    return not (row["expires_at"] and is_past(row["expires_at"]))


def find_valid(code: str | None) -> dict[str, Any] | None:
    """The usable invite matching *code*, or None.

    An invite is usable while it is not deleted, not used up (``max_uses`` 0
    means unlimited), not expired, and its creator still exists and is not
    suspended.
    """
    code = normalize(code)
    if not code:
        return None
    for candidate in _candidates(code):
        row = db.one(
            "SELECT ic.*, u.suspended AS creator_suspended FROM invite_codes ic "
            "LEFT JOIN users u ON u.id = ic.created_by WHERE ic.code = ? COLLATE NOCASE",
            (candidate,),
        )
        if row is not None:
            return row if _usable(row) else None
    return None


def assigned_role(invite: dict[str, Any]) -> tuple[str, int | None]:
    """The (role, custom role id) an invite grants; a custom role wins over a base role."""
    role = invite["assigned_role"] if invite.get("assigned_role") in ASSIGNABLE_ROLES else "user"
    custom_id = invite.get("assigned_custom_role_id")
    if custom_id:
        custom = db.one("SELECT id, base_role FROM custom_roles WHERE id = ?", (custom_id,))
        if custom:
            return custom["base_role"], custom["id"]
    return role, None


def consume(invite: dict[str, Any], user_id: str) -> bool:
    """Count one use of *invite* by *user_id*; False when it was used up meanwhile.

    Call inside the transaction that creates the account.
    """
    changed = db.execute(
        "UPDATE invite_codes SET use_count = use_count + 1 "
        "WHERE id = ? AND deleted = 0 AND (max_uses = 0 OR use_count < max_uses)",
        (invite["id"],),
    ).rowcount
    if changed != 1:
        return False
    db.execute(
        "INSERT INTO invite_code_usage (invite_code_id, user_id, used_at) VALUES (?, ?, ?)",
        (invite["id"], user_id, now_sql()),
    )
    return True
