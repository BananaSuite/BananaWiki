"""Hosting dashboard: instance admin."""

import logging
import os
import tempfile
from flask import (
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)
from .. import config
from .auth import hosting_admin_required, hosting_rate_limit
from ..instance_manager import (
    stop_instance,
    restart_instance,
    suspend_instance,
    unsuspend_instance,
    terminate_instance,
    get_instance_detail,
    extend_instance,
    reset_instance_password,
    make_instance_indefinite,
    restore_instance,
    build_instance_archive,
    read_instance_log,
    set_instance_storage_limit,
    hard_delete_terminated_instance,
    set_instance_user_password,
    remove_instance_user,
    list_instance_users_page,
    set_instance_expiry,
    force_restart_instance,
    apply_instance_owner_quota_policy,
    duplicate_instance,
    change_instance_identity,
    reset_wiki,
    set_instance_upload_policy,
    get_effective_instance_upload_policy,
    quarantine_instance_external_plugins,
    restore_latest_plugin_safety_snapshot,
    capture_plugin_safety_snapshot,
    lift_instance_plugin_quarantine,
    list_plugin_safety_snapshots,
    instance_plugins_quarantined,
)
from ..db import (
    get_instance,
    get_account_by_id,
    get_account_by_username,
    transfer_instance_ownership,
    get_instance_suspension_history,
    record_instance_suspension_action,
    check_instance_suspension_expired,
    suspend_grace_period_instance,
    unsuspend_grace_period_instance,
    FEATURE_LABELS,
    list_instance_feature_requests,
    get_hosting_db_context,
)
from ..db._events import record_event

logger = logging.getLogger(__name__)
from .dashboard_common import (
    _cleanup_stale_export_downloads,
    _export_download_root,
    _post_instance_admin_action_redirect,
    _stream_download_file,
)


def _record_plugin_safety_event(instance_id, action, ok, message):
    """Add a plugin safety action to the instance's append-only event log."""
    try:
        with get_hosting_db_context() as conn:
            record_event(
                conn,
                "instance",
                instance_id,
                f"plugins.{action}" + ("" if ok else ".failed"),
                session.get("hosting_account_id"),
                message,
            )
            conn.commit()
    except Exception:
        logger.exception("Could not record plugin event %s for %s", action, instance_id)


def register_hosting_instance_admin_routes(app):
    """Register hosting routes for instance admin."""

    @app.route("/admin/instances/<instance_id>/stop", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_stop_instance(instance_id):
        """Admin: stop any running instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = stop_instance(instance_id, allow_suspended=True)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' stopped by admin.", "success")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/restart", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_restart_instance(instance_id):
        """Admin: restart any stopped instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = restart_instance(instance_id, allow_suspended=True)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' restarted by admin.", "success")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/extend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_extend_instance(instance_id):
        """Admin: extend the trial duration of an instance."""
        try:
            days = int(request.form.get("extra_days") or "0")
            hours = int(request.form.get("extra_hours") or "0")
            minutes = int(request.form.get("extra_minutes") or "0")
            seconds = int(request.form.get("extra_seconds") or "0")
            if days < 0 or hours < 0 or minutes < 0 or seconds < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Extension amount must be a non-negative number.", "error")
            return redirect(url_for("hosting_admin"))

        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        if total_seconds < 1:
            flash("Extension must be at least 1 second.", "error")
            return redirect(url_for("hosting_admin"))
        _MAX_EXTEND_TOTAL_SECONDS = 10 * 365 * 86400  # ~10 years
        if total_seconds > _MAX_EXTEND_TOTAL_SECONDS:
            total_seconds = _MAX_EXTEND_TOTAL_SECONDS

        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        # Convert to fractional days so the day-based extend logic stays intact.
        extra_days = total_seconds / 86400
        ok, reason = extend_instance(instance_id, extra_days)
        if not ok:
            flash(reason, "error")
        else:
            dur_parts = []
            if days:
                dur_parts.append(f"{days}d")
            if hours:
                dur_parts.append(f"{hours}h")
            if minutes:
                dur_parts.append(f"{minutes}m")
            if seconds:
                dur_parts.append(f"{seconds}s")
            dur_str = " ".join(dur_parts) or f"{total_seconds}s"
            flash(
                f"Instance '{inst['subdomain']}' extended by {dur_str}.",
                "success",
            )
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/terminate", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_terminate_instance(instance_id):
        """Admin: terminate any instance regardless of owner."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = terminate_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' terminated by admin.", "success")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/transfer", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_transfer_instance(instance_id):
        """Admin: transfer ownership of an instance to another account."""
        target_username = (request.form.get("username") or "").strip()
        if not target_username:
            flash("Username is required to continue.", "error")
            return redirect(url_for("hosting_admin"))

        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        target = get_account_by_username(target_username)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("hosting_admin"))

        if target["id"] == inst["account_id"]:
            flash("This wiki is already owned by that account.", "error")
            return redirect(url_for("hosting_admin"))

        owner = get_account_by_id(inst["account_id"])
        was_admin_owned = bool(owner and owner["is_admin"])
        will_be_admin_owned = bool(target["is_admin"])

        converted_identity = None
        old_subdomain = inst["subdomain"]
        if inst["domain_mode"] == "apex" and not will_be_admin_owned:
            converted_identity, error = change_instance_identity(
                instance_id,
                inst["subdomain"],
                "hosting",
                account_is_admin=False,
                auto_suffix=True,
            )
            if error:
                flash(error, "error")
                return redirect(
                    url_for("hosting_admin_instance_manage", instance_id=instance_id)
                )
            inst = get_instance(instance_id)
            if inst is None:
                flash("Instance not found.", "error")
                return redirect(url_for("hosting_admin"))

        if not transfer_instance_ownership(instance_id, target["id"]):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        if was_admin_owned != will_be_admin_owned:
            ok, reason = apply_instance_owner_quota_policy(
                instance_id,
                owner_is_admin=will_be_admin_owned,
            )
            if not ok:
                logger.warning(
                    "Failed to re-apply quota policy after transfer for %s: %s",
                    instance_id,
                    reason,
                )

        flash(
            f"Instance '{inst['subdomain']}' has been successfully transferred to '{target['username']}'.",
            "success",
        )
        if converted_identity:
            rename_note = ""
            if converted_identity["subdomain"] != old_subdomain:
                rename_note = f" as '{converted_identity['subdomain']}'"
            flash(
                "Apex access is admin-only, so the wiki was moved to "
                f"hosting mode{rename_note}.",
                "info",
            )

        ref = request.referrer or ""
        if f"/admin/instances/{instance_id}/manage" in ref:
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/rename", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_rename_instance(instance_id):
        """Admin: rename an instance and/or change its URL mode."""
        subdomain = (request.form.get("subdomain") or "").strip().lower()
        domain_mode_val = (request.form.get("domain_mode") or "hosting").strip()
        if domain_mode_val not in ("hosting", "apex"):
            domain_mode_val = "hosting"

        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        owner = get_account_by_id(inst["account_id"])
        owner_is_admin = bool(owner and owner["is_admin"])
        if domain_mode_val == "apex" and not owner_is_admin:
            flash(
                "Apex subdomain mode is only available for admin-owned wikis.", "error"
            )
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )

        old_subdomain = inst["subdomain"]
        old_mode = inst["domain_mode"]
        new_inst, error = change_instance_identity(
            instance_id,
            subdomain,
            domain_mode_val,
            account_is_admin=owner_is_admin,
            auto_suffix=False,
        )
        if error:
            flash(error, "error")
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )

        flash(
            f"Instance '{old_subdomain}' has been successfully renamed to "
            f"'{new_inst['subdomain']}'.",
            "success",
        )
        if old_mode != new_inst["domain_mode"]:
            flash(
                f"Domain mode changed from {old_mode} to {new_inst['domain_mode']}.",
                "info",
            )
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route("/admin/instances/<instance_id>/duplicate", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_admin_duplicate_instance(instance_id):
        """Admin: duplicate an instance with a new subdomain and optional owner.

        The form submits ``subdomain``, ``domain_mode`` (``hosting`` or
        ``apex``) and an optional ``owner_username``.  When
        ``owner_username`` is blank the clone is assigned to the source
        instance's owner; the special value ``__self__`` assigns it to
        the admin performing the action.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        subdomain = (request.form.get("subdomain") or "").strip()
        if not subdomain:
            flash("Subdomain is required to continue.", "error")
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )

        domain_mode_val = (request.form.get("domain_mode") or "hosting").strip()
        if domain_mode_val not in ("hosting", "apex"):
            domain_mode_val = "hosting"

        owner_username = (request.form.get("owner_username") or "").strip()

        # Determine target account
        if owner_username == "__self__":
            target_account_id = session["hosting_account_id"]
        elif owner_username:
            target = get_account_by_username(owner_username)
            if target is None:
                flash(f"Account '{owner_username}' not found.", "error")
                return redirect(
                    url_for("hosting_admin_instance_manage", instance_id=instance_id)
                )
            target_account_id = target["id"]
        else:
            target_account_id = inst["account_id"]

        target_acct = get_account_by_id(target_account_id)
        is_admin = bool(target_acct and target_acct["is_admin"])

        if domain_mode_val == "apex" and not is_admin:
            flash("Apex subdomain mode is only available for admin accounts.", "error")
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )

        new_inst, error = duplicate_instance(
            instance_id,
            target_account_id,
            subdomain,
            domain_mode=domain_mode_val,
            account_is_admin=is_admin,
        )
        if error:
            flash(error, "error")
            return redirect(
                url_for("hosting_admin_instance_manage", instance_id=instance_id)
            )

        flash(
            f"Instance '{inst['subdomain']}' has been successfully duplicated "
            f"to '{new_inst['subdomain']}'.",
            "success",
        )
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=new_inst["id"])
        )

    @app.route("/admin/instances/<instance_id>/reset-password", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_reset_instance_password(instance_id):
        """Admin: reset the BananaWiki admin password for any instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, result = reset_instance_password(instance_id)
        if not ok:
            flash(result, "error")
        else:
            flash(
                f"Admin password for '{inst['subdomain']}' has been reset. "
                "The instance owner will see the new credentials on their instance page.",
                "success",
            )
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/reset-wiki", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_admin_reset_wiki(instance_id):
        """Admin: wipe instance content and re-seed to factory defaults."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, result = reset_wiki(instance_id)
        if not ok:
            flash(result, "error")
        else:
            flash(
                f"Wiki '{inst['subdomain']}' has been reset to factory defaults. "
                "New admin password is available on the instance detail page.",
                "success",
            )
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/make-indefinite", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_make_indefinite(instance_id):
        """Admin: remove the expiry limit from an instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = make_instance_indefinite(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(
                f"Instance '{inst['subdomain']}' is now set to run indefinitely.",
                "success",
            )
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/delete-terminated", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_delete_terminated_instance(instance_id):
        """Admin: hard-delete a terminated instance pending deletion."""
        ok, reason = hard_delete_terminated_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash("Terminated instance has been permanently deleted.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-restore-terminated", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_admin_bulk_restore_terminated_instances():
        """Admin: bulk-restore selected terminated grace-period instances."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No grace-period instances selected.", "error")
            return redirect(url_for("hosting_admin"))

        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if (
                inst is None
                or inst["status"] != "terminated"
                or not inst.get("data_retained_until")
            ):
                skipped += 1
                continue
            _new_inst, error = restore_instance(iid)
            if error:
                skipped += 1
            else:
                changed += 1
        flash(
            f"{changed} grace-period instance(s) restored. {skipped} skipped.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-delete-terminated", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_admin_bulk_delete_terminated_instances():
        """Admin: bulk-hard-delete selected terminated grace-period instances."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No grace-period instances selected.", "error")
            return redirect(url_for("hosting_admin"))

        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if (
                inst is None
                or inst["status"] != "terminated"
                or not inst.get("data_retained_until")
            ):
                skipped += 1
                continue
            ok, _reason = hard_delete_terminated_instance(iid)
            if ok:
                changed += 1
            else:
                skipped += 1
        flash(
            f"{changed} grace-period instance(s) permanently deleted. {skipped} skipped.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-delete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_admin_bulk_delete_instances():
        """Admin: bulk-terminate selected instances."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_admin"))
        count = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["status"] == "terminated":
                continue
            ok, _ = terminate_instance(iid)
            if ok:
                count += 1
        flash(f"{count} instance(s) have been successfully terminated.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-stop", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_bulk_stop_instances():
        """Admin: bulk-pause selected running instances.  Idempotent."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_admin"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["status"] == "terminated":
                continue
            if inst["status"] != "running":
                skipped += 1
                continue
            ok, _ = stop_instance(iid)
            if ok:
                changed += 1
        flash(
            f"{changed} instance(s) paused. {skipped} already not running.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-restart", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_bulk_restart_instances():
        """Admin: bulk-resume selected paused instances.  Idempotent."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_admin"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["status"] == "terminated":
                continue
            # ``restart_instance`` can also lift an admin-imposed
            # suspension, but admins are expected to use the explicit
            # unsuspend action for that path.  This bulk action only
            # touches ``stopped`` rows.
            if inst["status"] != "stopped":
                skipped += 1
                continue
            ok, _ = restart_instance(iid)
            if ok:
                changed += 1
        flash(
            f"{changed} instance(s) resumed. {skipped} already not paused.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-suspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_bulk_suspend_instances():
        """Admin: bulk-suspend selected instances.  Idempotent."""
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_admin"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["status"] == "terminated":
                continue
            if inst["status"] == "suspended":
                skipped += 1
                continue
            ok, _ = suspend_instance(iid)
            if ok:
                changed += 1
        flash(
            f"{changed} instance(s) suspended. {skipped} already suspended.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/bulk-unsuspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_bulk_unsuspend_instances():
        """Admin: bulk-unsuspend (and restart) selected instances.

        Idempotent: suspended rows are lifted via :func:`unsuspend_instance`
        (matching the single-instance ``/admin/instances/<id>/unsuspend``
        flow, including the expiry-window credit);
        rows that are not currently suspended are skipped.
        """
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_admin"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["status"] == "terminated":
                continue
            if inst["status"] != "suspended":
                skipped += 1
                continue
            ok, _ = unsuspend_instance(iid)
            if ok:
                changed += 1
        flash(
            f"{changed} instance(s) unsuspended. {skipped} already not suspended.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/storage-limit", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_set_storage_limit(instance_id):
        """Admin: set per-instance storage cap or unlimited mode."""
        unlimited = request.form.get("unlimited") == "1"
        limit_raw = (request.form.get("storage_limit_mb") or "").strip()
        try:
            limit = 0 if unlimited else int(limit_raw)
            if limit < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Storage limit must be a positive number or unlimited.", "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        ok, reason = set_instance_storage_limit(instance_id, limit)
        if not ok:
            flash(reason, "error")
        else:
            flash("Instance storage limit has been updated.", "success")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/upload-policy", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_set_upload_policy(instance_id):
        """Admin: set per-instance wiki attachment upload policy."""
        inherit_size = request.form.get("inherit_upload_size") == "1"
        size_raw = (request.form.get("upload_max_size_mb") or "").strip()
        try:
            upload_max_size_mb = None if inherit_size or not size_raw else int(size_raw)
            if upload_max_size_mb is not None and not (1 <= upload_max_size_mb <= 2048):
                raise ValueError
        except (ValueError, TypeError):
            flash(
                "Upload size must be between 1 and 2048 MB, or inherit the platform default.",
                "error",
            )
            return redirect(_post_instance_admin_action_redirect(instance_id))
        blocked_extensions = (
            request.form.get("upload_blocked_extensions") or ""
        ).strip()[:2000]
        ok, reason = set_instance_upload_policy(
            instance_id,
            upload_max_size_mb=upload_max_size_mb,
            upload_blocked_extensions=blocked_extensions,
        )
        if not ok:
            flash(reason, "error")
        else:
            flash("Wiki upload policy has been updated.", "success")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/manage", methods=["GET"])
    @hosting_admin_required
    def hosting_admin_instance_manage(instance_id):
        """Render the per-instance admin management page.

        Shows lifecycle controls and checks for expired timed suspensions.
        """
        # Auto-unsuspend if the instance's timed suspension has expired.
        check_instance_suspension_expired(instance_id)
        inst = get_instance_detail(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        # One bounded page of the wiki's users plus the full count.
        wiki_users, wiki_users_total = (
            list_instance_users_page(instance_id)
            if inst["status"] != "terminated"
            else ([], 0)
        )
        owner = get_account_by_id(inst["account_id"])
        suspension_history = get_instance_suspension_history(instance_id)
        upload_policy = get_effective_instance_upload_policy(inst)
        feature_requests = list_instance_feature_requests(instance_id)

        return render_template(
            "admin_instance_manage.html",
            instance=inst,
            wiki_users=wiki_users,
            wiki_users_total=wiki_users_total,
            plugins_quarantined=instance_plugins_quarantined(instance_id),
            plugin_snapshots=list_plugin_safety_snapshots(instance_id),
            owner=owner,
            suspension_history=suspension_history,
            upload_policy=upload_policy,
            base_domain=config.BASE_DOMAIN,
            instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            hosting_mode=config.HOSTING_MODE,
            storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
            duration_days=config.INSTANCE_DURATION_DAYS,
            feature_requests=feature_requests,
            feature_labels=FEATURE_LABELS,
        )

    @app.route("/admin/instances/<instance_id>/force-restart", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_force_restart(instance_id):
        """Admin: force-restart an instance, clearing stale PID state.

        Fixes the "stuck on Starting up…" failure mode after a VPS
        reboot: the instance row says ``running`` and a PID file
        exists, but the PID happens to belong to an unrelated process
        (e.g. systemd) so the soft restart path never actually spawns
        a fresh Gunicorn.  This action force-kills any lingering
        Gunicorn for the instance's port, wipes the PID file and
        boot-id sidecar, and re-spawns the process from scratch.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = force_restart_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(
                f"Instance '{inst['subdomain']}' has been force-restarted.",
                "success",
            )
        # Default to the dedicated admin management page so legacy
        # callers (no ``Referer`` header) still land where the action
        # was historically wired up.
        ref = request.referrer or ""
        if ref.rstrip("/").endswith(f"/instances/{instance_id}"):
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route("/admin/instances/<instance_id>/quarantine-plugins", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=3, window=300)
    def hosting_admin_quarantine_plugins(instance_id):
        """Offline kill switch for all custom plugins in one tenant."""
        ok, message = quarantine_instance_external_plugins(instance_id)
        _record_plugin_safety_event(instance_id, "quarantined", ok, message)
        flash(message, "success" if ok else "error")
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route(
        "/admin/instances/<instance_id>/restore-plugin-snapshot", methods=["POST"]
    )
    @hosting_admin_required
    @hosting_rate_limit(max_requests=2, window=300)
    def hosting_admin_restore_plugin_snapshot(instance_id):
        """Restore a platform-stored pre-enable snapshot for one tenant.

        Restores the newest one unless the form names a recorded snapshot.
        """
        snapshot_name = (request.form.get("snapshot") or "").strip() or None
        ok, message = restore_latest_plugin_safety_snapshot(
            instance_id, snapshot_name=snapshot_name
        )
        _record_plugin_safety_event(
            instance_id, "snapshot_restored", ok,
            f"{snapshot_name or 'newest'}: {message}",
        )
        flash(message, "success" if ok else "error")
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route(
        "/admin/instances/<instance_id>/capture-plugin-snapshot", methods=["POST"]
    )
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_admin_capture_plugin_snapshot(instance_id):
        """Take a trusted, host-stored plugin safety snapshot for one tenant."""
        ok, message = capture_plugin_safety_snapshot(instance_id)
        _record_plugin_safety_event(instance_id, "snapshot_saved", ok, message)
        flash(message, "success" if ok else "error")
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route(
        "/admin/instances/<instance_id>/lift-plugin-quarantine", methods=["POST"]
    )
    @hosting_admin_required
    @hosting_rate_limit(max_requests=3, window=300)
    def hosting_admin_lift_plugin_quarantine(instance_id):
        """Lift the plugin quarantine so external plugins can be enabled again."""
        ok, message = lift_instance_plugin_quarantine(instance_id)
        _record_plugin_safety_event(instance_id, "quarantine_lifted", ok, message)
        flash(message, "success" if ok else "error")
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route("/admin/instances/<instance_id>/users/set-password", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_set_instance_user_password(instance_id):
        """Admin: create/update a user and password inside an instance."""
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        ok, reason = set_instance_user_password(
            instance_id, username, password, role=role
        )
        if not ok:
            flash(reason, "error")
        else:
            flash("Instance user has been created/updated.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/users/remove", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_remove_instance_user(instance_id):
        """Admin: remove a user from an instance."""
        username = request.form.get("username", "")
        ok, reason = remove_instance_user(instance_id, username)
        if not ok:
            flash(reason, "error")
        else:
            flash("Instance user has been removed.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/restore", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_restore_instance(instance_id):
        """Admin: restore a terminated instance from its grace-period data.

        Falls back to provisioning a fresh instance when the grace
        window has already expired (no retained data on disk).  An
        optional ``extend_days`` form field grants additional time
        beyond the platform default expiry.
        """
        extend_days_raw = (request.form.get("extend_days") or "").strip()
        extend_days = None
        if extend_days_raw:
            try:
                extend_days = int(extend_days_raw)
                if extend_days < 1 or extend_days > 365 * 100:
                    raise ValueError
            except (ValueError, TypeError):
                flash("Extension must be between 1 and 36500 days.", "error")
                return redirect(url_for("hosting_admin"))

        new_inst, error = restore_instance(instance_id, extend_days=extend_days)
        if error:
            flash(error, "error")
            return redirect(url_for("hosting_admin"))
        flash(
            f"Instance '{new_inst['subdomain']}' has been restored.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/logs", methods=["GET"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=30, window=60)
    def hosting_admin_instance_logs(instance_id):
        """Admin: view the tail of an instance's gunicorn ``error.log`` /
        ``access.log`` so 500s on a hosted wiki can be diagnosed without
        SSH.  Only the last ``_LOG_TAIL_BYTES`` bytes are returned.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        log_name = (request.args.get("log") or "error.log").strip()
        content, log_path, size_bytes, error = read_instance_log(instance_id, log_name)
        if error:
            flash(error, "error")
            return redirect(url_for("hosting_admin"))

        from ..instance_manager import _LOG_TAIL_BYTES

        truncated = size_bytes > _LOG_TAIL_BYTES
        return render_template(
            "admin_instance_logs.html",
            instance=inst,
            log_name=log_name,
            content=content,
            log_path=log_path,
            size_bytes=size_bytes,
            truncated=truncated,
            tail_bytes=_LOG_TAIL_BYTES,
        )

    @app.route("/admin/instances/<instance_id>/download", methods=["GET"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_download_instance(instance_id):
        """Admin: download a ZIP archive of a terminated instance's data.

        Only works while the instance is inside its grace window: once
        the periodic cleanup has hard-deleted the data dir, this returns
        an error.
        """
        _cleanup_stale_export_downloads()
        tmp_dir = tempfile.mkdtemp(
            prefix="bwh-archive-",
            dir=_export_download_root(),
        )
        archive_path, filename, error = build_instance_archive(
            instance_id,
            output_dir=tmp_dir,
        )
        if error:
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
            flash(error, "error")
            return redirect(url_for("hosting_admin"))

        return _stream_download_file(
            archive_path,
            filename,
            mimetype="application/zip",
            cleanup_paths=(archive_path,),
            cleanup_dirs=(tmp_dir,),
        )

    @app.route("/admin/instances/<instance_id>/suspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_suspend_instance(instance_id):
        """Admin: suspend a running or paused instance.

        Supports timed suspensions via ``suspend_duration`` form field:
        ``permanent``, a numeric hour count, ``custom_datetime``, or
        ``custom_relative``.  Optional ``suspend_reason`` and visibility
        checkboxes control what the instance owner sees.
        """
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        if inst["status"] == "terminated":
            flash("Terminated instances cannot be suspended.", "error")
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
                    return redirect(_post_instance_admin_action_redirect(instance_id))
            else:
                flash("Custom date/time is required for timed suspension.", "error")
                return redirect(_post_instance_admin_action_redirect(instance_id))
        elif duration == "custom_relative":
            hours = request.form.get("suspend_rel_hours", "0").strip()
            minutes = request.form.get("suspend_rel_minutes", "0").strip()
            try:
                h = int(hours) if hours else 0
                m = int(minutes) if minutes else 0
                if h < 0 or m < 0:
                    raise ValueError
                total_seconds = h * 3600 + m * 60
                if total_seconds > 0:
                    exp = now_dt + _td(seconds=total_seconds)
                    suspended_until = exp.isoformat()
                    duration_label = f"{h}h {m}m"
            except (ValueError, TypeError):
                flash("Invalid relative time values.", "error")
                return redirect(_post_instance_admin_action_redirect(instance_id))
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
        account_id = session.get("hosting_account_id")

        ok, reason_msg = suspend_instance(
            instance_id,
            suspended_until=suspended_until,
            reason=reason,
            reason_visible=reason_visible,
            time_visible=time_visible,
            performed_by=account_id,
        )
        if ok:
            record_instance_suspension_action(
                instance_id,
                "suspend",
                performed_by=account_id,
                reason=reason or None,
                reason_visible=reason_visible,
                time_visible=time_visible,
                duration=duration_label,
                suspended_until=suspended_until,
            )
            flash(f"Instance '{inst['subdomain']}' has been suspended.", "success")
        else:
            flash(reason_msg, "error")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/unsuspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_unsuspend_instance(instance_id):
        """Admin: unsuspend a suspended instance.

        Delegates to :func:`unsuspend_instance`, which shifts the expiry
        forward by the time spent suspended and
        then restarts the Gunicorn process so the wiki is immediately
        usable again.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        if inst["status"] != "suspended":
            flash("Only suspended instances can be unsuspended.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = unsuspend_instance(instance_id)
        account_id = session.get("hosting_account_id")
        record_instance_suspension_action(
            instance_id,
            "unsuspend",
            performed_by=account_id,
        )
        if ok:
            flash(
                f"Instance '{inst['subdomain']}' has been unsuspended and restarted.",
                "success",
            )
        else:
            flash(f"Instance unsuspended but failed to restart: {reason}", "warning")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/grace-suspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_grace_suspend_instance(instance_id):
        """Admin: suspend a terminated instance in its grace period.

        Disables the user-facing DB export until an admin unsuspends it.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        if inst["status"] != "terminated":
            flash("Only terminated instances can be grace-suspended.", "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        if not inst.get("data_retained_until"):
            flash("This instance is not in a grace period.", "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        ok = suspend_grace_period_instance(instance_id)
        if ok:
            flash(
                f"Instance '{inst['subdomain']}' grace-period export has been suspended.",
                "success",
            )
        else:
            flash(
                "Instance is already grace-period suspended or not in a valid state.",
                "error",
            )
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/grace-unsuspend", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_grace_unsuspend_instance(instance_id):
        """Admin: unsuspend a terminated instance in its grace period.

        Re-enables the user-facing DB export.
        """
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        if inst["status"] != "terminated":
            flash("Only terminated instances can be grace-unsuspended.", "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        ok = unsuspend_grace_period_instance(instance_id)
        if ok:
            flash(
                f"Instance '{inst['subdomain']}' grace-period export has been re-enabled.",
                "success",
            )
        else:
            flash("Instance is not grace-period suspended.", "error")
        return redirect(_post_instance_admin_action_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/shorten-expiry", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_shorten_expiry(instance_id):
        """Admin: shorten the expiration date of an instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        try:
            days = int(request.form.get("shorten_days") or "0")
            hours = int(request.form.get("shorten_hours") or "0")
            minutes = int(request.form.get("shorten_minutes") or "0")
            seconds = int(request.form.get("shorten_seconds") or "0")
            if days < 0 or hours < 0 or minutes < 0 or seconds < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Invalid amount of time.", "error")
            return redirect(url_for("hosting_admin"))
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        if total_seconds < 1:
            flash("Time to remove must be at least 1 second.", "error")
            return redirect(url_for("hosting_admin"))

        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        current = inst["expires_at"]
        if current:
            try:
                exp = _dt.fromisoformat(current.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_tz.utc)
            except (ValueError, TypeError):
                exp = _dt.now(_tz.utc)
        else:
            exp = _dt.now(_tz.utc)
        new_exp = exp - _td(seconds=total_seconds)
        ok, reason = set_instance_expiry(instance_id, new_exp)
        if not ok:
            flash(reason, "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        dur_parts = []
        if days:
            dur_parts.append(f"{days}d")
        if hours:
            dur_parts.append(f"{hours}h")
        if minutes:
            dur_parts.append(f"{minutes}m")
        if seconds:
            dur_parts.append(f"{seconds}s")
        dur_str = " ".join(dur_parts) or f"{total_seconds}s"
        flash(
            f"Instance '{inst['subdomain']}' expiration shortened by {dur_str}.",
            "success",
        )
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/<instance_id>/set-expiry", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_set_expiry(instance_id):
        """Admin: set a specific expiration date and time for an instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        expiry_str = request.form.get("expiry_datetime", "").strip()
        if not expiry_str:
            flash("Expiry date/time is required.", "error")
            return redirect(url_for("hosting_admin"))
        from datetime import datetime as _dt, timezone as _tz

        try:
            exp = _dt.fromisoformat(expiry_str)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
        except (ValueError, TypeError):
            flash("Invalid date/time format.", "error")
            return redirect(url_for("hosting_admin"))
        ok, reason = set_instance_expiry(instance_id, exp)
        if not ok:
            flash(reason, "error")
            return redirect(_post_instance_admin_action_redirect(instance_id))
        flash(
            f"Instance '{inst['subdomain']}' expiry set to {exp.strftime('%Y-%m-%d %H:%M UTC')}.",
            "success",
        )
        return redirect(url_for("hosting_admin"))
