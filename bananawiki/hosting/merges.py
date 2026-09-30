"""Account merges: both accounts approve (or an administrator decides), then
every wiki, SSO link and collaboration of the source moves to the target and
the source is suspended with all its sessions and tokens revoked."""

from __future__ import annotations

from typing import Any

from ..core.timeutil import now_sql
from . import accounts, api_tokens, attention, events
from .db import db
from .errors import ServiceError

ACTIVE = ("pending", "approved")


def _usable(account: dict[str, Any] | None) -> dict[str, Any]:
    if not account or account["deleted_at"] or account["suspended"] or account["approval_status"] != "approved":
        raise ServiceError("hosting.merges.account_unavailable")
    return account


def get(merge_id: int) -> dict[str, Any] | None:
    return db.one(
        "SELECT m.*, s.username AS source_username, t.username AS target_username FROM hosting_account_merge_requests m "
        "JOIN accounts s ON s.id = m.source_account_id JOIN accounts t ON t.id = m.target_account_id WHERE m.id = ?",
        (merge_id,),
    )


def for_account(account_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT m.*, s.username AS source_username, t.username AS target_username FROM hosting_account_merge_requests m "
        "JOIN accounts s ON s.id = m.source_account_id JOIN accounts t ON t.id = m.target_account_id "
        "WHERE (m.source_account_id = ? OR m.target_account_id = ?) AND m.status IN ('pending', 'approved') "
        "ORDER BY m.created_at DESC", (account_id, account_id),
    )


def recent(limit: int = 100) -> list[dict[str, Any]]:
    return db.all(
        "SELECT m.*, s.username AS source_username, t.username AS target_username FROM hosting_account_merge_requests m "
        "JOIN accounts s ON s.id = m.source_account_id JOIN accounts t ON t.id = m.target_account_id "
        "ORDER BY m.created_at DESC LIMIT ?", (limit,),
    )


def request(source: dict[str, Any], target_username: str, reason: str) -> dict[str, Any]:
    target = accounts.active_by_username(target_username)
    if target is None:
        raise ServiceError("hosting.accounts.not_found")
    if target["id"] == source["id"]:
        raise ServiceError("hosting.merges.self")
    _usable(source)
    _usable(target)
    with db.transaction():
        busy = db.scalar(
            "SELECT 1 FROM hosting_account_merge_requests WHERE status IN ('pending', 'approved') AND "
            "(source_account_id IN (?, ?) OR target_account_id IN (?, ?))",
            (source["id"], target["id"], source["id"], target["id"]),
        )
        if busy:
            raise ServiceError("hosting.merges.busy")
        merge_id = db.insert("hosting_account_merge_requests", {
            "source_account_id": source["id"], "target_account_id": target["id"], "requested_by": source["id"],
            "request_reason": (reason or "")[:4000], "created_at": now_sql(), "source_approved": 1,
        })
        db.update("accounts", {"pending_merge_target_id": target["id"]}, "id = ?", (source["id"],))
        db.update("accounts", {"pending_merge_source_id": source["id"]}, "id = ?", (target["id"],))
        events.record("account", source["id"], "merge.requested", source["id"], target["username"])
    return get(merge_id)  # type: ignore[return-value]


def approve(merge_id: int, actor: dict[str, Any], *, as_admin: bool = False) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM hosting_account_merge_requests WHERE id = ?", (merge_id,))
        if row is None or row["status"] not in ACTIVE:
            raise ServiceError("hosting.merges.not_pending")
        if as_admin:
            source = target = admin = True
        else:
            _usable(actor)
            if actor["id"] not in (row["source_account_id"], row["target_account_id"]):
                raise ServiceError("hosting.merges.not_party")
            source = bool(row["source_approved"]) or actor["id"] == row["source_account_id"]
            target = bool(row["target_approved"]) or actor["id"] == row["target_account_id"]
            admin = bool(row["admin_approved"])
        db.update("hosting_account_merge_requests", {
            "source_approved": int(source), "target_approved": int(target), "admin_approved": int(admin),
            "status": "approved" if source and target else "pending",
        }, "id = ?", (merge_id,))
        if source and target and not as_admin and row["status"] != "approved":
            attention.created("merges.awaiting", merge_id)


def close(merge_id: int, actor: dict[str, Any], status: str) -> None:
    with db.transaction():
        row = db.one("SELECT * FROM hosting_account_merge_requests WHERE id = ?", (merge_id,))
        if row is None or row["status"] not in ACTIVE:
            raise ServiceError("hosting.merges.not_pending")
        party = actor["id"] in (row["source_account_id"], row["target_account_id"])
        if not actor["is_admin"] and (status == "denied" or not party):
            raise ServiceError("hosting.merges.not_party")
        db.update("hosting_account_merge_requests", {"status": status, "completed_at": now_sql()}, "id = ?", (merge_id,))
        _clear_pending(row["source_account_id"], row["target_account_id"])
        if status == "denied":
            for party in (row["source_account_id"], row["target_account_id"]):
                attention.decided(party, "merges.awaiting", "denied", object_id=merge_id)


def _clear_pending(source_id: str, target_id: str) -> None:
    db.execute("UPDATE accounts SET pending_merge_source_id = NULL, pending_merge_target_id = NULL WHERE id IN (?, ?)",
               (source_id, target_id))


def _merge(source_id: str, target_id: str, admin: dict[str, Any]) -> int:
    if source_id == target_id:
        raise ServiceError("hosting.merges.self")
    source = accounts.get(source_id)
    target = _usable(accounts.get(target_id))
    if source is None or source["deleted_at"]:
        raise ServiceError("hosting.merges.account_unavailable")
    if source["is_admin"] and not target["is_admin"]:
        raise ServiceError("hosting.merges.admin_into_user")
    if db.scalar("SELECT 1 FROM hosting_account_merge_logs WHERE source_account_id = ?", (source_id,)):
        raise ServiceError("hosting.merges.already_merged")
    conflict = db.scalar(
        "SELECT 1 FROM hosting_oauth_account_links s JOIN hosting_oauth_account_links t ON s.instance_id = t.instance_id "
        "WHERE s.account_id = ? AND t.account_id = ? AND s.wiki_user_id != t.wiki_user_id", (source_id, target_id),
    )
    if conflict:
        raise ServiceError("hosting.merges.sso_conflict")
    moved = db.execute("UPDATE instances SET account_id = ? WHERE account_id = ?", (target_id, source_id)).rowcount
    db.execute("DELETE FROM instance_collaborators WHERE account_id = ? AND instance_id IN "
               "(SELECT id FROM instances WHERE account_id = ?)", (target_id, target_id))
    db.execute("UPDATE OR IGNORE instance_collaborators SET account_id = ? WHERE account_id = ?", (target_id, source_id))
    db.execute("DELETE FROM instance_collaborators WHERE account_id = ?", (source_id,))
    db.execute("DELETE FROM hosting_oauth_account_links WHERE account_id = ? AND instance_id IN "
               "(SELECT instance_id FROM hosting_oauth_account_links WHERE account_id = ?)", (source_id, target_id))
    db.execute("UPDATE hosting_oauth_account_links SET account_id = ? WHERE account_id = ?", (target_id, source_id))
    db.execute("UPDATE accounts SET suspended = 1, suspended_at = ?, suspended_until = NULL, "
               "suspend_reason = 'Account merged into another', session_version = session_version + 1 WHERE id = ?",
               (now_sql(), source_id))
    db.execute("UPDATE hosting_account_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
               (now_sql(), source_id))
    db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ?", (source_id,))
    api_tokens.revoke_all(source_id, admin["id"], "Account merged into another")
    db.insert("hosting_account_merge_logs", {"target_account_id": target_id, "source_account_id": source_id,
                                             "merged_by": admin["id"], "instances_transferred": moved,
                                             "created_at": now_sql()})
    _clear_pending(source_id, target_id)
    events.record("account", source_id, "merge.completed", admin["id"], target["username"])
    return moved


def execute(merge_id: int, admin: dict[str, Any]) -> int:
    with db.transaction():
        row = db.one("SELECT * FROM hosting_account_merge_requests WHERE id = ?", (merge_id,))
        if row is None or row["status"] != "approved" or not (row["source_approved"] and row["target_approved"]):
            raise ServiceError("hosting.merges.needs_approval")
        moved = _merge(row["source_account_id"], row["target_account_id"], admin)
        db.update("hosting_account_merge_requests", {"status": "merged", "completed_at": now_sql()}, "id = ?",
                  (merge_id,))
        attention.decided(row["target_account_id"], "merges.awaiting", "approved", object_id=merge_id)
    return moved


def admin_merge(source_username: str, target_username: str, admin: dict[str, Any]) -> int:
    source = accounts.active_by_username(source_username)
    target = accounts.active_by_username(target_username)
    if source is None or target is None:
        raise ServiceError("hosting.accounts.not_found")
    with db.transaction():
        moved = _merge(source["id"], target["id"], admin)
        db.execute(
            "UPDATE hosting_account_merge_requests SET status = 'cancelled', completed_at = ? WHERE status IN "
            "('pending', 'approved') AND (source_account_id IN (?, ?) OR target_account_id IN (?, ?))",
            (now_sql(), source["id"], target["id"], source["id"], target["id"]),
        )
    return moved
