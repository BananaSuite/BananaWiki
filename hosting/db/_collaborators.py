"""Instance collaborator and ownership transfer management."""

import json
import logging
from datetime import datetime, timezone

from ._connection import get_hosting_db_context

logger = logging.getLogger("hosting.db.collaborators")

# Every permission a collaborator can hold on a hosted wiki instance.
# ``full_access`` role grants all of these implicitly.
COLLABORATOR_PERMISSIONS = (
    "view",                  # view instance details, status, storage, users
    "start_stop",            # pause / resume the instance
    "terminate",             # terminate (delete) the instance
    "reset_password",        # reset the wiki admin password
    "reset_wiki",            # factory-reset wiki content
    "download",              # download instance data archive
    "toggle_mode",           # switch EasyWiki / full mode
    "manage_collaborators",  # add / remove / edit other collaborators
    "use_case",              # update the declared use-case
    "analytics",             # view instance analytics
)

ALL_PERMISSIONS = list(COLLABORATOR_PERMISSIONS)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def add_collaborator(instance_id, account_id, invited_by, role="custom", permissions=None):
    """Add a collaborator to an instance.

    ``role`` is ``'full_access'`` (all permissions) or ``'custom'``
    (only the permissions listed in *permissions*).

    Returns the new row as a dict, or ``None`` if the collaborator
    already exists.
    """
    if role not in ("full_access", "custom"):
        raise ValueError("invalid_role")
    perms = permissions or []
    # Validate permission strings
    perms = [p for p in perms if p in COLLABORATOR_PERMISSIONS]
    perms_json = json.dumps(sorted(set(perms)))
    with get_hosting_db_context() as conn:
        # Prevent adding the instance owner as a collaborator
        owner_row = conn.execute(
            "SELECT account_id FROM instances WHERE id=?", (instance_id,)
        ).fetchone()
        if owner_row and owner_row["account_id"] == account_id:
            return None
        try:
            conn.execute(
                "INSERT INTO instance_collaborators "
                "(instance_id, account_id, role, permissions, invited_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (instance_id, account_id, role, perms_json, invited_by, _now_iso()),
            )
            conn.commit()
        except Exception:
            # UNIQUE constraint violation: already a collaborator
            return None
        row = conn.execute(
            "SELECT * FROM instance_collaborators "
            "WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        ).fetchone()
    return dict(row) if row else None


def remove_collaborator(instance_id, account_id):
    """Remove a collaborator from an instance.  Returns ``True`` on success."""
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "DELETE FROM instance_collaborators WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        )
        conn.commit()
    return result.rowcount == 1


def update_collaborator(instance_id, account_id, role=None, permissions=None):
    """Update a collaborator's role and/or permissions.

    Returns ``True`` on success, ``False`` if the collaborator does not
    exist.
    """
    with get_hosting_db_context() as conn:
        existing = conn.execute(
            "SELECT * FROM instance_collaborators WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        ).fetchone()
        if not existing:
            return False
        new_role = role if role in ("full_access", "custom") else existing["role"]
        if permissions is not None:
            perms = [p for p in permissions if p in COLLABORATOR_PERMISSIONS]
            perms_json = json.dumps(sorted(set(perms)))
        else:
            perms_json = existing["permissions"]
        conn.execute(
            "UPDATE instance_collaborators SET role=?, permissions=? "
            "WHERE instance_id=? AND account_id=?",
            (new_role, perms_json, instance_id, account_id),
        )
        conn.commit()
    return True


def get_collaborator(instance_id, account_id):
    """Return a single collaborator row, or ``None``."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT ic.*, a.username AS account_username "
            "FROM instance_collaborators ic "
            "JOIN accounts a ON ic.account_id = a.id "
            "WHERE ic.instance_id=? AND ic.account_id=?",
            (instance_id, account_id),
        ).fetchone()
    return dict(row) if row else None


def get_collaborators_for_instance(instance_id):
    """Return all collaborators for an instance, with account usernames."""
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT ic.*, a.username AS account_username "
            "FROM instance_collaborators ic "
            "JOIN accounts a ON ic.account_id = a.id "
            "WHERE ic.instance_id=? "
            "ORDER BY ic.created_at ASC",
            (instance_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_shared_instances_for_account(account_id):
    """Return all instances where *account_id* is a collaborator (not owner).

    Each row includes the owner's username for display.
    """
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT i.*, ic.role AS collab_role, ic.permissions AS collab_permissions, "
            "       a.username AS owner_username "
            "FROM instance_collaborators ic "
            "JOIN instances i ON ic.instance_id = i.id "
            "JOIN accounts a ON i.account_id = a.id "
            "WHERE ic.account_id=? AND i.status != 'terminated' "
            "ORDER BY i.created_at DESC",
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def collaborator_has_permission(instance_id, account_id, permission):
    """Return ``True`` if *account_id* is a collaborator on *instance_id*
    and holds *permission*.

    ``full_access`` collaborators implicitly hold every permission.
    """
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT role, permissions FROM instance_collaborators "
            "WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        ).fetchone()
    if not row:
        return False
    if row["role"] == "full_access":
        return True
    try:
        perms = json.loads(row["permissions"] or "[]")
    except (json.JSONDecodeError, TypeError):
        perms = []
    return permission in perms


def is_collaborator(instance_id, account_id):
    """Return ``True`` if *account_id* is a collaborator on *instance_id*."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT 1 FROM instance_collaborators WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        ).fetchone()
    return row is not None


def count_collaborators(instance_id):
    """Return the number of collaborators on an instance."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM instance_collaborators WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    return row[0] if row else 0


def remove_all_collaborators(instance_id):
    """Remove all collaborators from an instance (e.g. on termination)."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "DELETE FROM instance_collaborators WHERE instance_id=?",
            (instance_id,),
        )
        conn.commit()


def create_ownership_transfer(instance_id, from_account_id, to_account_id):
    """Create a pending ownership transfer request.

    Returns the new row as a dict, or ``None`` if a pending transfer
    already exists for this instance.
    """
    with get_hosting_db_context() as conn:
        # Cancel any existing pending transfers for this instance
        conn.execute(
            "UPDATE instance_ownership_transfers "
            "SET status='cancelled', resolved_at=? "
            "WHERE instance_id=? AND status='pending'",
            (_now_iso(), instance_id),
        )
        conn.execute(
            "INSERT INTO instance_ownership_transfers "
            "(instance_id, from_account_id, to_account_id, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (instance_id, from_account_id, to_account_id, _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM instance_ownership_transfers "
            "WHERE instance_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


def get_pending_transfer(instance_id):
    """Return the pending transfer for an instance, or ``None``."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT t.*, "
            "  fa.username AS from_username, "
            "  ta.username AS to_username "
            "FROM instance_ownership_transfers t "
            "JOIN accounts fa ON t.from_account_id = fa.id "
            "JOIN accounts ta ON t.to_account_id = ta.id "
            "WHERE t.instance_id=? AND t.status='pending' "
            "ORDER BY t.id DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
    return dict(row) if row else None


def get_pending_transfers_for_account(account_id):
    """Return all pending ownership transfers where *account_id* is the recipient."""
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT t.*, "
            "  fa.username AS from_username, "
            "  ta.username AS to_username, "
            "  i.subdomain AS instance_subdomain, "
            "  i.domain_mode AS instance_domain_mode "
            "FROM instance_ownership_transfers t "
            "JOIN accounts fa ON t.from_account_id = fa.id "
            "JOIN accounts ta ON t.to_account_id = ta.id "
            "JOIN instances i ON t.instance_id = i.id "
            "WHERE t.to_account_id=? AND t.status='pending' "
            "ORDER BY t.created_at DESC",
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def accept_ownership_transfer(transfer_id, account_id):
    """Accept an ownership transfer.

    The current owner becomes a ``full_access`` collaborator on the
    instance.  Returns ``(True, instance_id)`` on success, or
    ``(False, reason)`` on failure.
    """
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM instance_ownership_transfers WHERE id=? AND status='pending'",
            (transfer_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "transfer_not_found"
        if row["to_account_id"] != account_id:
            conn.rollback()
            return False, "not_recipient"
        instance_id = row["instance_id"]
        old_owner_id = row["from_account_id"]

        # Verify instance still exists and is owned by the sender
        inst = conn.execute(
            "SELECT * FROM instances WHERE id=?", (instance_id,)
        ).fetchone()
        if not inst:
            conn.rollback()
            return False, "instance_not_found"
        if inst["account_id"] != old_owner_id:
            conn.rollback()
            return False, "owner_changed"

        # Transfer ownership
        conn.execute(
            "UPDATE instances SET account_id=? WHERE id=?",
            (account_id, instance_id),
        )

        # Remove new owner from collaborators if they were one
        conn.execute(
            "DELETE FROM instance_collaborators WHERE instance_id=? AND account_id=?",
            (instance_id, account_id),
        )

        # Add old owner as full_access collaborator
        conn.execute(
            "INSERT OR IGNORE INTO instance_collaborators "
            "(instance_id, account_id, role, permissions, invited_by, created_at) "
            "VALUES (?, ?, 'full_access', '[]', ?, ?)",
            (instance_id, old_owner_id, account_id, _now_iso()),
        )

        # Mark transfer as accepted
        conn.execute(
            "UPDATE instance_ownership_transfers SET status='accepted', resolved_at=? WHERE id=?",
            (_now_iso(), transfer_id),
        )
        conn.commit()
    return True, instance_id


def cancel_ownership_transfer(transfer_id, account_id):
    """Cancel a pending ownership transfer.

    Only the sender (current owner) can cancel.  Returns ``True``
    on success.
    """
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instance_ownership_transfers "
            "SET status='cancelled', resolved_at=? "
            "WHERE id=? AND from_account_id=? AND status='pending'",
            (_now_iso(), transfer_id, account_id),
        )
        conn.commit()
    return result.rowcount == 1


def decline_ownership_transfer(transfer_id, account_id):
    """Decline a pending ownership transfer.

    Only the recipient can decline.  Returns ``True`` on success.
    """
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instance_ownership_transfers "
            "SET status='declined', resolved_at=? "
            "WHERE id=? AND to_account_id=? AND status='pending'",
            (_now_iso(), transfer_id, account_id),
        )
        conn.commit()
    return result.rowcount == 1
