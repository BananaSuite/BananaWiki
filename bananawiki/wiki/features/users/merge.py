"""Account merges: move everything one account owns into another.

Workflow
--------
1. A signed-in user asks to merge another account into theirs (or theirs into
   another one), confirming with their password. Their side is confirmed.
2. The owner of the other account signs in and confirms with *their* password.
   The request is then ``approved_by_both``.
3. An administrator approves it, which runs the merge. Administrators can also
   merge two accounts directly.

The merge re-points every column that references ``users(id)`` from the
source to the target (discovered from the schema, so features added later are
covered), keeps the target's row on unique conflicts, and then deletes or
locks the source account (suspended for good, see :func:`_lock_source`).
Credentials, audit trails and the source's own permission grants are not
transferred.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from ....core.sqlite import quote_identifier, tuples
from ....core.timeutil import now_sql
from ... import accounts, attention, auth
from ...db import db
from ...registry import emit
from ..admin.service import clear_overrides, protection_error
from ..pages import service as pages
from . import preferences, service

ACTIVE = ("pending", "approved_by_both")
MAX_REASON = 500
# Tables that stay with the source account.
KEEP_WITH_SOURCE = frozenset({
    "user_sessions", "api_tokens", "userbot_api_tokens", "api_service__tokens", "api_service__audit_log",
    "account_merge_requests", "account_merge_logs", "username_history", "role_history", "suspension_audit",
    "impersonation_logs", "temp_users", "temp_roles", "user_permissions", "user_category_access",
    "user_allowed_categories", "editor_category_access", "editor_allowed_categories", "users",
})


class MergeError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Requests ──────────────────────────────────────────────────────────────────


def get(merge_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM account_merge_requests WHERE id = ?", (merge_id,))


_SELECT = (
    "SELECT r.*, s.username AS source_username, t.username AS target_username, c.username AS creator_username "
    "FROM account_merge_requests r LEFT JOIN users s ON s.id = r.source_user_id "
    "LEFT JOIN users t ON t.id = r.target_user_id LEFT JOIN users c ON c.id = r.created_by"
)


def for_user(user_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(active, finished) requests involving *user_id*."""
    rows = db.all(f"{_SELECT} WHERE r.source_user_id = ? OR r.target_user_id = ? ORDER BY r.created_at DESC, r.id DESC",
                  (user_id, user_id))
    return [r for r in rows if r["status"] in ACTIVE], [r for r in rows if r["status"] not in ACTIVE]


def listing(status: str) -> list[dict[str, Any]]:
    if status == "all":
        return db.all(f"{_SELECT} ORDER BY r.created_at DESC, r.id DESC LIMIT 500")
    return db.all(f"{_SELECT} WHERE r.status IN ('pending', 'approved_by_both') ORDER BY r.created_at DESC")


def awaiting_admin_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM account_merge_requests WHERE status = 'approved_by_both'",
                         default=0))


def awaiting_admin_oldest() -> str | None:
    return db.scalar("SELECT MIN(created_at) FROM account_merge_requests WHERE status = 'approved_by_both'")


def _check_pair(source: dict[str, Any], target: dict[str, Any]) -> None:
    if source["id"] == target["id"]:
        raise MergeError("users.merge.error.same_account")
    if source.get("is_superuser") or target.get("is_superuser"):
        raise MergeError("users.merge.error.protected")
    if source["role"] == "owner" and accounts.owners_count() <= 1:
        raise MergeError("users.merge.error.last_owner")
    # The source is deleted, or locked as a plain user: neither may leave the wiki without an administrator.
    if source["role"] in ("admin", "owner") and not source.get("suspended") and service.active_admin_count() <= 1:
        raise MergeError("users.merge.error.last_admin")


def create(user: dict[str, Any], other: dict[str, Any], *, into_mine: bool, reason: str,
           password: str | None) -> dict[str, Any]:
    """Request a merge between *user* and *other*; *user*'s side is confirmed at once."""
    source, target = (other, user) if into_mine else (user, other)
    _check_pair(source, target)
    if not service.password_ok(user, password):
        raise MergeError("auth.error.current_password_wrong")
    with db.transaction():
        busy = db.scalar(
            "SELECT 1 FROM account_merge_requests WHERE status IN ('pending', 'approved_by_both') "
            "AND (source_user_id IN (?, ?) OR target_user_id IN (?, ?))",
            (source["id"], target["id"], source["id"], target["id"]),
        )
        if busy:
            raise MergeError("users.merge.error.already_pending")
        merge_id = db.insert("account_merge_requests", {
            "source_user_id": source["id"], "target_user_id": target["id"], "created_by": user["id"],
            "status": "pending", "source_approved": 1 if user["id"] == source["id"] else 0,
            "target_approved": 1 if user["id"] == target["id"] else 0,
            "request_reason": " ".join((reason or "").split())[:MAX_REASON], "created_at": now_sql(),
        })
        db.execute("UPDATE users SET pending_merge_target_id = ? WHERE id = ?", (target["id"], source["id"]))
        db.execute("UPDATE users SET pending_merge_source_id = ? WHERE id = ?", (source["id"], target["id"]))
    request_row = get(merge_id)
    assert request_row is not None
    return request_row


def _clear_pending(request_row: dict[str, Any]) -> None:
    db.execute(
        "UPDATE users SET pending_merge_source_id = NULL, pending_merge_target_id = NULL WHERE id IN (?, ?)",
        (request_row["source_user_id"], request_row["target_user_id"]),
    )


def confirm(merge_id: int, user: dict[str, Any], password: str | None) -> dict[str, Any]:
    """The other account's owner confirms with their password."""
    with db.transaction():
        row = get(merge_id)
        if row is None or user["id"] not in (row["source_user_id"], row["target_user_id"]):
            raise MergeError("users.merge.error.not_found")
        if row["status"] != "pending":
            raise MergeError("users.merge.error.not_pending")
        if not service.password_ok(user, password):
            raise MergeError("auth.error.current_password_wrong")
        side = "source_approved" if user["id"] == row["source_user_id"] else "target_approved"
        db.execute(f"UPDATE account_merge_requests SET {side} = 1 WHERE id = ?", (merge_id,))
        db.execute(
            "UPDATE account_merge_requests SET status = 'approved_by_both' "
            "WHERE id = ? AND source_approved = 1 AND target_approved = 1",
            (merge_id,),
        )
    updated = get(merge_id)
    assert updated is not None
    if updated["status"] == "approved_by_both":
        attention.created("users.merge_requests", merge_id)
    return updated


def close(merge_id: int, status: str, *, user: dict[str, Any] | None = None) -> None:
    """Cancel or deny an active request. With *user*, only its parties may do it."""
    with db.transaction():
        row = get(merge_id)
        if row is None or (user is not None and user["id"] not in (
                row["source_user_id"], row["target_user_id"], row["created_by"])):
            raise MergeError("users.merge.error.not_found")
        if row["status"] not in ACTIVE:
            raise MergeError("users.merge.error.not_pending")
        db.execute("UPDATE account_merge_requests SET status = ? WHERE id = ?", (status, merge_id))
        _clear_pending(row)


def approve(merge_id: int, admin: dict[str, Any], *, delete_source: bool) -> dict[str, Any]:
    """Administrator approval: runs the merge once both owners confirmed."""
    row = get(merge_id)
    if row is None:
        raise MergeError("users.merge.error.not_found")
    if row["status"] != "approved_by_both":
        raise MergeError("users.merge.error.not_confirmed")
    source = accounts.by_id(row["source_user_id"])
    target = accounts.by_id(row["target_user_id"])
    if source is None or target is None:
        raise MergeError("users.merge.error.account_missing")
    return execute(source, target, admin, delete_source=delete_source, merge_id=merge_id)


# ── Execution ─────────────────────────────────────────────────────────────────


def _user_columns() -> list[tuple[str, str]]:
    """(table, column) for every foreign key to ``users(id)`` outside KEEP_WITH_SOURCE."""
    found = []
    for (table,) in tuples(db.conn, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        if table in KEEP_WITH_SOURCE:
            continue
        for row in tuples(db.conn, f"PRAGMA foreign_key_list({quote_identifier(table)})"):
            if row[2] == "users" and row[4] in (None, "id"):
                found.append((table, row[3]))
    return found


def preview(source: dict[str, Any]) -> dict[str, int]:
    """How many rows of each table would move."""
    counts = {}
    for table, column in _user_columns():
        n = int(db.scalar(f"SELECT COUNT(*) FROM {quote_identifier(table)} WHERE {quote_identifier(column)} = ?",
                          (source["id"],), default=0))
        if n:
            counts[f"{table}.{column}"] = n
    return counts


def _prepare_special_tables(source_id: str, target_id: str) -> None:
    """Rows that need more than re-pointing a column."""
    existing = {row[0] for row in tuples(db.conn, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "assessment_attempts" in existing:
        db.execute(
            "UPDATE assessment_attempts SET attempt_number = attempt_number + COALESCE((SELECT MAX(a.attempt_number) "
            "FROM assessment_attempts a WHERE a.user_id = ? AND a.assessment_id = assessment_attempts.assessment_id), 0) "
            "WHERE user_id = ?",
            (target_id, source_id),
        )
    if "chats" in existing:
        # A conversation between the two accounts would become a chat with oneself.
        db.execute("DELETE FROM chats WHERE (user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)",
                   (source_id, target_id, target_id, source_id))
    if "user_profile_fields__values" in existing:
        db.execute("UPDATE OR IGNORE user_profile_fields__values SET user_id = ? WHERE user_id = ?",
                   (target_id, source_id))
    source_profile = service.get_profile(source_id)
    target_profile = service.get_profile(target_id)
    if source_profile and target_profile:
        fill = {key: source_profile[key] for key in ("real_name", "bio", "birth_date", "avatar_filename")
                if not target_profile[key] and source_profile[key]}
        if fill:
            db.update("user_profiles", fill, "user_id = ?", (target_id,))
            if "avatar_filename" in fill:
                db.execute("UPDATE user_profiles SET avatar_filename = '' WHERE user_id = ?", (source_id,))


def _dedupe_badges(target_id: str) -> None:
    db.execute(
        "DELETE FROM user_badges WHERE user_id = ? AND revoked = 0 AND badge_type_id IN "
        "(SELECT id FROM badge_types WHERE allow_multiple = 0) AND id NOT IN (SELECT MIN(id) FROM user_badges "
        "WHERE user_id = ? AND revoked = 0 GROUP BY badge_type_id)",
        (target_id, target_id),
    )


def _merge_preferences(source: dict[str, Any], target: dict[str, Any]) -> None:
    merged = {**preferences.parse(source.get("accessibility")), **preferences.parse(target.get("accessibility"))}
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps(merged), target["id"]))


def _locked_name(username: str) -> str:
    base = f"merged_{username}"[:50]
    candidate, n = base, 2
    while accounts.username_taken(candidate):
        suffix = f"_{n}"
        candidate = base[:50 - len(suffix)] + suffix
        n += 1
    return candidate


def _current(source: dict[str, Any], target: dict[str, Any], admin: dict[str, Any], merge_id: int | None
             ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reload both accounts and the administrator inside the merge transaction and check them again.

    The caller's copies may predate a promotion, a suspension or another owner
    stepping down: two owners merged away at the same time, or one merged away
    while the other steps down, would leave the wiki without an owner. Nobody
    consented to a direct merge (no *merge_id*), so there the administrator must
    also be allowed to change both accounts.
    """
    current_source, current_target = accounts.by_id(source["id"]), accounts.by_id(target["id"])
    if current_source is None or current_target is None:
        raise MergeError("users.merge.error.account_missing")
    actor = accounts.by_id(admin["id"])
    if actor is None or not auth.is_admin(actor) or auth.account_block(actor) \
            or actor["password"] != admin["password"]:
        raise MergeError("error.forbidden")
    if merge_id is not None and db.scalar("SELECT status FROM account_merge_requests WHERE id = ?",
                                          (merge_id,)) != "approved_by_both":
        raise MergeError("users.merge.error.not_pending")
    _check_pair(current_source, current_target)
    if merge_id is None:
        refusal = protection_error(actor, current_source) or protection_error(actor, current_target)
        if refusal:
            raise MergeError(refusal)
    return current_source, current_target, actor


def _uploads_left_behind(source: dict[str, Any], target_id: str) -> list[str]:
    """Images of the deleted source, except those the target now uses (a background carried over)."""
    target = accounts.by_id(target_id)
    kept = set(service.uploads_of(target)) if target else set()
    return [name for name in service.uploads_of(source) if name not in kept]


def _lock_source(source: dict[str, Any], target: dict[str, Any], admin: dict[str, Any],
                 password_hash: str) -> None:
    """Keep the emptied source for the record, out of reach of whoever knew its password.

    It is renamed, given a password nobody knows, reduced to a plain user (owners
    may lift their own suspension) and suspended with an audit entry that
    :func:`service.may_reactivate_self` never accepts; only an administrator can
    undo this. Runs inside the merge transaction on the account as reloaded there.
    """
    source_id = source["id"]
    locked_name = _locked_name(source["username"])
    reason = f"Merged into @{target['username']}"
    db.execute(
        "UPDATE users SET username = ?, password = ?, role = 'user', custom_role_id = NULL, suspended = 1, "
        "suspended_until = NULL, suspend_reason = ?, suspend_reason_visible = 0, suspend_time_visible = 0 "
        "WHERE id = ?",
        (locked_name, password_hash, reason, source_id),
    )
    db.insert("username_history", {"user_id": source_id, "old_username": source["username"],
                                   "new_username": locked_name, "changed_at": now_sql()})
    # An expiring temporary role would otherwise give the old role back; overrides
    # go as on any demotion (admin.service.change_role).
    db.execute("DELETE FROM temp_roles WHERE user_id = ?", (source_id,))
    clear_overrides(source_id)
    if source["role"] != "user":
        db.insert("role_history", {"user_id": source_id, "old_role": source["role"], "new_role": "user",
                                   "changed_by": admin["id"], "changed_at": now_sql()})
    db.insert("suspension_audit", {"user_id": source_id, "action": "suspend", "reason": reason,
                                   "duration": "permanent", "performed_by": admin["id"], "imposed_by_top": 1,
                                   "created_at": now_sql()})
    auth.revoke_sessions(source_id)


def _announce_lock(before: dict[str, Any], admin: dict[str, Any]) -> None:
    user = accounts.by_id(before["id"])
    emit("user.renamed", user=user, old_username=before["username"], changed_by=admin["id"])
    if before["role"] != "user":
        emit("user.role_changed", user=user, old_role=before["role"], new_role="user", changed_by=admin["id"])
    emit("user.suspended", user=user, until=None, actor_id=admin["id"])


def execute(source: dict[str, Any], target: dict[str, Any], admin: dict[str, Any], *, delete_source: bool,
            merge_id: int | None = None) -> dict[str, Any]:
    """Move the source account's data to the target, then delete or lock the source.

    The accounts are checked again (:func:`_current`) in the same transaction
    as the move and the deletion or lock, so a refused merge moves nothing.
    Mentions are rewritten last: a page that cannot be updated keeps its text,
    the merge stands.
    """
    _check_pair(source, target)
    # Hashing is slow: not while holding the write lock.
    unusable_password = None if delete_source else accounts.hash_password(secrets.token_urlsafe(32))
    leftovers: list[str] = []
    moved: dict[str, int] = {}
    with db.transaction():
        source, target, admin = _current(source, target, admin, merge_id)
        _prepare_special_tables(source["id"], target["id"])
        for table, column in _user_columns():
            cursor = db.execute(
                f"UPDATE OR IGNORE {quote_identifier(table)} SET {quote_identifier(column)} = ? "
                f"WHERE {quote_identifier(column)} = ?",
                (target["id"], source["id"]),
            )
            if cursor.rowcount:
                moved[f"{table}.{column}"] = cursor.rowcount
        if "user_badges.user_id" in moved:
            _dedupe_badges(target["id"])
        _merge_preferences(source, target)
        if merge_id is not None:
            db.execute("UPDATE account_merge_requests SET status = 'merged', completed_at = ?, admin_approved = ? "
                       "WHERE id = ?", (now_sql(), admin["id"], merge_id))
        db.execute(
            "UPDATE account_merge_requests SET status = 'cancelled' WHERE status IN ('pending', 'approved_by_both') "
            "AND (source_user_id = ? OR target_user_id = ?)",
            (source["id"], source["id"]),
        )
        db.insert("account_merge_logs", {"target_user_id": target["id"], "source_user_id": source["id"],
                                         "merged_by": admin["id"], "data_transferred": json.dumps(moved),
                                         "created_at": now_sql()})
        _clear_pending({"source_user_id": source["id"], "target_user_id": target["id"]})
        if unusable_password is None:
            leftovers = _uploads_left_behind(source, target["id"])
            try:
                accounts.delete(source, deleted_by=admin["id"], emit_event=False)
            except accounts.AccountError as error:
                raise MergeError(error.key, **error.values) from error
        else:
            _lock_source(source, target, admin, unusable_password)
    if unusable_password is None:
        emit("user.deleted", user=source, deleted_by=admin["id"])
        for name in leftovers:
            service.delete_upload(name)
    else:
        _announce_lock(source, admin)
    pages.rewrite_mentions(source["username"], "@" + target["username"])
    return moved
