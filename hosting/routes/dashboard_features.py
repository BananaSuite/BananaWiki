"""Hosting dashboard: features."""

import logging
from flask import (
    abort,
    redirect,
    request,
    session,
    url_for,
    flash,
)
from .auth import hosting_admin_required, hosting_rate_limit
from ..instance_manager import (
    force_restart_instance,
)
from ..db import (
    get_instance,
    FEATURE_ENTITLEMENT_COLUMNS,
    FEATURE_LABELS,
    get_instance_feature_request,
    review_instance_feature_request,
    set_instance_feature_entitlement,
)

logger = logging.getLogger(__name__)


def register_hosting_features_routes(app):
    """Register hosting routes for features."""

    def _review_feature_request(request_id, decision):
        feature_request = get_instance_feature_request(request_id)
        if feature_request is None:
            flash("Feature request not found.", "error")
            return redirect(url_for("hosting_admin"))
        if feature_request["status"] != "pending":
            flash("This feature request has already been resolved.", "error")
            return redirect(
                url_for(
                    "hosting_admin_instance_manage",
                    instance_id=feature_request["instance_id"],
                )
            )
        if (
            decision == "approved"
            and feature_request["instance_status"] == "terminated"
        ):
            flash("A feature cannot be approved for a terminated instance.", "error")
            return redirect(url_for("hosting_admin"))
        review_note = (request.form.get("review_note") or "").strip()
        if len(review_note) > 2000:
            flash("Review note must not exceed 2000 characters.", "error")
            return redirect(url_for("hosting_admin"))
        try:
            reviewed = review_instance_feature_request(
                request_id,
                session["hosting_account_id"],
                decision,
                review_note,
            )
        except ValueError as exc:
            if str(exc) == "not_pending":
                flash("This feature request has already been resolved.", "error")
            else:
                flash("The feature request could not be reviewed.", "error")
            return redirect(url_for("hosting_admin"))

        label = FEATURE_LABELS[reviewed["feature"]]
        if decision == "denied":
            flash(f"{label} request denied.", "success")
            return redirect(
                url_for(
                    "hosting_admin_instance_manage",
                    instance_id=reviewed["instance_id"],
                )
            )

        inst = get_instance(reviewed["instance_id"])
        if inst and inst["status"] == "running":
            try:
                restarted, restart_error = force_restart_instance(
                    reviewed["instance_id"]
                )
            except Exception:
                logger.exception(
                    "Failed to restart instance %s after approving feature request %s",
                    reviewed["instance_id"],
                    request_id,
                )
                restarted, restart_error = False, "Unexpected restart failure."
            if not restarted:
                flash(
                    f"{label} request approved and entitlement granted, but the running "
                    f"instance could not be restarted: {restart_error}",
                    "error",
                )
            else:
                flash(
                    f"{label} request approved. The running instance was restarted.",
                    "success",
                )
        else:
            flash(f"{label} request approved and entitlement granted.", "success")
        return redirect(
            url_for(
                "hosting_admin_instance_manage", instance_id=reviewed["instance_id"]
            )
        )

    @app.route("/admin/feature-requests/<int:request_id>/approve", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_approve_feature_request(request_id):
        """Admin: approve a pending request and grant its entitlement."""
        return _review_feature_request(request_id, "approved")

    @app.route("/admin/feature-requests/<int:request_id>/deny", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_deny_feature_request(request_id):
        """Admin: deny a pending request without changing entitlement."""
        return _review_feature_request(request_id, "denied")

    def _feature_admin_redirect(instance_id):
        ref = request.referrer or ""
        if f"/admin/instances/{instance_id}/manage" in ref:
            return url_for("hosting_admin_instance_manage", instance_id=instance_id)
        return url_for("hosting_admin")

    def _set_feature_entitlement_and_restart(instance_id, feature, allowed):
        """Apply an entitlement and restart a running tenant for its new env."""
        inst = get_instance(instance_id)
        if inst is None:
            return False, "Instance not found.", False
        if inst["status"] == "terminated":
            return (
                False,
                "Entitlements cannot be changed for a terminated instance.",
                False,
            )
        if not set_instance_feature_entitlement(instance_id, feature, allowed):
            return False, "The entitlement could not be updated.", False
        if inst["status"] == "running":
            try:
                restarted, restart_error = force_restart_instance(instance_id)
            except Exception:
                logger.exception(
                    "Failed to restart instance %s after %s entitlement change",
                    instance_id,
                    feature,
                )
                restarted, restart_error = False, "Unexpected restart failure."
            if not restarted:
                return True, restart_error or "The instance did not restart.", False
            return True, "", True
        return True, "", False

    @app.route(
        "/admin/instances/<instance_id>/features/<feature>/entitlement",
        methods=["POST"],
    )
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_set_feature_entitlement(instance_id, feature):
        """Admin: directly grant or revoke either managed feature."""
        if feature not in FEATURE_ENTITLEMENT_COLUMNS:
            abort(404)
        allowed_raw = request.form.get("allowed")
        if allowed_raw not in {"0", "1"}:
            flash("Invalid entitlement value.", "error")
            return redirect(_feature_admin_redirect(instance_id))
        allowed = allowed_raw == "1"
        changed, detail, restarted = _set_feature_entitlement_and_restart(
            instance_id, feature, allowed
        )
        label = FEATURE_LABELS[feature]
        if not changed:
            flash(detail, "error")
        elif detail:
            flash(
                f"{label} was {'granted' if allowed else 'revoked'}, but the running "
                f"instance could not be restarted: {detail}",
                "error",
            )
        else:
            suffix = " The running instance was restarted." if restarted else ""
            flash(f"{label} {'granted' if allowed else 'revoked'}.{suffix}", "success")
        return redirect(_feature_admin_redirect(instance_id))

    @app.route("/admin/instances/<instance_id>/toggle-public-wiki", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_toggle_public_wiki(instance_id):
        """Compatibility endpoint for the former public-access toggle."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))
        allowed = not bool(inst.get("public_wiki_allowed"))
        changed, detail, restarted = _set_feature_entitlement_and_restart(
            instance_id, "public_access", allowed
        )
        if not changed:
            flash(detail, "error")
        elif detail:
            flash(
                f"Public access was {'granted' if allowed else 'revoked'}, but the "
                f"running instance could not be restarted: {detail}",
                "error",
            )
        else:
            suffix = " The running instance was restarted." if restarted else ""
            flash(
                f"Public access {'granted' if allowed else 'revoked'}.{suffix}",
                "success",
            )
        return redirect(_feature_admin_redirect(instance_id))
