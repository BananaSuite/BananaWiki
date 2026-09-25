"""Hosting dashboard: accounts."""

import logging
import sqlite3
from flask import (
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)
from helpers._passwords import generate_password_hash
from .. import config
from .auth import (
    hosting_login_required,
    hosting_admin_required,
    hosting_rate_limit,
    get_current_account,
)
from ..instance_manager import (
    stop_instance,
    suspend_instance,
    unsuspend_instance,
    terminate_instance,
    force_restart_instance,
    apply_account_owner_quota_policy,
    convert_account_apex_instances_to_hosting,
)
from ..db import (
    set_account_admin,
    delete_account,
    get_account_by_id,
    get_account_by_username,
    create_account,
    create_invite_code,
    delete_invite_code,
    get_signup_mode,
    update_hosting_settings,
    count_active_admin_accounts,
    record_account_suspension_action,
    get_instances_for_account,
    is_account_suspended,
    log_hosting_impersonation_start,
    log_hosting_impersonation_stop,
    approve_hosting_account,
    deny_hosting_account,
    get_hosting_db_context,
    update_hosting_account,
    get_account_by_email,
    change_account_password,
    suspend_account,
    unsuspend_account,
    approve_all_pending_hosting_accounts,
    flag_email_invalid,
    clear_email_flag,
)

logger = logging.getLogger(__name__)
from .dashboard_common import (
    _send_account_notice,
)


def register_hosting_accounts_routes(app):
    """Register hosting routes for accounts."""

    @app.route("/admin/accounts/<account_id>/toggle-admin", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_toggle_admin(account_id):
        """Admin: toggle admin status on an account.

        Demoting an admin also converts every apex-mode instance owned
        by that account (e.g. ``wiki.example.com``) back to the standard
        ``{slug}-{suffix}.{BASE_DOMAIN}`` hosting format. Apex domains
        are an admin-only privilege.
        """
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if account_id == session["hosting_account_id"]:
            flash("You cannot change your own admin status.", "error")
            return redirect(url_for("hosting_admin"))
        if target.get("approval_status") in ("pending", "denied"):
            flash(
                f"Cannot grant admin to a {target['approval_status']} account.", "error"
            )
            return redirect(url_for("hosting_admin"))
        new_status = not bool(target["is_admin"])
        set_account_admin(account_id, new_status)
        apply_account_owner_quota_policy(account_id, owner_is_admin=new_status)
        converted_ids = []
        if not new_status:
            # Demotion: revoke apex-mode privileges on existing instances.
            converted_ids = convert_account_apex_instances_to_hosting(account_id)
            restart_failures = []
            for owned_instance in get_instances_for_account(account_id):
                if owned_instance["status"] != "running":
                    continue
                try:
                    ok, reason = force_restart_instance(owned_instance["id"])
                except Exception as exc:
                    ok, reason = False, str(exc)
                if not ok:
                    try:
                        stop_instance(owned_instance["id"])
                    except Exception:
                        pass
                    restart_failures.append(
                        f"{owned_instance['subdomain']}: {reason or 'restart failed'} "
                        "(stopped to apply restrictions safely)"
                    )
            if restart_failures:
                flash(
                    "Admin access was revoked, but restrictive policy could not be applied "
                    "to every running wiki: " + "; ".join(restart_failures),
                    "error",
                )
        label = "granted" if new_status else "revoked"
        flash(f"Admin access {label} for '{target['username']}'.", "success")
        if converted_ids:
            flash(
                f"{len(converted_ids)} apex-domain instance(s) were converted "
                "back to the standard hosting subdomain format.",
                "info",
            )
            renamed = [
                f"{old} → {new}"
                for _instance_id, old, new in converted_ids
                if old != new
            ]
            if renamed:
                flash(
                    "Name conflicts were resolved automatically: " + ", ".join(renamed),
                    "info",
                )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/impersonate", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_impersonate_account(account_id):
        """Admin: view the hosting portal as a regular account."""
        admin = get_account_by_id(session["hosting_account_id"])
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if session.get("hosting_impersonator_account_id"):
            flash(
                "Stop impersonating before starting another impersonation session.",
                "error",
            )
            return redirect(url_for("hosting_dashboard"))
        if target["id"] == admin["id"]:
            flash("You cannot impersonate yourself.", "error")
            return redirect(url_for("hosting_admin"))
        if target["is_admin"]:
            flash("Hosting admins can only impersonate regular accounts.", "error")
            return redirect(url_for("hosting_admin"))
        if is_account_suspended(target["id"]):
            flash("You cannot impersonate a suspended account.", "error")
            return redirect(url_for("hosting_admin"))
        if target.get("approval_status") in ("pending", "denied"):
            flash("You cannot impersonate a pending or denied account.", "error")
            return redirect(url_for("hosting_admin"))

        log_id = log_hosting_impersonation_start(admin["id"], target["id"])
        session["hosting_impersonator_account_id"] = admin["id"]
        session["hosting_impersonation_log_id"] = log_id
        session["hosting_account_id"] = target["id"]
        session["hosting_session_version"] = int(target.get("session_version") or 0)
        flash(f"You are now impersonating '{target['username']}'.", "success")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/admin/stop-impersonating", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_stop_impersonating():
        """Return from hosting account impersonation to the original admin."""
        admin_id = session.pop("hosting_impersonator_account_id", None)
        log_id = session.pop("hosting_impersonation_log_id", None)
        if not admin_id:
            flash("You are not impersonating anyone.", "error")
            return redirect(url_for("hosting_dashboard"))

        log_hosting_impersonation_stop(log_id)
        admin = get_account_by_id(admin_id)
        if admin is None:
            session.pop("hosting_account_id", None)
            flash("Original admin account not found. Please log in again.", "error")
            return redirect(url_for("hosting_login"))
        if is_account_suspended(admin["id"]):
            session.pop("hosting_account_id", None)
            flash(
                "Your original admin account is suspended. Please log in with another administrator account.",
                "error",
            )
            return redirect(url_for("hosting_login"))
        if not admin["is_admin"]:
            session.pop("hosting_account_id", None)
            flash(
                "Your admin access was revoked while impersonating. Please log in again.",
                "error",
            )
            return redirect(url_for("hosting_login"))

        session["hosting_account_id"] = admin["id"]
        session["hosting_session_version"] = int(admin.get("session_version") or 0)
        flash(
            "You have stopped impersonating and returned to your admin account.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/delete", methods=["GET", "POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_delete_account(account_id):
        """Admin: delete any account and handle its instances."""
        from ..db import get_account_by_id, get_instances_for_account

        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if account_id == session["hosting_account_id"]:
            flash("You cannot delete your own account from the admin panel.", "error")
            return redirect(url_for("hosting_admin"))

        instances = get_instances_for_account(account_id)
        active_instances = [i for i in instances if i["status"] != "terminated"]

        if request.method == "GET":
            # Show confirmation page with instances
            return render_template(
                "admin_delete_account.html",
                target=target,
                instances=active_instances,
                terminated_count=len(instances) - len(active_instances),
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                hosting_mode=config.HOSTING_MODE,
            )

        # POST: actually delete
        instance_action = request.form.get("instance_action", "terminate")
        transfer_username = (request.form.get("transfer_username") or "").strip()

        if instance_action == "transfer" and transfer_username:
            transfer_target = get_account_by_username(transfer_username)
            if transfer_target is None:
                flash(f"Target account '{transfer_username}' not found.", "error")
                return render_template(
                    "admin_delete_account.html",
                    target=target,
                    instances=active_instances,
                    terminated_count=len(instances) - len(active_instances),
                    base_domain=config.BASE_DOMAIN,
                    instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                    hosting_mode=config.HOSTING_MODE,
                )
            from ..db import transfer_instance_ownership

            transferred = 0
            for inst in active_instances:
                if transfer_instance_ownership(inst["id"], transfer_target["id"]):
                    transferred += 1
            if transferred:
                flash(
                    f"Transferred {transferred} instance(s) to '{transfer_username}'.",
                    "info",
                )
        else:
            # Terminate all active instances
            for inst in active_instances:
                terminate_instance(inst["id"])

        delete_account(account_id)
        try:
            from ..backups import notify_change

            notify_change(
                "account_deleted",
                f"Account '{target['username']}' deleted with {len(instances)} instance(s)",
            )
        except Exception:
            pass
        flash(
            f"Account '{target['username']}' and all its instances have been deleted.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/schedule-deletion", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_schedule_deletion(account_id):
        """Admin: schedule an account for slow deletion with a countdown."""
        from ..db import get_account_by_id, set_pending_deletion

        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if account_id == session["hosting_account_id"]:
            flash("You cannot schedule your own account for deletion.", "error")
            return redirect(url_for("hosting_admin"))
        if target.get("pending_deletion"):
            flash(
                f"Account '{target['username']}' is already scheduled for deletion.",
                "info",
            )
            return redirect(url_for("hosting_admin"))

        try:
            hours = max(0, int(request.form.get("deletion_hours") or "0"))
            minutes = max(0, int(request.form.get("deletion_minutes") or "0"))
            secs = max(0, int(request.form.get("deletion_seconds") or "0"))
        except (ValueError, TypeError):
            hours, minutes, secs = 24, 0, 0
        _MAX_DELETION_TOTAL_SECONDS = 10 * 365 * 86400  # ~10 years
        total_seconds = hours * 3600 + minutes * 60 + secs
        if total_seconds < 1:
            total_seconds = 1  # minimum 1 second
        if total_seconds > _MAX_DELETION_TOTAL_SECONDS:
            total_seconds = _MAX_DELETION_TOTAL_SECONDS
        reason = (request.form.get("deletion_reason") or "").strip()[:500]

        set_pending_deletion(account_id, seconds=total_seconds, reason=reason)

        # Build human-readable duration for notifications
        dur_parts = []
        if hours:
            dur_parts.append(f"{hours}h")
        if minutes:
            dur_parts.append(f"{minutes}m")
        if secs:
            dur_parts.append(f"{secs}s")
        dur_str = " ".join(dur_parts) or "1m"

        if request.form.get("notify_user"):
            _send_account_notice(
                target,
                "Account Scheduled for Deletion: BananaWiki",
                "Your BananaWiki Hosting account has been scheduled for deletion in {}.{}".format(
                    dur_str,
                    "\n\nReason: {}".format(reason) if reason else "",
                )
                + f"\n\nIf you believe this is a mistake, contact {config.HOSTING_CONTACT_EMAIL} immediately.",
            )
        try:
            from ..backups import notify_change

            notify_change(
                "account_pending_deletion",
                f"Account '{target['username']}' scheduled for deletion in {dur_str}"
                + (f" (reason: {reason})" if reason else ""),
            )
        except Exception:
            pass
        flash(
            f"Account '{target['username']}' scheduled for deletion in {dur_str}.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/cancel-deletion", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_cancel_deletion(account_id):
        """Admin: cancel a pending deletion and restore normal access."""
        from ..db import get_account_by_id, cancel_pending_deletion

        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if not target.get("pending_deletion"):
            flash(
                f"Account '{target['username']}' is not scheduled for deletion.", "info"
            )
            return redirect(url_for("hosting_admin"))

        cancel_pending_deletion(account_id)
        flash(
            f"Pending deletion for '{target['username']}' has been cancelled.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/bulk-delete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_admin_bulk_delete_accounts():
        """Admin: bulk-delete selected accounts."""
        ids = request.form.getlist("account_ids")
        current_id = session["hosting_account_id"]
        if not ids:
            flash("No accounts selected.", "error")
            return redirect(url_for("hosting_admin"))
        count = 0
        for aid in ids:
            if aid == current_id:
                continue
            acct = get_account_by_id(aid)
            if acct is None:
                continue
            # Terminate all instances first
            from ..db import get_instances_for_account

            for inst in get_instances_for_account(aid):
                if inst["status"] != "terminated":
                    terminate_instance(inst["id"])
            delete_account(aid)
            count += 1
        flash(f"{count} account(s) have been successfully deleted.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/signup-mode", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_set_signup_mode():
        """Admin: switch the platform between open / invite / closed signup."""
        mode = (request.form.get("signup_mode") or "").strip().lower()
        if mode not in {"open", "invite", "closed", "approval"}:
            flash(
                "Signup mode must be one of: open, invite, closed, approval.", "error"
            )
            return redirect(url_for("hosting_admin"))

        old_mode = get_signup_mode()
        update_hosting_settings(signup_mode=mode)

        # Sync hosting_activation_required for backwards compat
        update_hosting_settings(
            hosting_activation_required=1 if mode == "approval" else 0
        )

        # If switching away from approval mode, auto-approve all pending accounts
        if old_mode == "approval" and mode != "approval":
            admin_id = session.get("hosting_account_id")
            approved = approve_all_pending_hosting_accounts(admin_id=admin_id)
            if approved:
                flash(f"Auto-approved {approved} pending account(s).", "info")

        # If switching to approval mode, keep existing accounts as-is (only future signups are affected)
        labels = {
            "open": "Open signups",
            "invite": "Invite-code signups",
            "closed": "Closed signups",
            "approval": "Approval required",
        }
        flash(
            f"Signup mode has been updated to: {labels[mode]}.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/invites/create", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_create_invite():
        """Admin: mint a new invite code."""
        note = (request.form.get("note") or "").strip()[:200]
        custom_code = (request.form.get("custom_code") or "").strip()
        if custom_code and (len(custom_code) > 32 or not custom_code.isalnum()):
            flash(
                "Custom invite code must be between 1 and 32 alphanumeric characters.",
                "error",
            )
            return redirect(url_for("hosting_admin"))
        try:
            max_uses = int(request.form.get("max_uses") or 1)
            if max_uses < 1 or max_uses > 1000:
                raise ValueError
        except (TypeError, ValueError):
            flash("Max uses must be between 1 and 1000.", "error")
            return redirect(url_for("hosting_admin"))

        try:
            code_row = create_invite_code(
                created_by=session["hosting_account_id"],
                note=note,
                max_uses=max_uses,
                code=custom_code or None,
            )
        except ValueError:
            flash(
                "Custom invite code must be between 1 and 32 alphanumeric characters.",
                "error",
            )
            return redirect(url_for("hosting_admin"))
        except sqlite3.IntegrityError:
            flash(
                "That invite code is already in use. Please choose a different one.",
                "error",
            )
            return redirect(url_for("hosting_admin"))
        except Exception:
            logger.exception("Failed to mint invite code")
            flash("Failed to create invite code. Please try again.", "error")
            return redirect(url_for("hosting_admin"))
        flash(
            f"Invite code created: {code_row['code']} ({max_uses} use(s))",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/invites/<int:invite_id>/delete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_delete_invite(invite_id):
        """Admin: revoke an invite code."""
        removed = delete_invite_code(invite_id)
        if removed:
            flash("Invite code has been successfully revoked.", "success")
        else:
            flash("Invite code not found.", "error")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/create", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_create_account():
        """Admin: create a new account directly, bypassing signup gating.

        Allows hosting admins to provision accounts on behalf of users
        without having to flip the platform back to ``open`` signup mode
        or hand-issue an invite code each time.  Honours the standard
        username / password rules so admin-created credentials remain
        equivalent to self-signup credentials.
        """
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        make_admin = bool(request.form.get("is_admin"))

        import re

        if not re.match(r"^[a-zA-Z0-9_-]{3,30}$", username):
            flash(
                "Username must be 3-30 characters (letters, digits, hyphens, underscores).",
                "error",
            )
            return redirect(url_for("hosting_admin"))
        if len(password) < 8 or len(password) > 1000:
            flash("Password must be 8-1000 characters.", "error")
            return redirect(url_for("hosting_admin"))
        if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
            flash("Password must include at least one letter and one number.", "error")
            return redirect(url_for("hosting_admin"))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("hosting_admin"))
        if get_account_by_username(username):
            flash("That username is already taken.", "error")
            return redirect(url_for("hosting_admin"))

        hashed = generate_password_hash(password)
        try:
            create_account(username, hashed, is_admin=make_admin)
        except Exception:
            logger.exception("Failed to create account %s", username)
            flash("Failed to create account. Please try again.", "error")
            return redirect(url_for("hosting_admin"))
        flash(
            f"Account '{username}' has been successfully created"
            f"{' (admin)' if make_admin else ''}.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/suspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_suspend_account(account_id):
        """Admin: suspend an account with optional timed suspension, reason,
        and visibility controls (mirrors the instance suspension UX).

        Supports timed suspensions via ``suspend_duration`` form field:
        ``permanent``, a numeric hour count, ``custom_datetime``, or
        ``custom_relative``.  Optional ``suspend_reason`` and visibility
        checkboxes control what the suspended user sees.
        """
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if account_id == session["hosting_account_id"]:
            flash("You cannot suspend your own account.", "error")
            return redirect(url_for("hosting_admin"))

        # Prevent suspending the last active admin
        if target["is_admin"] and count_active_admin_accounts() <= 1:
            flash("Cannot suspend the last active admin account.", "error")
            return redirect(url_for("hosting_admin"))

        # Parse duration
        duration = request.form.get("suspend_duration", "permanent")
        suspended_until = None
        duration_label = "permanent"
        now_dt = _dt.now(_tz.utc)

        if duration == "custom_datetime":
            raw = request.form.get("suspend_custom_datetime", "").strip()
            if raw:
                try:
                    exp = _dt.fromisoformat(raw)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=_tz.utc)
                    if exp > now_dt:
                        suspended_until = exp.isoformat()
                        duration_label = exp.strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, TypeError):
                    flash("Invalid custom date/time format.", "error")
                    return redirect(url_for("hosting_admin"))
            else:
                flash("Custom date/time is required for timed suspension.", "error")
                return redirect(url_for("hosting_admin"))
        elif duration == "custom_relative":
            days = request.form.get("suspend_rel_days", "0").strip()
            hours = request.form.get("suspend_rel_hours", "0").strip()
            minutes = request.form.get("suspend_rel_minutes", "0").strip()
            seconds = request.form.get("suspend_rel_seconds", "0").strip()
            try:
                d = int(days) if days else 0
                h = int(hours) if hours else 0
                m = int(minutes) if minutes else 0
                s = int(seconds) if seconds else 0
                if d < 0 or h < 0 or m < 0 or s < 0:
                    raise ValueError
                _MAX_SUSPEND_TOTAL_SECONDS = 10 * 365 * 86400  # ~10 years
                total_seconds = d * 86400 + h * 3600 + m * 60 + s
                if total_seconds > 0:
                    if total_seconds > _MAX_SUSPEND_TOTAL_SECONDS:
                        total_seconds = _MAX_SUSPEND_TOTAL_SECONDS
                    exp = now_dt + _td(seconds=total_seconds)
                    suspended_until = exp.isoformat()
                    parts = []
                    if d:
                        parts.append(f"{d}d")
                    if h:
                        parts.append(f"{h}h")
                    if m:
                        parts.append(f"{m}m")
                    if s:
                        parts.append(f"{s}s")
                    duration_label = " ".join(parts) or "0s"
            except (ValueError, TypeError):
                flash("Invalid relative time values.", "error")
                return redirect(url_for("hosting_admin"))
        elif duration and duration != "permanent":
            try:
                hours = int(duration)
                if hours > 0:
                    exp = now_dt + _td(hours=hours)
                    suspended_until = exp.isoformat()
                    duration_label = f"{hours}h"
            except (ValueError, TypeError):
                pass

        reason = request.form.get("suspend_reason", "").strip()
        reason_visible = request.form.get("suspend_reason_visible") == "1" and bool(
            reason
        )
        time_visible = (
            request.form.get("suspend_time_visible") == "1"
            and suspended_until is not None
        )
        suspend_instances = request.form.get("suspend_instances") == "1"
        performed_by = session.get("hosting_account_id")

        suspend_account(
            account_id,
            reason=reason,
            suspended_until=suspended_until,
            reason_visible=reason_visible,
            time_visible=time_visible,
            performed_by=performed_by,
        )
        record_account_suspension_action(
            account_id,
            "suspend",
            performed_by=performed_by,
            reason=reason or None,
            reason_visible=reason_visible,
            time_visible=time_visible,
            duration=duration_label,
            suspended_until=suspended_until,
        )
        if suspend_instances:
            instances = get_instances_for_account(account_id)
            for inst in instances:
                if inst["status"] in ("running", "stopped"):
                    suspend_instance(inst["id"])
        try:
            from ..backups import notify_change

            notify_change(
                "account_suspended", f"Account '{target['username']}' suspended"
            )
        except Exception:
            pass
        if request.form.get("notify_user", "1") != "0":
            notice = "Your BananaWiki Hosting account has been suspended."
            if reason_visible and reason:
                notice += f"\n\nReason: {reason}"
            if time_visible and suspended_until:
                notice += f"\n\nScheduled end: {suspended_until}"
            notice += (
                "\n\nContact the hosting operator if you believe this is a mistake."
            )
            _send_account_notice(target, "BananaWiki Hosting account suspended", notice)
        flash(f"Account '{target['username']}' has been suspended.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/unsuspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_unsuspend_account(account_id):
        """Admin: remove suspension from an account.

        When ``unsuspend_instances=1`` is sent, every suspended instance
        owned by this account is also unsuspended (expiry shifted and runtime restarted) so the user can pick up right where
        they left off.
        """
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        performed_by = session.get("hosting_account_id")
        unsuspend_account(account_id)
        record_account_suspension_action(
            account_id,
            "unsuspend",
            performed_by=performed_by,
        )
        restored = 0
        if request.form.get("unsuspend_instances") == "1":
            for inst in get_instances_for_account(account_id):
                if inst["status"] == "suspended":
                    ok, _ = unsuspend_instance(inst["id"])
                    if ok:
                        restored += 1
        try:
            from ..backups import notify_change

            notify_change(
                "account_unsuspended", f"Account '{target['username']}' unsuspended"
            )
        except Exception:
            pass
        msg = f"Account '{target['username']}' has been unsuspended."
        if restored:
            msg += f" {restored} instance(s) also unsuspended."
        if request.form.get("notify_user", "1") != "0":
            _send_account_notice(
                target,
                "BananaWiki Hosting account restored",
                "Your BananaWiki Hosting account is active again. You can sign in and manage your wikis.",
            )
        flash(msg, "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/edit", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=30, window=60)
    def hosting_admin_edit_account(account_id):
        """Admin: change username, email, and/or password for any hosting account."""
        import re as _re

        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))

        new_username = (request.form.get("new_username") or "").strip()
        new_email = (request.form.get("new_email") or "").strip().lower()
        new_password = (request.form.get("new_password") or "").strip()
        confirm_pw = (request.form.get("confirm_password") or "").strip()

        if not any([new_username, new_email, new_password]):
            flash("No changes submitted.", "info")
            return redirect(url_for("hosting_admin"))

        errors = []

        if new_username:
            if not _re.match(r"^[a-zA-Z0-9_-]{3,30}$", new_username):
                errors.append(
                    "Username must be 3–30 characters: letters, numbers, hyphens, underscores only."
                )
            elif new_username.lower() != target["username"].lower():
                existing = get_account_by_username(new_username)
                if existing and existing["id"] != account_id:
                    errors.append(f"Username '{new_username}' is already taken.")

        if new_email:
            if (
                not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new_email)
                or len(new_email) > 254
            ):
                errors.append("Email address is not valid.")
            else:
                existing_email = get_account_by_email(new_email)
                if existing_email and existing_email["id"] != account_id:
                    errors.append(
                        f"Email '{new_email}' is already in use by another account."
                    )

        if new_password:
            if len(new_password) < 8:
                errors.append("New password must be at least 8 characters.")
            elif new_password != confirm_pw:
                errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("hosting_admin"))

        updates = {}
        email_changed = False
        if new_username and new_username != target["username"]:
            updates["username"] = new_username
        if new_email and new_email != (target.get("email") or "").lower():
            updates["email"] = new_email
            # Clear old verification so they re-verify the new address
            updates["email_verified_at"] = None
            email_changed = True

        if updates:
            update_hosting_account(account_id, **updates)

        if new_password:
            change_account_password(account_id, generate_password_hash(new_password))

        # Send a verification email to the new address so the user is not
        # stuck behind the verification gate with no pending token.
        if email_changed and new_email:
            try:
                from .auth import _send_verification_email

                refreshed = get_account_by_id(account_id)
                if refreshed:
                    _send_verification_email(refreshed, ignore_cooldown=True)
            except Exception:
                pass  # best-effort; user can still hit "resend"

        try:
            from ..backups import notify_change

            notify_change(
                "account_edited", f"Account '{target['username']}' edited by admin"
            )
        except Exception:
            pass

        changed = []
        if "username" in updates:
            changed.append(f"username → {new_username}")
        if "email" in updates:
            changed.append(f"email → {new_email}")
        if new_password:
            changed.append("password reset")

        flash(
            f"Account '{target['username']}' updated: {', '.join(changed)}.", "success"
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/flag-email", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_flag_email(account_id):
        """Admin: flag a user's email as invalid and force them to re-enter it.

        When the optional ``new_email`` field is filled in, the admin is
        setting an address on behalf of the user. No flag is set and the
        user is not prompted.
        """
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        reason = (request.form.get("reason") or "").strip()
        reason_visible = request.form.get("reason_visible") == "1"
        new_email = (request.form.get("new_email") or "").strip().lower()

        if new_email:
            # Admin sets email directly: validate format
            import re as _re

            if (
                not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new_email)
                or len(new_email) > 254
            ):
                flash("The replacement email address is not valid.", "error")
                return redirect(url_for("hosting_admin"))
            dup = get_account_by_email(new_email)
            if dup and dup["id"] != account_id:
                flash(
                    f"Email '{new_email}' is already in use by another account.",
                    "error",
                )
                return redirect(url_for("hosting_admin"))
            flag_email_invalid(
                account_id,
                reason=reason,
                reason_visible=reason_visible,
                replacement_email=new_email,
            )
            flash(
                f"Email for '{target['username']}' has been changed to '{new_email}'.",
                "success",
            )
        else:
            flag_email_invalid(account_id, reason=reason, reason_visible=reason_visible)
            flash(
                f"Email for '{target['username']}' has been flagged as invalid. "
                "They will be asked to enter a new email on their next visit.",
                "success",
            )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/unflag-email", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_unflag_email(account_id):
        """Admin: clear the invalid-email flag on an account."""
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        clear_email_flag(account_id)
        flash(f"Email flag cleared for '{target['username']}'.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/approve", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_approve_account(account_id):
        """Admin: approve a pending account, or undo a previous denial."""
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if target.get("approval_status") not in ("pending", "denied"):
            flash(
                f"Account '{target['username']}' is not awaiting approval.",
                "error",
            )
            return redirect(url_for("hosting_admin"))
        decision_reason = (request.form.get("decision_reason") or "").strip()
        if len(decision_reason) > 1000:
            flash("Decision reason must not exceed 1000 characters.", "error")
            return redirect(url_for("hosting_admin"))
        admin_id = session["hosting_account_id"]
        if approve_hosting_account(account_id, admin_id, decision_reason):
            notice = (
                "Your BananaWiki Hosting account has been approved. You can now "
                "sign in and create or manage your wiki."
            )
            if decision_reason:
                notice += f"\n\nDecision reason: {decision_reason}"
            _send_account_notice(
                target,
                "Your BananaWiki Hosting account was approved",
                notice,
            )
            flash(f"Account '{target['username']}' has been approved.", "success")
        else:
            flash(f"Failed to approve '{target['username']}'.", "error")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/accounts/<account_id>/deny", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_deny_account(account_id):
        """Admin: deny a pending account.

        The account is marked as denied and may be automatically
        deleted after the configured timeout (in seconds).
        """
        target = get_account_by_id(account_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))
        if target.get("approval_status") != "pending":
            flash(f"Account '{target['username']}' is not pending approval.", "error")
            return redirect(url_for("hosting_admin"))
        decision_reason = (request.form.get("decision_reason") or "").strip()
        if len(decision_reason) > 1000:
            flash("Decision reason must not exceed 1000 characters.", "error")
            return redirect(url_for("hosting_admin"))
        admin_id = session["hosting_account_id"]
        if deny_hosting_account(account_id, admin_id, decision_reason):
            notice = "Your BananaWiki Hosting account request was not approved."
            if decision_reason:
                notice += f"\n\nDecision reason: {decision_reason}"
            notice += "\n\nContact the hosting operator if you need more information."
            _send_account_notice(
                target,
                "Your BananaWiki Hosting request was not approved",
                notice,
            )
            flash(f"Account '{target['username']}' has been denied.", "success")
        else:
            flash(f"Failed to deny '{target['username']}'.", "error")
        return redirect(url_for("hosting_admin"))

    @app.route("/account/merge-request", methods=["GET", "POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=300)
    def hosting_merge_request():
        """Request to merge this account into another hosting account."""
        from .. import db as _hosting_db

        acct = get_current_account()

        if request.method == "POST":
            target_username = (request.form.get("target_username", "")).strip()
            reason = request.form.get("reason", "").strip()

            if not target_username:
                flash(
                    "Please specify the username of the account you want to merge into.",
                    "error",
                )
                return render_template("account_merge_request.html")

            target = _hosting_db.get_account_by_username(target_username)
            if not target:
                flash("No account found with that username.", "error")
                return render_template("account_merge_request.html")

            if target["id"] == acct["id"]:
                flash("Cannot merge with your own account.", "error")
                return render_template("account_merge_request.html")

            pending = _hosting_db.get_active_requests_for_account(acct["id"])
            if any(r["source_account_id"] == acct["id"] for r in pending):
                flash(
                    "You already have a pending merge request as the source account.",
                    "error",
                )
                return render_template("account_merge_pending.html")

            try:
                _hosting_db.create_merge_request(
                    acct["id"], target["id"], acct["id"], reason
                )
                flash("Merge request sent. Both accounts must approve.")
                return redirect(url_for("hosting_merge_pending"))
            except ValueError as exc:
                flash(str(exc), "error")

        return render_template("account_merge_request.html")

    @app.route("/account/merge/pending")
    @hosting_login_required
    def hosting_merge_pending():
        """Display pending merge requests for the current account."""
        from .. import db as _hosting_db

        acct = get_current_account()
        pending = _hosting_db.get_active_requests_for_account(acct["id"])

        enriched = []
        for req in pending:
            source = _hosting_db.get_account_by_id(req["source_account_id"])
            target = _hosting_db.get_account_by_id(req["target_account_id"])
            enriched.append(
                {
                    **req,
                    "source_username": source["username"] if source else "deleted",
                    "target_username": target["username"] if target else "deleted",
                    "source_is_current": req["source_account_id"] == acct["id"],
                    "target_is_current": req["target_account_id"] == acct["id"],
                }
            )

        return render_template(
            "account_merge_pending.html",
            pending=enriched,
        )

    @app.route("/account/merge/approve/<int:merge_id>", methods=["POST"])
    @hosting_login_required
    def hosting_merge_approve(merge_id):
        """Approve a merge request as source or target."""
        from .. import db as _hosting_db

        acct = get_current_account()
        req = _hosting_db.get_merge_request(merge_id)
        if not req:
            flash("Merge request not found.", "error")
            return redirect(url_for("hosting_merge_pending"))

        if req["status"] not in ("pending",):
            flash("This merge request cannot be approved.", "error")
            return redirect(url_for("hosting_merge_pending"))

        try:
            if req["source_account_id"] == acct["id"]:
                _hosting_db.approve_by_source(merge_id, acct["id"])
                flash("You have approved the merge request.")
            elif req["target_account_id"] == acct["id"]:
                _hosting_db.approve_by_target(merge_id, acct["id"])
                flash("You have approved the merge request.")
            else:
                flash("You are not part of this merge request.", "error")
        except ValueError as exc:
            flash(str(exc), "error")

        return redirect(url_for("hosting_merge_pending"))

    @app.route("/account/merge/cancel/<int:merge_id>", methods=["POST"])
    @hosting_login_required
    def hosting_merge_cancel(merge_id):
        """Cancel a merge request."""
        from .. import db as _hosting_db

        acct = get_current_account()
        req = _hosting_db.get_merge_request(merge_id)
        if not req:
            flash("Merge request not found.", "error")
            return redirect(url_for("hosting_merge_pending"))

        try:
            _hosting_db.cancel_merge(merge_id, acct["id"])
            flash("Merge request cancelled.")
        except ValueError as exc:
            flash(str(exc), "error")

        return redirect(url_for("hosting_merge_pending"))

    @app.route("/admin/merge-accounts", methods=["GET", "POST"])
    @hosting_admin_required
    def admin_merge_accounts():
        """Admin-initiated account merge."""
        from .. import db as _hosting_db

        if request.method == "POST":
            source_username = (request.form.get("source_username", "")).strip()
            target_username = (request.form.get("target_username", "")).strip()

            if not source_username or not target_username:
                flash("Both usernames are required.", "error")
                return render_template("admin/merge_accounts.html")

            source = _hosting_db.get_account_by_username(source_username)
            target = _hosting_db.get_account_by_username(target_username)

            if not source:
                flash(f"No account found with username '{source_username}'.", "error")
                return render_template("admin/merge_accounts.html")
            if not target:
                flash(f"No account found with username '{target_username}'.", "error")
                return render_template("admin/merge_accounts.html")
            if source["id"] == target["id"]:
                flash("Cannot merge an account with itself.", "error")
                return render_template("admin/merge_accounts.html")

            admin = get_current_account()

            action = request.form.get("action", "")

            if action == "execute":
                try:
                    result = _hosting_db.admin_execute_merge(
                        source["id"], target["id"], admin["id"]
                    )
                    flash(
                        f"Account '{source['username']}' merged into '{target['username']}'. "
                        f"{result['instances_transferred']} instances transferred."
                    )
                except ValueError as exc:
                    flash(str(exc), "error")

                return redirect(url_for("admin_merge_requests"))

        return render_template("admin/merge_accounts.html")

    @app.route("/admin/merge-requests", methods=["GET"])
    @hosting_admin_required
    def admin_merge_requests():
        """List all account merge requests."""
        from .. import db as _hosting_db

        with get_hosting_db_context() as _conn:
            rows = _conn.execute(
                """SELECT * FROM hosting_account_merge_requests
                    ORDER BY created_at DESC LIMIT 100"""
            ).fetchall()

        enriched = []
        for req in rows:
            source = _hosting_db.get_account_by_id(req["source_account_id"])
            target = _hosting_db.get_account_by_id(req["target_account_id"])
            req_dict = dict(req)
            req_dict["source_username"] = source["username"] if source else "deleted"
            req_dict["target_username"] = target["username"] if target else "deleted"
            req_dict["both_ready"] = req_dict.get("source_approved") and req_dict.get(
                "target_approved"
            )
            enriched.append(req_dict)

        return render_template(
            "admin/merge_requests.html",
            requests=enriched,
        )

    @app.route("/admin/merge-requests/<int:merge_id>/approve", methods=["POST"])
    @hosting_admin_required
    def admin_approve_merge_request(merge_id):
        """Admin forces a merge request."""
        from .. import db as _hosting_db

        try:
            _hosting_db.approve_by_admin(merge_id, get_current_account()["id"])
            flash("Merge request approved by admin.")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))

    @app.route("/admin/merge-requests/<int:merge_id>/deny", methods=["POST"])
    @hosting_admin_required
    def admin_deny_merge_request(merge_id):
        """Admin denies a merge request."""
        from .. import db as _hosting_db

        try:
            _hosting_db.deny_merge(merge_id, get_current_account()["id"])
            flash("Merge request denied.")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))

    @app.route("/admin/merge-requests/<int:merge_id>/execute", methods=["POST"])
    @hosting_admin_required
    def admin_execute_merge_request(merge_id):
        """Complete a merge after both accounts or an administrator approve it."""
        from .. import db as _hosting_db
        try:
            result = _hosting_db.execute_merge(merge_id, get_current_account()["id"])
            flash(f"Merge completed. {result['instances_transferred']} instances transferred.")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))

    @app.route("/admin/merge-requests/<int:merge_id>/cancel", methods=["POST"])
    @hosting_admin_required
    def admin_cancel_merge_request(merge_id):
        """Admin cancels a merge request."""
        from .. import db as _hosting_db

        try:
            _hosting_db.cancel_merge(merge_id, get_current_account()["id"])
            flash("Merge request cancelled.")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_merge_requests"))
