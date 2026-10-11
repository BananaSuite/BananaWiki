"""Invite codes: creation, listing and deletion.

Administrators manage every code. Editors who were granted the ``invite.*``
permissions manage only the codes they created, and may only hand out the
``user`` or ``editor`` role (or a custom role, whose base role is at most
``editor``) so an invite can never be used to create an administrator.
Beyond a plain member account, a delegated invite may only grant what the
inviter holds: no permission they lack, no category they cannot read or
write (:func:`within_authority`). Sign-up checks this again against the
creator's rights at that moment.
Redeeming a code at sign-up is the sign-up feature's job; the stored format
(``XXXX-XXXX``, compared case-insensitively) is the one 1.4 used.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bananawiki.core.numbers import ascii_int

from ....core.timeutil import now_sql, to_sql, utcnow
from ... import auth
from ... import permissions as perms
from ...accounts import AccountError
from ...db import db
from ..audit.service import record

CODE_ALPHABET = string.ascii_uppercase + string.digits
CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")
MAX_USES_LIMIT = 10_000
EXPIRY_PRESETS = {"24h": 24, "48h": 48, "72h": 72, "7d": 168, "30d": 720}
ADMIN_ASSIGNABLE = ("user", "editor", "admin")
EDITOR_ASSIGNABLE = ("user", "editor")

_ACTIVE = ("ic.deleted = 0 AND (ic.max_uses = 0 OR ic.use_count < ic.max_uses) "
           "AND (ic.expires_at IS NULL OR ic.expires_at > ?)")


def random_code() -> str:
    chars = [secrets.choice(CODE_ALPHABET) for _ in range(8)]
    return "".join(chars[:4]) + "-" + "".join(chars[4:])


def manages_all(user: dict[str, Any]) -> bool:
    return auth.is_admin(user)


def assignable_roles(user: dict[str, Any]) -> tuple[str, ...]:
    if manages_all(user):
        return ADMIN_ASSIGNABLE
    return tuple(role for role in EDITOR_ASSIGNABLE if within_authority(user, role, None))


def _categories(grants: perms.Grants, access: str) -> frozenset[int] | None:
    """The categories *grants* may read or write (None: all of them)."""
    if grants.is_admin:
        return None
    write = (grants.write.allowed if grants.write.restricted else None) if grants.role == "editor" else frozenset()
    if access == "write" or write is None:
        return write
    return grants.read.allowed | write if grants.read.restricted else None


def _covers(mine: frozenset[int] | None, theirs: frozenset[int] | None) -> bool:
    return mine is None or (theirs is not None and theirs <= mine)


def within_authority(creator: dict[str, Any], role: str | None, custom_role_id: int | None) -> bool:
    """Whether *creator* may hand out an account with this role (or custom role) by invite.

    Administrators may hand out anything an invite can carry. Others may
    always invite a plain member; anything more must stay within their own
    rights: base role, permission keys, and readable and writable categories.
    """
    if auth.is_admin(creator) and not auth.account_block(creator):
        return True
    role = role or "user"
    if role not in EDITOR_ASSIGNABLE or auth.account_block(creator):
        return False
    if custom_role_id is None and role == "user":
        return True
    mine = perms.load_grants(db, creator)
    theirs = perms.load_grants(db, {"id": "", "role": role, "custom_role_id": custom_role_id})
    if perms.ROLE_RANK[theirs.role] > perms.ROLE_RANK[mine.role]:
        return False
    if not theirs.keys <= mine.keys | perms.defaults("user"):
        return False
    return all(_covers(_categories(mine, access), _categories(theirs, access)) for access in ("read", "write"))


@dataclass
class CodeRequest:
    max_uses: int
    expires_at: str | None
    custom_code: str
    assigned_role: str | None
    assigned_custom_role_id: int | None


def parse_request(form: Any, custom_expiry: datetime | None) -> CodeRequest:
    try:
        max_uses = int(form.get("max_uses") or 1)
    except ValueError:
        raise AccountError("admin.codes.error.max_uses") from None
    if not 0 <= max_uses <= MAX_USES_LIMIT:
        raise AccountError("admin.codes.error.max_uses")
    mode = form.get("expiry_mode") or "48h"
    if mode in EXPIRY_PRESETS:
        expires_at = to_sql(utcnow() + timedelta(hours=EXPIRY_PRESETS[mode]))
    elif mode == "never":
        expires_at = None
    elif mode == "custom":
        if custom_expiry is None:
            raise AccountError("admin.codes.error.expiry")
        if custom_expiry <= utcnow():
            raise AccountError("admin.codes.error.expiry_past")
        expires_at = to_sql(custom_expiry)
    else:
        raise AccountError("admin.codes.error.expiry")
    custom_code = (form.get("custom_code") or "").strip()
    if custom_code and not CUSTOM_CODE_RE.fullmatch(custom_code):
        raise AccountError("admin.codes.error.custom_code")
    custom_role = (form.get("assigned_custom_role_id") or "").strip()
    return CodeRequest(
        max_uses=max_uses,
        expires_at=expires_at,
        custom_code=custom_code.upper(),
        assigned_role=(form.get("assigned_role") or "").strip() or None,
        assigned_custom_role_id=ascii_int(custom_role),
    )


def generate(actor: dict[str, Any], req: CodeRequest) -> str:
    if req.assigned_role and req.assigned_role not in (ADMIN_ASSIGNABLE if manages_all(actor) else EDITOR_ASSIGNABLE):
        raise AccountError("admin.codes.error.role")
    base_role = req.assigned_role
    if req.assigned_custom_role_id is not None:
        role = db.one("SELECT id, base_role FROM custom_roles WHERE id = ?", (req.assigned_custom_role_id,))
        if role is None:
            raise AccountError("admin.codes.error.role")
        base_role = role["base_role"]  # what sign-up grants (auth.invites.assigned_role)
    if not within_authority(actor, base_role, req.assigned_custom_role_id):
        raise AccountError("admin.codes.error.beyond_own")
    with db.transaction():
        if req.custom_code:
            if db.scalar("SELECT 1 FROM invite_codes WHERE code = ? COLLATE NOCASE", (req.custom_code,)):
                raise AccountError("admin.codes.error.taken")
            code = req.custom_code
        else:
            code = random_code()
            while db.scalar("SELECT 1 FROM invite_codes WHERE code = ? COLLATE NOCASE", (code,)):
                code = random_code()
        code_id = db.insert("invite_codes", {
            "code": code, "created_by": actor["id"], "created_at": now_sql(), "expires_at": req.expires_at,
            "max_uses": req.max_uses, "assigned_role": req.assigned_role,
            "assigned_custom_role_id": req.assigned_custom_role_id,
        })
    # Never the code itself: it is a credential for whoever reads the log.
    record("invite.created", actor_id=actor["id"], target_type="invite", target_id=code_id,
           details={"role": base_role or "user", "custom_role_id": req.assigned_custom_role_id,
                    "max_uses": req.max_uses, "expires_at": req.expires_at})
    return code


def _list(condition: str, params: list[Any], actor: dict[str, Any]) -> list[dict[str, Any]]:
    if not manages_all(actor):
        condition = f"({condition}) AND ic.created_by = ?"
        params = [*params, actor["id"]]
    codes = db.all(
        "SELECT ic.*, u.username AS creator_name, cr.name AS custom_role_name FROM invite_codes ic "
        "LEFT JOIN users u ON u.id = ic.created_by LEFT JOIN custom_roles cr ON cr.id = ic.assigned_custom_role_id "
        f"WHERE {condition} ORDER BY ic.created_at DESC, ic.id DESC",
        params,
    )
    usage = _usage([c["id"] for c in codes])
    for code in codes:
        code["used_by"] = usage.get(code["id"], [])
    return codes


def _usage(code_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not code_ids:
        return {}
    marks = ",".join("?" for _ in code_ids)
    rows = db.all(
        "SELECT icu.invite_code_id, icu.used_at, u.username FROM invite_code_usage icu "
        f"LEFT JOIN users u ON u.id = icu.user_id WHERE icu.invite_code_id IN ({marks}) ORDER BY icu.used_at",
        code_ids,
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["invite_code_id"], []).append(row)
    return out


def list_active(actor: dict[str, Any]) -> list[dict[str, Any]]:
    return _list(_ACTIVE, [now_sql()], actor)


def list_expired(actor: dict[str, Any]) -> list[dict[str, Any]]:
    return _list(f"NOT ({_ACTIVE})", [now_sql()], actor)


def get_for(actor: dict[str, Any], code_id: int) -> dict[str, Any] | None:
    """The code with *code_id* if *actor* may manage it."""
    code = db.one("SELECT * FROM invite_codes WHERE id = ?", (code_id,))
    if code is None or (not manages_all(actor) and code["created_by"] != actor["id"]):
        return None
    return code


def deactivate(code: dict[str, Any]) -> None:
    if db.execute("UPDATE invite_codes SET deleted = 1, deleted_at = ? WHERE id = ? AND deleted = 0",
                  (now_sql(), code["id"])).rowcount:
        record("invite.deactivated", target_type="invite", target_id=code["id"])


def is_active(code: dict[str, Any]) -> bool:
    if code["deleted"]:
        return False
    if code["max_uses"] and code["use_count"] >= code["max_uses"]:
        return False
    return not code["expires_at"] or code["expires_at"] > now_sql()


def purge(code: dict[str, Any]) -> None:
    """Remove a used, expired or deactivated code for good."""
    if is_active(code):
        raise AccountError("admin.codes.error.still_active")
    db.execute("DELETE FROM invite_codes WHERE id = ?", (code["id"],))
    record("invite.deleted", target_type="invite", target_id=code["id"], details={"uses": code["use_count"]})
