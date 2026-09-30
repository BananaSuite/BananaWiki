"""Wiki collaborators and ownership transfers.

Rules fixed from 1.4: a collaborator holding ``manage_collaborators`` may
only hand out permissions it holds itself, cannot grant ``full_access`` and
cannot change or remove owners of full access; transfers are always made
from the current owner (an administrator starting one no longer produced a
transfer that could never be accepted); the recipient must be an active,
approved account, and accepting one applies the new owner's privileges.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..core.timeutil import now_sql
from . import accounts, events, instances
from .db import db
from .errors import ServiceError

PERMISSIONS = ("view", "start_stop", "terminate", "reset_password", "reset_wiki", "download", "toggle_mode",
               "manage_collaborators", "use_case", "analytics")
ROLES = ("full_access", "custom")


def _permissions(row: dict[str, Any]) -> set[str]:
    if row["role"] == "full_access":
        return set(PERMISSIONS)
    try:
        return {p for p in json.loads(row["permissions"] or "[]") if p in PERMISSIONS} | {"view"}
    except (TypeError, ValueError):
        return {"view"}


def membership(instance_id: str, account_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                  (instance_id, account_id))


def permissions_of(inst: dict[str, Any], account: dict[str, Any]) -> set[str]:
    """What *account* may do on *inst*: everything for owners and administrators."""
    if account["is_admin"] or inst["account_id"] == account["id"]:
        return set(PERMISSIONS)
    row = membership(inst["id"], account["id"])
    return _permissions(row) if row else set()


def can(inst: dict[str, Any] | None, account: dict[str, Any], permission: str) -> bool:
    return inst is not None and permission in permissions_of(inst, account)


def list_for(instance_id: str) -> list[dict[str, Any]]:
    rows = db.all("SELECT c.*, a.username FROM instance_collaborators c JOIN accounts a ON a.id = c.account_id "
                  "WHERE c.instance_id = ? ORDER BY c.created_at", (instance_id,))
    return [{**row, "granted": sorted(_permissions(row))} for row in rows]


def _check_grant(inst: dict[str, Any], actor: dict[str, Any], role: str, permissions: list[str]) -> list[str]:
    if role not in ROLES:
        raise ServiceError("hosting.collaborators.invalid_role")
    granted = sorted({p for p in permissions if p in PERMISSIONS})
    held = permissions_of(inst, actor)
    privileged = actor["is_admin"] or inst["account_id"] == actor["id"]
    if not privileged and (role == "full_access" or not set(granted) <= held):
        raise ServiceError("hosting.collaborators.cannot_grant")
    return [] if role == "full_access" else granted


def add(inst: dict[str, Any], actor: dict[str, Any], username: str, role: str, permissions: list[str]) -> None:
    target = accounts.active_by_username(username)
    if target is None or target["approval_status"] != "approved":
        raise ServiceError("hosting.accounts.not_found")
    if target["id"] == inst["account_id"]:
        raise ServiceError("hosting.collaborators.owner")
    granted = _check_grant(inst, actor, role, permissions)
    try:
        db.insert("instance_collaborators", {
            "instance_id": inst["id"], "account_id": target["id"], "role": role, "permissions": json.dumps(granted),
            "invited_by": actor["id"], "created_at": now_sql(),
        })
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.collaborators.exists") from error
    events.record("instance", inst["id"], "collaborator.added", actor["id"], target["username"])


def _editable(inst: dict[str, Any], actor: dict[str, Any], member: dict[str, Any] | None) -> dict[str, Any]:
    if member is None:
        raise ServiceError("hosting.collaborators.not_found")
    privileged = actor["is_admin"] or inst["account_id"] == actor["id"]
    if not privileged and member["role"] == "full_access":
        raise ServiceError("hosting.collaborators.cannot_grant")
    return member


def update(inst: dict[str, Any], actor: dict[str, Any], account_id: str, role: str, permissions: list[str]) -> None:
    _editable(inst, actor, membership(inst["id"], account_id))
    granted = _check_grant(inst, actor, role, permissions)
    db.update("instance_collaborators", {"role": role, "permissions": json.dumps(granted)},
              "instance_id = ? AND account_id = ?", (inst["id"], account_id))
    events.record("instance", inst["id"], "collaborator.updated", actor["id"], account_id)


def remove(inst: dict[str, Any], actor: dict[str, Any], account_id: str) -> None:
    member = membership(inst["id"], account_id)
    if account_id != actor["id"]:
        _editable(inst, actor, member)
    elif member is None:
        raise ServiceError("hosting.collaborators.not_found")
    db.execute("DELETE FROM instance_collaborators WHERE instance_id = ? AND account_id = ?", (inst["id"], account_id))
    events.record("instance", inst["id"], "collaborator.removed", actor["id"], account_id)


# ── Ownership transfers ───────────────────────────────────────────────────────


def pending_transfer(instance_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT t.*, a.username AS to_username FROM instance_ownership_transfers t JOIN accounts a "
        "ON a.id = t.to_account_id WHERE t.instance_id = ? AND t.status = 'pending' ORDER BY t.id DESC LIMIT 1",
        (instance_id,),
    )


def incoming_transfers(account_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT t.*, f.username AS from_username, i.subdomain FROM instance_ownership_transfers t "
        "JOIN accounts f ON f.id = t.from_account_id JOIN instances i ON i.id = t.instance_id "
        "WHERE t.to_account_id = ? AND t.status = 'pending' AND i.status != 'terminated' ORDER BY t.created_at DESC",
        (account_id,),
    )


def start_transfer(inst: dict[str, Any], actor: dict[str, Any], username: str) -> None:
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    target = accounts.active_by_username(username)
    if target is None or target["approval_status"] != "approved" or target["suspended"]:
        raise ServiceError("hosting.accounts.not_found")
    if target["id"] == inst["account_id"]:
        raise ServiceError("hosting.transfers.already_owner")
    with db.transaction():
        db.execute("UPDATE instance_ownership_transfers SET status = 'cancelled', resolved_at = ? "
                   "WHERE instance_id = ? AND status = 'pending'", (now_sql(), inst["id"]))
        db.insert("instance_ownership_transfers", {
            "instance_id": inst["id"], "from_account_id": inst["account_id"], "to_account_id": target["id"],
            "status": "pending", "created_at": now_sql(),
        })
        events.record("instance", inst["id"], "transfer.started", actor["id"], target["username"])


def accept_transfer(transfer_id: int, account: dict[str, Any]) -> str:
    row = db.one("SELECT * FROM instance_ownership_transfers WHERE id = ? AND status = 'pending'", (transfer_id,))
    if row is None or row["to_account_id"] != account["id"]:
        raise ServiceError("hosting.transfers.not_found")
    inst = instances.get(row["instance_id"])
    if inst is None or inst["status"] == "terminated" or inst["account_id"] != row["from_account_id"]:
        db.update("instance_ownership_transfers", {"status": "cancelled", "resolved_at": now_sql()}, "id = ?",
                  (transfer_id,))
        raise ServiceError("hosting.transfers.stale")
    with db.transaction():
        db.update("instance_ownership_transfers", {"status": "accepted", "resolved_at": now_sql()}, "id = ?",
                  (transfer_id,))
        db.execute(
            "INSERT OR IGNORE INTO instance_collaborators (instance_id, account_id, role, permissions, invited_by, "
            "created_at) VALUES (?, ?, 'full_access', '[]', ?, ?)",
            (inst["id"], row["from_account_id"], account["id"], now_sql()),
        )
    instances.move_to_owner(inst, account, actor_id=account["id"])
    return inst["id"]


def resolve_transfer(transfer_id: int, account_id: str, *, as_sender: bool) -> None:
    column, status = ("from_account_id", "cancelled") if as_sender else ("to_account_id", "declined")
    changed = db.execute(
        f"UPDATE instance_ownership_transfers SET status = ?, resolved_at = ? WHERE id = ? AND {column} = ? "
        "AND status = 'pending'", (status, now_sql(), transfer_id, account_id),
    ).rowcount
    if not changed:
        raise ServiceError("hosting.transfers.not_found")
