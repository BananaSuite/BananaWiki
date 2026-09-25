"""Atomic account merges with explicit approvals and retained instance data."""

from datetime import datetime, timezone
import sqlite3

from ._api_tokens import revoke_account_api_tokens
from ._connection import get_hosting_db_context


def _now():
    return datetime.now(timezone.utc).isoformat()


def _account(conn, account_id, *, active=False, admin=False):
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row or (active and (row["suspended"] or row["approval_status"] in {"pending", "denied"})):
        raise ValueError("The account is unavailable for this merge.")
    if admin and not row["is_admin"]:
        raise ValueError("An active administrator must complete this action.")
    return row


def _request(conn, merge_id):
    row = conn.execute("SELECT * FROM hosting_account_merge_requests WHERE id=?", (merge_id,)).fetchone()
    if not row:
        raise ValueError("Merge request not found.")
    return row


def _clear_pending(conn, source, target):
    conn.execute("UPDATE accounts SET pending_merge_source_id=NULL, pending_merge_target_id=NULL WHERE id IN (?,?)", (source, target))


def create_merge_request(source_account_id, target_account_id, requested_by, reason=""):
    if source_account_id == target_account_id:
        raise ValueError("Cannot merge an account with itself.")
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account(conn, source_account_id, active=True)
        _account(conn, target_account_id, active=True)
        actor = _account(conn, requested_by, active=True)
        if requested_by not in {source_account_id, target_account_id} and not actor["is_admin"]:
            raise ValueError("You are not part of this merge.")
        existing = conn.execute(
            "SELECT * FROM hosting_account_merge_requests WHERE status IN ('pending','approved') "
            "AND (source_account_id IN (?,?) OR target_account_id IN (?,?)) LIMIT 1",
            (source_account_id, target_account_id, source_account_id, target_account_id),
        ).fetchone()
        if existing:
            if (existing["source_account_id"], existing["target_account_id"]) == (source_account_id, target_account_id):
                return existing
            raise ValueError("One of these accounts already has an active merge request.")
        cursor = conn.execute(
            "INSERT INTO hosting_account_merge_requests (source_account_id,target_account_id,requested_by,request_reason,created_at) VALUES(?,?,?,?,?)",
            (source_account_id, target_account_id, requested_by, str(reason)[:4000], _now()),
        )
        conn.execute("UPDATE accounts SET pending_merge_target_id=? WHERE id=?", (target_account_id, source_account_id))
        conn.execute("UPDATE accounts SET pending_merge_source_id=? WHERE id=?", (source_account_id, target_account_id))
        result = _request(conn, cursor.lastrowid)
        conn.commit()
        return result


def get_merge_request(merge_id):
    with get_hosting_db_context() as conn:
        return conn.execute("SELECT * FROM hosting_account_merge_requests WHERE id=?", (merge_id,)).fetchone()


def get_active_requests_for_account(account_id):
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM hosting_account_merge_requests WHERE (source_account_id=? OR target_account_id=?) "
            "AND status IN ('pending','approved') ORDER BY created_at DESC", (account_id, account_id),
        ).fetchall()


def _approve(merge_id, actor_id, side):
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        req = _request(conn, merge_id)
        actor = _account(conn, actor_id, active=True)
        if side == "admin":
            if not actor["is_admin"]:
                raise ValueError("An administrator must approve this action.")
        elif actor_id != req[side + "_account_id"]:
            raise ValueError("You are not the approving account.")
        if req["status"] not in {"pending", "approved"}:
            raise ValueError("This merge request is no longer pending.")
        source = bool(req["source_approved"] or side in {"source", "admin"})
        target = bool(req["target_approved"] or side in {"target", "admin"})
        conn.execute(
            "UPDATE hosting_account_merge_requests SET source_approved=?,target_approved=?,admin_approved=?,status=? WHERE id=?",
            (source, target, bool(req["admin_approved"] or side == "admin"), "approved" if source and target else "pending", merge_id),
        )
        result = _request(conn, merge_id)
        conn.commit()
        return result


def approve_by_source(merge_id, approver_id):
    return _approve(merge_id, approver_id, "source")


def approve_by_target(merge_id, approver_id):
    return _approve(merge_id, approver_id, "target")


def approve_by_admin(merge_id, admin_id):
    return _approve(merge_id, admin_id, "admin")


def _close_request(merge_id, actor_id, status):
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        req = _request(conn, merge_id)
        actor = _account(conn, actor_id, active=True)
        if not actor["is_admin"] and (status == "denied" or actor_id not in {req["source_account_id"], req["target_account_id"]}):
            raise ValueError("You cannot close this merge request.")
        if req["status"] not in {"pending", "approved"}:
            raise ValueError("This merge request is already closed.")
        conn.execute("UPDATE hosting_account_merge_requests SET status=? WHERE id=?", (status, merge_id))
        _clear_pending(conn, req["source_account_id"], req["target_account_id"])
        result = _request(conn, merge_id)
        conn.commit()
        return result


def deny_merge(merge_id, admin_id):
    return _close_request(merge_id, admin_id, "denied")


def cancel_merge(merge_id, account_id):
    return _close_request(merge_id, account_id, "cancelled")


def _merge(conn, source_id, target_id, actor_id):
    if source_id == target_id:
        raise ValueError("Cannot merge an account with itself.")
    _account(conn, actor_id, active=True, admin=True)
    source = _account(conn, source_id)
    target = _account(conn, target_id, active=True)
    if source["is_admin"] and not target["is_admin"]:
        raise ValueError("Demote the source administrator and apply its tenant restrictions before merging into a regular account.")
    if conn.execute("SELECT 1 FROM hosting_account_merge_logs WHERE source_account_id=? LIMIT 1", (source_id,)).fetchone():
        raise ValueError("This source account has already been merged.")
    # Keep all instances, including terminated instances still available for
    # export during their retention period. A failed write rolls everything back.
    transferred = conn.execute("UPDATE instances SET account_id=? WHERE account_id=?", (target_id, source_id)).rowcount
    # Identical SSO mappings can be coalesced. Conflicting wiki identities need
    # an administrator's explicit choice and must never be silently discarded.
    conflict = conn.execute(
        "SELECT 1 FROM hosting_oauth_account_links source JOIN hosting_oauth_account_links target "
        "ON source.instance_id=target.instance_id WHERE source.account_id=? AND target.account_id=? "
        "AND source.wiki_user_id<>target.wiki_user_id LIMIT 1", (source_id, target_id),
    ).fetchone()
    if conflict:
        raise ValueError("These accounts link to different users on the same wiki. Resolve that wiki's SSO link before merging.")
    conn.execute(
        "DELETE FROM hosting_oauth_account_links WHERE account_id=? AND instance_id IN "
        "(SELECT instance_id FROM hosting_oauth_account_links WHERE account_id=?)", (source_id, target_id),
    )
    conn.execute("UPDATE hosting_oauth_account_links SET account_id=? WHERE account_id=?", (target_id, source_id))
    now = _now()
    conn.execute(
        "UPDATE accounts SET suspended=1,suspended_at=?,suspended_until=NULL,suspend_reason='Account merged into another', "
        "session_version=session_version+1 WHERE id=?", (now, source_id),
    )
    conn.execute("UPDATE hosting_account_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL", (now, source_id))
    conn.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id=?", (source_id,))
    revoke_account_api_tokens(conn, source_id, actor_id, "Account merged into another")
    conn.execute(
        "INSERT INTO hosting_account_merge_logs (target_account_id,source_account_id,merged_by,instances_transferred) VALUES(?,?,?,?)",
        (target_id, source_id, actor_id, transferred),
    )
    _clear_pending(conn, source_id, target_id)
    return {"success": True, "source_account_id": source_id, "target_account_id": target_id, "instances_transferred": transferred}


def execute_merge(merge_id, actor_id):
    """Complete an approved merge exactly once under one write transaction."""
    try:
        with get_hosting_db_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = _request(conn, merge_id)
            if req["status"] != "approved" or not (req["source_approved"] and req["target_approved"]):
                raise ValueError("Both accounts must approve before the merge can be completed.")
            result = _merge(conn, req["source_account_id"], req["target_account_id"], actor_id)
            conn.execute("UPDATE hosting_account_merge_requests SET status='merged',completed_at=? WHERE id=?", (_now(), merge_id))
            conn.commit()
            return result
    except sqlite3.IntegrityError as error:
        raise ValueError("The accounts have conflicting data. Nothing was merged.") from error


def admin_execute_merge(source_account_id, target_account_id, admin_id):
    """Apply an explicit administrator merge with the same atomic data checks."""
    try:
        with get_hosting_db_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = _merge(conn, source_account_id, target_account_id, admin_id)
            conn.execute(
                "UPDATE hosting_account_merge_requests SET status='cancelled' WHERE status IN ('pending','approved') "
                "AND (source_account_id IN (?,?) OR target_account_id IN (?,?))",
                (source_account_id, target_account_id, source_account_id, target_account_id),
            )
            conn.commit()
            return dict(result, admin_initiated=True)
    except sqlite3.IntegrityError as error:
        raise ValueError("The accounts have conflicting data. Nothing was merged.") from error
