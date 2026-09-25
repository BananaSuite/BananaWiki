"""Managed instance feature entitlements and request lifecycle."""

import sqlite3
from datetime import datetime, timezone

from ._connection import get_hosting_db_context


FEATURE_ENTITLEMENT_COLUMNS = {
    "public_access": "public_wiki_allowed",
    "page_builder": "page_builder_allowed",
}
FEATURE_LABELS = {
    "public_access": "Public access",
    "page_builder": "Page builder",
}
FEATURE_AUTO_APPROVAL_SETTINGS = {
    "public_access": "auto_approve_public_access_requests",
    "page_builder": "auto_approve_page_builder_requests",
}
AUTOMATIC_APPROVAL_NOTE = (
    "Automatically approved because hosting policy permits this feature request."
)


def _feature_column(feature):
    try:
        return FEATURE_ENTITLEMENT_COLUMNS[feature]
    except KeyError as exc:
        raise ValueError("invalid_feature") from exc


def _as_dict(row):
    return dict(row) if row is not None else None


def create_instance_feature_request(instance_id, requested_by, feature, reason):
    """Create a request and apply any configured automatic-approval policy."""
    column = _feature_column(feature)
    auto_approval_setting = FEATURE_AUTO_APPROVAL_SETTINGS[feature]
    reason = (reason or "").strip()
    if not 20 <= len(reason) <= 2000:
        raise ValueError("invalid_reason")

    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        instance = conn.execute(
            f"SELECT id, account_id, status, {column} AS allowed FROM instances WHERE id=?",  # noqa: S608
            (instance_id,),
        ).fetchone()
        if instance is None or instance["account_id"] != requested_by:
            conn.rollback()
            raise ValueError("not_owner")
        if instance["status"] == "terminated":
            conn.rollback()
            raise ValueError("invalid_instance_status")
        if instance["allowed"]:
            conn.rollback()
            raise ValueError("already_allowed")
        pending = conn.execute(
            "SELECT 1 FROM instance_feature_requests "
            "WHERE instance_id=? AND feature=? AND status='pending'",
            (instance_id, feature),
        ).fetchone()
        if pending:
            conn.rollback()
            raise ValueError("pending_exists")
        settings = conn.execute(
            f"SELECT {auto_approval_setting} AS enabled FROM hosting_settings WHERE id=1"  # noqa: S608
        ).fetchone()
        automatically_approved = bool(settings and settings["enabled"])
        try:
            if automatically_approved:
                cursor = conn.execute(
                    "INSERT INTO instance_feature_requests "
                    "(instance_id, feature, requested_by, reason, status, "
                    "reviewed_by, reviewed_at, review_note, review_source) "
                    "VALUES (?, ?, ?, ?, 'approved', NULL, ?, ?, 'automatic')",
                    (
                        instance_id,
                        feature,
                        requested_by,
                        reason,
                        datetime.now(timezone.utc).isoformat(),
                        AUTOMATIC_APPROVAL_NOTE,
                    ),
                )
                conn.execute(
                    f"UPDATE instances SET {column}=1 WHERE id=?",  # noqa: S608
                    (instance_id,),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO instance_feature_requests "
                    "(instance_id, feature, requested_by, reason) VALUES (?, ?, ?, ?)",
                    (instance_id, feature, requested_by, reason),
                )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError("pending_exists") from exc
            raise
        conn.commit()
        row = conn.execute(
            "SELECT * FROM instance_feature_requests WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
    return _as_dict(row)


def get_instance_feature_request(request_id):
    """Return one request with owner/reviewer context, or ``None``."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            """SELECT r.*, i.account_id AS owner_account_id, i.status AS instance_status,
                      i.subdomain, requester.username AS requested_by_username,
                      reviewer.username AS reviewed_by_username
               FROM instance_feature_requests r
               JOIN instances i ON i.id=r.instance_id
               JOIN accounts requester ON requester.id=r.requested_by
               LEFT JOIN accounts reviewer ON reviewer.id=r.reviewed_by
               WHERE r.id=?""",
            (request_id,),
        ).fetchone()
    return _as_dict(row)


def list_instance_feature_requests(instance_id):
    """Return the complete newest-first request and decision history."""
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            """SELECT r.*, requester.username AS requested_by_username,
                      reviewer.username AS reviewed_by_username
               FROM instance_feature_requests r
               JOIN accounts requester ON requester.id=r.requested_by
               LEFT JOIN accounts reviewer ON reviewer.id=r.reviewed_by
               WHERE r.instance_id=? ORDER BY r.requested_at DESC, r.id DESC""",
            (instance_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_pending_instance_feature_requests():
    """Return the platform review queue, oldest request first."""
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            """SELECT r.*, i.subdomain, i.account_id AS owner_account_id,
                      a.username AS owner_username
               FROM instance_feature_requests r
               JOIN instances i ON i.id=r.instance_id
               JOIN accounts a ON a.id=i.account_id
               WHERE r.status='pending'
               ORDER BY r.requested_at, r.id"""
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_instance_feature_request(request_id, account_id):
    """Cancel a pending request when *account_id* still owns its instance."""
    now = datetime.now(timezone.utc).isoformat()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = conn.execute(
            """UPDATE instance_feature_requests SET status='cancelled', cancelled_at=?
               WHERE id=? AND status='pending' AND instance_id IN
                   (SELECT id FROM instances WHERE account_id=?)""",
            (now, request_id, account_id),
        )
        if result.rowcount != 1:
            conn.rollback()
            raise ValueError("not_pending_or_not_owner")
        conn.commit()
    return True


def review_instance_feature_request(request_id, reviewer_id, decision, review_note=""):
    """Approve or deny one pending request as a platform administrator."""
    if decision not in {"approved", "denied"}:
        raise ValueError("invalid_decision")
    review_note = (review_note or "").strip()
    if len(review_note) > 2000:
        raise ValueError("invalid_review_note")
    now = datetime.now(timezone.utc).isoformat()

    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        reviewer = conn.execute(
            "SELECT is_admin FROM accounts WHERE id=?", (reviewer_id,)
        ).fetchone()
        if reviewer is None or not reviewer["is_admin"]:
            conn.rollback()
            raise ValueError("not_admin")
        feature_request = conn.execute(
            "SELECT r.*, i.status AS instance_status "
            "FROM instance_feature_requests r "
            "JOIN instances i ON i.id=r.instance_id WHERE r.id=?",
            (request_id,),
        ).fetchone()
        if feature_request is None:
            conn.rollback()
            raise ValueError("not_found")
        if feature_request["status"] != "pending":
            conn.rollback()
            raise ValueError("not_pending")
        if decision == "approved" and feature_request["instance_status"] == "terminated":
            conn.rollback()
            raise ValueError("invalid_instance_status")
        result = conn.execute(
            """UPDATE instance_feature_requests
               SET status=?, reviewed_by=?, review_note=?, review_source='manual', reviewed_at=?
               WHERE id=? AND status='pending'""",
            (decision, reviewer_id, review_note, now, request_id),
        )
        if result.rowcount != 1:
            conn.rollback()
            raise ValueError("not_pending")
        if decision == "approved":
            column = _feature_column(feature_request["feature"])
            conn.execute(
                f"UPDATE instances SET {column}=1 WHERE id=?",  # noqa: S608
                (feature_request["instance_id"],),
            )
        conn.commit()
    return get_instance_feature_request(request_id)


def set_instance_feature_entitlement(instance_id, feature, allowed):
    """Directly grant or revoke an entitlement without altering request history."""
    column = _feature_column(feature)
    with get_hosting_db_context() as conn:
        result = conn.execute(
            f"UPDATE instances SET {column}=? WHERE id=? AND status != 'terminated'",  # noqa: S608
            (1 if allowed else 0, instance_id),
        )
        conn.commit()
    return result.rowcount == 1
