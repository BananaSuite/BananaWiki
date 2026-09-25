"""Instance management for the hosting platform."""

import logging
import secrets
import socket
import string
from datetime import datetime, timezone, timedelta

from .. import config
from ._connection import get_hosting_db_context

logger = logging.getLogger("hosting.db.instances")


def _gen_instance_id():
    """Generate a random 16-character alphanumeric instance ID."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _is_port_free(port):
    """Return True if *port* is not currently bound on any interface.

    Performs a real OS-level probe so that ports already held by other
    processes (e.g. X11 on 6000/6001, system services, etc.) are skipped
    during allocation rather than handed to Gunicorn which would then fail
    to bind and die silently.

    Checks both ``0.0.0.0`` and ``127.0.0.1`` because a process may bind
    exclusively to the loopback interface, making the port free on the
    wildcard but still unavailable for Gunicorn.
    """
    for bind_addr in ("0.0.0.0", "127.0.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((bind_addr, port))
        except OSError:
            return False
    return True


def _allocate_port(conn):
    """Find the lowest unused *and* OS-free port in the configured range."""
    used = {
        row[0]
        for row in conn.execute(
            "SELECT port FROM instances WHERE status IN ('running', 'stopped') AND port IS NOT NULL"
        ).fetchall()
    }
    for port in range(config.INSTANCE_PORT_START, config.INSTANCE_PORT_END):
        if port not in used and _is_port_free(port):
            return port
    return None


def create_instance(
    account_id,
    subdomain,
    admin_username=None,
    admin_password=None,
    domain_mode="hosting",
    custom_credentials=False,
    easy_wiki=False,
    declared_use_case="",
):
    """Provision a new instance and return its row as a dict, or ``None`` on failure.

    ``domain_mode`` controls the public URL format:

    * ``"hosting"`` (default): ``{slug}-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``.
      Available to any account.
    * ``"apex"``: ``{slug}.{BASE_DOMAIN}`` (e.g. ``wiki.example.com``).
      Should only be passed in for admin accounts: routes enforce this.

    The same *subdomain* string can be claimed in both modes, e.g.
    ``wiki`` in apex mode (``wiki.example.com``) and ``wiki`` in hosting
    mode (``wiki-hosting.example.com``) are independent rows.  Uniqueness
    is enforced on the ``(subdomain, domain_mode)`` pair.
    """
    if domain_mode not in ("hosting", "apex"):
        raise ValueError("invalid_domain_mode")
    with get_hosting_db_context() as conn:
        # Acquire an exclusive write lock before reading used ports so that
        # two concurrent requests cannot both allocate the same port.
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """SELECT 1 FROM instances
               WHERE subdomain=? COLLATE NOCASE
                 AND domain_mode=?
                 AND status != 'terminated'""",
            (subdomain, domain_mode),
        ).fetchone()
        if existing:
            conn.rollback()
            raise ValueError("subdomain_in_use")

        port = _allocate_port(conn)
        if port is None:
            logger.error(
                "Port exhaustion: no free port found in range %d-%d for new instance",
                config.INSTANCE_PORT_START,
                config.INSTANCE_PORT_END,
            )
            conn.rollback()
            return None

        iid = _gen_instance_id()
        while conn.execute("SELECT 1 FROM instances WHERE id=?", (iid,)).fetchone():
            iid = _gen_instance_id()

        now = datetime.now(timezone.utc)
        # Apex instances (``<slug>.BASE_DOMAIN``) are admin-provisioned
        # for production / vanity domains and must NEVER auto-expire
        # after the default 14-day trial window: setting expires_at to
        # NULL signals "perpetual / no scheduled expiry" everywhere in
        # the hosting platform (UI and cleanup scheduler).
        # Hosting-mode instances continue to use the default trial
        # duration.
        if domain_mode == "apex":
            expires_iso = None
        else:
            expires = now + timedelta(days=config.INSTANCE_DURATION_DAYS)
            expires_iso = expires.isoformat()

        conn.execute(
            """INSERT INTO instances
               (id, account_id, subdomain, status, port,
                admin_username, admin_password_plain,
                storage_limit_mb,
                created_at, expires_at, domain_mode, custom_credentials, easy_wiki,
                declared_use_case, tos_compliance_declared_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
            (iid, account_id, subdomain, port,
             admin_username, admin_password,
             now.isoformat(), expires_iso, domain_mode, 1 if custom_credentials else 0,
             1 if easy_wiki else 0, declared_use_case, now.isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    return dict(row) if row else None


def clear_instance_password(instance_id):
    """Erase the stored plaintext admin password for an instance.

    Called after the password has been displayed to the user so that it
    is not kept in the database longer than necessary.
    """
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET admin_password_plain=NULL WHERE id=?",
            (instance_id,),
        )
        conn.commit()


def get_instance(instance_id):
    """Return an instance row by ID, or ``None``."""
    with get_hosting_db_context() as conn:
        return conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()


def get_instance_by_subdomain(subdomain, *, domain_mode=None):
    """Return an instance row by subdomain, or ``None``.

    When *domain_mode* is ``None`` (the default), the search is not
    filtered by mode: used by callers (admin restore, terminated
    name reuse, etc.) that only care about the slug.  Pass
    ``domain_mode="hosting"`` or ``domain_mode="apex"`` to disambiguate
    when both modes can legitimately have a row with the same slug, as
    the subdomain reverse-proxy middleware does on every request.
    """
    with get_hosting_db_context() as conn:
        if domain_mode is None:
            return conn.execute(
                "SELECT * FROM instances WHERE subdomain=? COLLATE NOCASE AND status != 'terminated'",
                (subdomain,),
            ).fetchone()
        return conn.execute(
            """SELECT * FROM instances
               WHERE subdomain=? COLLATE NOCASE
                 AND domain_mode=?
                 AND status != 'terminated'""",
            (subdomain, domain_mode),
        ).fetchone()


def get_apex_instance_by_subdomain(subdomain):
    """Return the apex-mode instance for *subdomain*, or ``None``.

    Used by the subdomain reverse proxy to route ``{slug}.{BASE_DOMAIN}``
    traffic (without the hosting suffix) to admin-claimed instances.
    """
    with get_hosting_db_context() as conn:
        return conn.execute(
            """SELECT * FROM instances
               WHERE subdomain=? COLLATE NOCASE
                 AND status != 'terminated'
                 AND domain_mode = 'apex'""",
            (subdomain,),
        ).fetchone()


def _subdomain_in_use(conn, subdomain, domain_mode, *, exclude_instance_id=None):
    query = """SELECT 1 FROM instances
               WHERE subdomain=? COLLATE NOCASE
                 AND domain_mode=?
                 AND status != 'terminated'"""
    params = [subdomain, domain_mode]
    if exclude_instance_id is not None:
        query += " AND id != ?"
        params.append(exclude_instance_id)
    return conn.execute(query, params).fetchone() is not None


def _candidate_with_suffix(base, suffix_number):
    suffix = f"-{suffix_number}"
    max_base_len = max(config.SUBDOMAIN_MIN_LENGTH, config.SUBDOMAIN_MAX_LENGTH - len(suffix))
    return f"{base[:max_base_len].rstrip('-')}{suffix}"


def find_available_instance_subdomain(base_subdomain, domain_mode, *, exclude_instance_id=None):
    """Return an available slug in *domain_mode*, trying ``base``, ``base-2`` ...

    The returned value is guaranteed not to collide with another active
    instance in the same namespace.  Terminated archived rows are ignored
    just like the create path ignores them.
    """
    if domain_mode not in ("hosting", "apex"):
        raise ValueError("invalid_domain_mode")
    base = (base_subdomain or "").strip().lower()
    with get_hosting_db_context() as conn:
        if not _subdomain_in_use(
            conn, base, domain_mode, exclude_instance_id=exclude_instance_id,
        ):
            return base
        for suffix_number in range(2, 10000):
            candidate = _candidate_with_suffix(base, suffix_number)
            if not _subdomain_in_use(
                conn, candidate, domain_mode, exclude_instance_id=exclude_instance_id,
            ):
                return candidate
    raise ValueError("no_available_subdomain")


def update_instance_identity(instance_id, subdomain, domain_mode):
    """Update an instance's slug and URL mode.

    Raises ``ValueError("subdomain_in_use")`` when another active row
    already owns the requested ``(subdomain, domain_mode)`` pair.
    """
    if domain_mode not in ("hosting", "apex"):
        raise ValueError("invalid_domain_mode")
    subdomain = (subdomain or "").strip().lower()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        if _subdomain_in_use(
            conn, subdomain, domain_mode, exclude_instance_id=instance_id,
        ):
            conn.rollback()
            raise ValueError("subdomain_in_use")
        conn.execute(
            "UPDATE instances SET subdomain=?, domain_mode=? WHERE id=?",
            (subdomain, domain_mode, instance_id),
        )
        conn.commit()
    return True


def convert_apex_instances_to_hosting(account_id):
    """Convert all of *account_id*'s apex-mode instances back to hosting mode.

    Called when an admin account is demoted: apex domains (e.g.
    ``wiki.example.com``) are an admin-only privilege, so the instances
    must fall back to the standard ``{slug}-{suffix}.{BASE_DOMAIN}`` URL
    format.  If the natural hosting slug is already taken, the instance
    is renamed to the next available ``slug-2``, ``slug-3`` ... value.

    Returns ``[(instance_id, old_subdomain, new_subdomain), ...]``.
    """
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            """SELECT id, subdomain FROM instances
               WHERE account_id=? AND domain_mode='apex'
                 AND status != 'terminated'""",
            (account_id,),
        ).fetchall()
        converted = []
        for row in rows:
            new_subdomain = row["subdomain"]
            if _subdomain_in_use(
                conn, new_subdomain, "hosting", exclude_instance_id=row["id"],
            ):
                for suffix_number in range(2, 10000):
                    candidate = _candidate_with_suffix(row["subdomain"], suffix_number)
                    if not _subdomain_in_use(
                        conn, candidate, "hosting", exclude_instance_id=row["id"],
                    ):
                        new_subdomain = candidate
                        break
                else:
                    raise ValueError("no_available_subdomain")
            conn.execute(
                "UPDATE instances SET subdomain=?, domain_mode='hosting' WHERE id=?",
                (new_subdomain, row["id"]),
            )
            converted.append((row["id"], row["subdomain"], new_subdomain))
        conn.commit()
    return converted


def get_instances_for_account(account_id):
    """Return all non-terminated instances for an account."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM instances WHERE account_id=? AND status != 'terminated' ORDER BY created_at DESC",
            (account_id,),
        ).fetchall()


def count_active_instances(account_id):
    """Return the number of running or stopped instances for an account."""
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM instances WHERE account_id=? AND status IN ('running', 'stopped')",
            (account_id,),
        ).fetchone()
    return row[0] if row else 0


_STATUS_QUERIES = {
    "stopped": (
        "UPDATE instances SET status=?, stopped_at=? WHERE id=?",
        lambda now: ["stopped", now],
    ),
    "running": (
        "UPDATE instances SET status=?, stopped_at=NULL WHERE id=?",
        lambda now: ["running"],
    ),
    "suspended": (
        "UPDATE instances SET status=?, stopped_at=? WHERE id=?",
        lambda now: ["suspended", now],
    ),
    "terminated": (
        "UPDATE instances SET status=?, terminated_at=?, port=NULL WHERE id=?",
        lambda now: ["terminated", now],
    ),
}


def update_instance_status(instance_id, new_status, expected_status=None):
    """Update the status of an instance.  Returns ``True`` on success.

    With *expected_status* the update is a compare-and-set: it only
    happens while the row still has that status, and ``False`` means
    something else changed the instance first.
    """
    if new_status not in _STATUS_QUERIES:
        raise ValueError(f"Invalid status: {new_status}")
    query, build_params = _STATUS_QUERIES[new_status]
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        params = build_params(now) + [instance_id]
        if expected_status is not None:
            query += " AND status=?"
            params.append(expected_status)
        cur = conn.execute(query, params)
        conn.commit()
    return expected_status is None or cur.rowcount > 0


def terminate_instance(instance_id):
    """Mark an instance as terminated."""
    return update_instance_status(instance_id, "terminated")


def set_instance_upload_policy(instance_id, upload_max_size_mb=None, upload_blocked_extensions=None):
    """Set per-instance upload policy overrides.

    ``upload_max_size_mb=None`` means inherit the platform default.  The
    blocked-extension list is additive with the platform-wide blocklist.
    """
    value = None if upload_max_size_mb is None else int(upload_max_size_mb)
    blocked = (upload_blocked_extensions or "").strip()
    with get_hosting_db_context() as conn:
        result = conn.execute(
            """UPDATE instances
               SET upload_max_size_mb=?, upload_blocked_extensions=?
               WHERE id=? AND status != 'terminated'""",
            (value, blocked, instance_id),
        )
        conn.commit()
    return result.rowcount == 1


def _build_terminated_subdomain(instance_id, original_subdomain):
    """Return the archived subdomain value for a terminated instance."""
    return f"{(original_subdomain or '').strip().lower()}--terminated-{instance_id[:8]}"


def archive_terminated_subdomain(instance_id, original_subdomain):
    """Rename a terminated instance's subdomain so the original name can be reused."""
    archived = _build_terminated_subdomain(instance_id, original_subdomain)
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instances SET subdomain=? WHERE id=? AND status='terminated'",
            (archived, instance_id),
        )
        conn.commit()
    return archived if result.rowcount == 1 else None


def terminate_and_archive_instance(
    instance_id,
    original_subdomain,
    *,
    grace_period_days=None,
    reason=None,
):
    """Terminate an instance, archive its subdomain, and start the grace period.

    When ``grace_period_days`` is a positive integer, ``data_retained_until``
    is set to ``now + grace_period_days``.  Pass ``0`` (or a negative value)
    to disable retention so the data is hard-deleted immediately by the
    caller.  ``reason`` is recorded as ``terminated_reason`` (typically
    ``'manual'`` or ``'expired'``).
    """
    archived = _build_terminated_subdomain(instance_id, original_subdomain)
    with get_hosting_db_context() as conn:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        retained_until = None
        if grace_period_days is not None and grace_period_days > 0:
            retained_until = (now_dt + timedelta(days=int(grace_period_days))).isoformat()
        result = conn.execute(
            """UPDATE instances
               SET status=?, terminated_at=?, port=NULL, subdomain=?,
                   data_retained_until=?, terminated_reason=?
               WHERE id=? AND status != 'terminated'""",
            (
                "terminated", now, archived,
                retained_until, reason, instance_id,
            ),
        )
        conn.commit()
    return (result.rowcount == 1), archived


def clear_instance_data_retention(instance_id):
    """Clear ``data_retained_until`` and ``terminated_reason`` for an instance.

    Called after the data dir has been hard-deleted (so the periodic cleanup
    does not retry) or when an instance is restored from grace period.
    """
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET data_retained_until=NULL, terminated_reason=NULL WHERE id=?",
            (instance_id,),
        )
        conn.commit()


def get_instances_with_expired_grace_period():
    """Return terminated instances whose ``data_retained_until`` has passed.

    These rows have data still on disk that should now be hard-deleted by the
    periodic cleanup task.
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            """SELECT * FROM instances
               WHERE status='terminated'
                 AND data_retained_until IS NOT NULL
                 AND data_retained_until <= ?""",
            (now,),
        ).fetchall()


def restore_instance_in_place(instance_id, target_subdomain, expires_at):
    """Atomically flip a terminated instance back to ``running``.

    Inside a single ``BEGIN IMMEDIATE`` transaction this:

    * verifies that ``target_subdomain`` is not in use by any other
      instance **in the same ``domain_mode``** (apex and hosting
      namespaces are independent, mirroring ``create_instance``),
    * allocates a free port,
    * resets the row's status, subdomain, port, and ``expires_at`` and
      clears the grace-period bookkeeping (``data_retained_until``,
      ``terminated_reason``, ``terminated_at``).

    Returns ``(True, port, None)`` on success, or
    ``(False, None, reason)`` on failure.  ``reason`` is one of
    ``'subdomain_in_use'``, ``'no_free_port'``, or ``'not_terminated'``.
    """
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Look up the row's own ``domain_mode`` so the clash check stays
        # scoped to the same namespace.  A row archived in apex mode
        # must only collide with other apex rows, never with
        # hosting-mode rows that share the slug.
        own_mode_row = conn.execute(
            "SELECT domain_mode FROM instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        own_mode = (
            own_mode_row["domain_mode"] if own_mode_row else "hosting"
        ) or "hosting"
        clash = conn.execute(
            """SELECT 1 FROM instances
               WHERE subdomain=? COLLATE NOCASE
                 AND domain_mode=?
                 AND id != ?""",
            (target_subdomain, own_mode, instance_id),
        ).fetchone()
        if clash:
            conn.rollback()
            return False, None, "subdomain_in_use"
        port = _allocate_port(conn)
        if port is None:
            conn.rollback()
            return False, None, "no_free_port"
        result = conn.execute(
            """UPDATE instances
               SET status='running',
                   subdomain=?,
                   port=?,
                   expires_at=?,
                   stopped_at=NULL,
                   terminated_at=NULL,
                   data_retained_until=NULL,
                   terminated_reason=NULL
               WHERE id=? AND status='terminated'""",
            (target_subdomain, port, expires_at, instance_id),
        )
        if result.rowcount != 1:
            conn.rollback()
            return False, None, "not_terminated"
        conn.commit()
    return True, port, None


def get_all_active_instances():
    """Return all running or stopped instances across the platform."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            "SELECT * FROM instances WHERE status IN ('running', 'stopped')"
        ).fetchall()


def get_all_instances():
    """Return all instances across the platform with account info."""
    with get_hosting_db_context() as conn:
        return conn.execute(
            """SELECT i.*, a.username AS account_username, a.is_admin AS account_is_admin
               FROM instances i
               JOIN accounts a ON i.account_id = a.id
               ORDER BY i.created_at DESC"""
        ).fetchall()


def get_expired_instances():
    """Return running/stopped instances whose expiry has passed.

    Suspended instances are intentionally excluded: per the suspension
    lockdown contract their expiration timer freezes while they are
    suspended.  The expiry sweeper must NOT auto-terminate them:
    that would silently steal trial days from the user.  Once an admin
    unsuspends the instance, the expiry is shifted forward by the time
    spent suspended and normal expiry handling resumes.
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        return conn.execute(
            "SELECT * FROM instances WHERE status IN ('running', 'stopped') "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()


def is_instance_suspension_active(inst):
    """Return True if the instance is currently suspended.

    Handles both permanent suspensions (``suspended_until`` is NULL) and timed
    suspensions (``suspended_until`` is a future UTC ISO datetime).  If the
    suspension has expired this function returns False but does **not** modify
    the database, call :func:`check_instance_suspension_expired` to auto-unsuspend.
    """
    if not inst or inst.get("status") != "suspended":
        return False
    suspended_until = inst.get("suspended_until")
    if not suspended_until:
        return True
    try:
        expiry = datetime.fromisoformat(suspended_until.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    return datetime.now(timezone.utc) < expiry


def check_instance_suspension_expired(instance_id):
    """Auto-unsuspend an instance whose timed suspension has elapsed.

    Returns True if the instance was unsuspended, False otherwise.
    """
    from ..instance_manager import unsuspend_instance
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT status, suspended_until FROM instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        if not row or row["status"] != "suspended":
            return False
        suspended_until = row["suspended_until"]
        if not suspended_until:
            return False
        try:
            expiry = datetime.fromisoformat(suspended_until.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        if datetime.now(timezone.utc) >= expiry:
            ok, _ = unsuspend_instance(instance_id)
            return ok
        return False


def get_expired_instance_suspensions():
    """Return instance IDs whose timed suspension has elapsed.

    These instances have ``status = 'suspended'`` with a non-NULL
    ``suspended_until`` that is in the past.  Permanent suspensions
    (``suspended_until IS NULL``) are never returned.
    """
    with get_hosting_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            "SELECT id FROM instances WHERE status = 'suspended' "
            "AND suspended_until IS NOT NULL AND suspended_until <= ?",
            (now,),
        ).fetchall()
    return [r["id"] for r in rows]


def record_instance_suspension_action(
    instance_id, action, performed_by=None,
    reason=None, reason_visible=False,
    time_visible=False, duration=None,
    suspended_until=None,
):
    """Record a suspend or unsuspend action for an instance in the audit log."""
    with get_hosting_db_context() as conn:
        conn.execute(
            "INSERT INTO instance_suspension_audit "
            "(instance_id, action, reason, reason_visible, time_visible, "
            " duration, suspended_until, performed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (instance_id, action, reason, int(reason_visible),
             int(time_visible), duration, suspended_until, performed_by),
        )
        from ._events import record_event
        record_event(conn, "instance", instance_id, "instance." + action, performed_by, reason)
        conn.commit()


def get_instance_suspension_history(instance_id):
    """Return the suspension audit log for an instance, newest first."""
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT isa.*, a.username AS performed_by_username "
            "FROM instance_suspension_audit isa "
            "LEFT JOIN accounts a ON isa.performed_by = a.id "
            "WHERE isa.instance_id=? ORDER BY isa.created_at DESC, isa.id DESC",
            (instance_id,),
        ).fetchall()
    return rows


def delete_instance(instance_id):
    """Hard-delete an instance row from the database."""
    with get_hosting_db_context() as conn:
        conn.execute("DELETE FROM instances WHERE id=?", (instance_id,))
        conn.commit()


def transfer_instance_ownership(instance_id, account_id):
    """Move an instance to a different hosting account.

    Returns ``True`` when the row was updated, ``False`` when the instance
    does not exist.
    """
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instances SET account_id=? WHERE id=?",
            (account_id, instance_id),
        )
        conn.commit()
    return result.rowcount == 1


def suspend_grace_period_instance(instance_id):
    """Suspend a terminated instance in its grace period.

    Sets ``grace_period_suspended=1`` so the user-facing DB export is
    disabled.  Returns ``True`` on success, ``False`` if the instance
    is not in a valid state for grace-period suspension.
    """
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instances SET grace_period_suspended=1 "
            "WHERE id=? AND status='terminated' AND data_retained_until IS NOT NULL "
            "AND grace_period_suspended=0",
            (instance_id,),
        )
        conn.commit()
    return result.rowcount == 1


def unsuspend_grace_period_instance(instance_id):
    """Unsuspend a terminated instance in its grace period.

    Clears ``grace_period_suspended`` so the user-facing DB export is
    re-enabled.  Returns ``True`` on success.
    """
    with get_hosting_db_context() as conn:
        result = conn.execute(
            "UPDATE instances SET grace_period_suspended=0 "
            "WHERE id=? AND status='terminated' AND grace_period_suspended=1",
            (instance_id,),
        )
        conn.commit()
    return result.rowcount == 1


def is_grace_period_suspended(inst):
    """Return True if the instance is terminated, in grace period, and suspended."""
    if inst is None:
        return False
    if inst["status"] != "terminated":
        return False
    if not inst.get("data_retained_until"):
        return False
    return bool(inst.get("grace_period_suspended"))
