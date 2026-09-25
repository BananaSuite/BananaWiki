"""Hosting dashboard: collaboration."""

import logging
from flask import (
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)
from .. import config
from .auth import hosting_login_required, hosting_rate_limit
from ..instance_manager import (
    get_instance_detail,
)
from ..db import (
    get_instance,
    get_account_by_id,
    get_account_by_username,
    ALL_PERMISSIONS,
    add_collaborator,
    remove_collaborator,
    update_collaborator,
    get_collaborators_for_instance,
    collaborator_has_permission,
    create_ownership_transfer,
    get_pending_transfer,
    accept_ownership_transfer,
    cancel_ownership_transfer,
    decline_ownership_transfer,
)

logger = logging.getLogger(__name__)
from .dashboard_common import (
    _is_owner_or_admin,
)


def register_hosting_collaboration_routes(app):
    """Register hosting routes for collaboration."""

    @app.route("/instances/<instance_id>/collaborators")
    @hosting_login_required
    def hosting_instance_collaborators(instance_id):
        """Show the collaborator management page for an instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance_detail(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        viewer_is_owner = inst["account_id"] == account_id
        if not viewer_is_owner and not is_admin:
            # Only owners and platform admins can manage collaborators
            if not collaborator_has_permission(
                instance_id, account_id, "manage_collaborators"
            ):
                flash("You do not have permission to manage collaborators.", "error")
                return redirect(
                    url_for("hosting_instance_detail", instance_id=instance_id)
                )
        collaborators = get_collaborators_for_instance(instance_id)
        pending_transfer = get_pending_transfer(instance_id)
        owner_account = get_account_by_id(inst["account_id"])
        return render_template(
            "instance_collaborators.html",
            instance=inst,
            collaborators=collaborators,
            pending_transfer=pending_transfer,
            viewer_is_owner=viewer_is_owner,
            viewer_is_admin=is_admin,
            owner_username=owner_account["username"] if owner_account else "unknown",
            all_permissions=ALL_PERMISSIONS,
            base_domain=config.BASE_DOMAIN,
            instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            hosting_mode=config.HOSTING_MODE,
        )

    @app.route("/instances/<instance_id>/collaborators/add", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=15, window=60)
    def hosting_add_collaborator(instance_id):
        """Add a collaborator to an instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        viewer_is_owner = inst["account_id"] == account_id
        if not viewer_is_owner and not is_admin:
            if not collaborator_has_permission(
                instance_id, account_id, "manage_collaborators"
            ):
                flash("You do not have permission to manage collaborators.", "error")
                return redirect(
                    url_for("hosting_instance_detail", instance_id=instance_id)
                )

        target_username = (request.form.get("username") or "").strip()
        if not target_username:
            flash("Username is required.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )
        target = get_account_by_username(target_username)
        if target is None:
            flash(f"Account '{target_username}' not found.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )
        if target["id"] == inst["account_id"]:
            flash("The instance owner cannot be added as a collaborator.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )

        role = request.form.get("role", "custom")
        if role not in ("full_access", "custom"):
            role = "custom"
        permissions = request.form.getlist("permissions")
        # full_access role doesn't need explicit permissions
        if role == "full_access":
            permissions = []

        collab = add_collaborator(
            instance_id,
            target["id"],
            invited_by=account_id,
            role=role,
            permissions=permissions,
        )
        if collab is None:
            flash(
                f"'{target_username}' is already a collaborator on this instance.",
                "error",
            )
        else:
            flash(f"'{target_username}' has been added as a collaborator.", "success")
        return redirect(
            url_for("hosting_instance_collaborators", instance_id=instance_id)
        )

    @app.route(
        "/instances/<instance_id>/collaborators/<collab_account_id>/remove",
        methods=["POST"],
    )
    @hosting_login_required
    @hosting_rate_limit(max_requests=15, window=60)
    def hosting_remove_collaborator(instance_id, collab_account_id):
        """Remove a collaborator from an instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        viewer_is_owner = inst["account_id"] == account_id
        # Allow self-removal (collaborator leaving) or owner/admin/manage_collaborators
        is_self_removal = collab_account_id == account_id
        if not is_self_removal and not viewer_is_owner and not is_admin:
            if not collaborator_has_permission(
                instance_id, account_id, "manage_collaborators"
            ):
                flash("You do not have permission to manage collaborators.", "error")
                return redirect(
                    url_for("hosting_instance_detail", instance_id=instance_id)
                )

        target = get_account_by_id(collab_account_id)
        target_name = target["username"] if target else "unknown"
        if remove_collaborator(instance_id, collab_account_id):
            if is_self_removal:
                flash("You have left this instance.", "success")
                return redirect(url_for("hosting_dashboard"))
            flash(f"'{target_name}' has been removed as a collaborator.", "success")
        else:
            flash("Collaborator not found.", "error")
        return redirect(
            url_for("hosting_instance_collaborators", instance_id=instance_id)
        )

    @app.route(
        "/instances/<instance_id>/collaborators/<collab_account_id>/update",
        methods=["POST"],
    )
    @hosting_login_required
    @hosting_rate_limit(max_requests=15, window=60)
    def hosting_update_collaborator(instance_id, collab_account_id):
        """Update a collaborator's role and permissions."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        viewer_is_owner = inst["account_id"] == account_id
        if not viewer_is_owner and not is_admin:
            if not collaborator_has_permission(
                instance_id, account_id, "manage_collaborators"
            ):
                flash("You do not have permission to manage collaborators.", "error")
                return redirect(
                    url_for("hosting_instance_detail", instance_id=instance_id)
                )

        role = request.form.get("role", "custom")
        if role not in ("full_access", "custom"):
            role = "custom"
        permissions = request.form.getlist("permissions")
        if role == "full_access":
            permissions = []

        target = get_account_by_id(collab_account_id)
        target_name = target["username"] if target else "unknown"
        if update_collaborator(
            instance_id, collab_account_id, role=role, permissions=permissions
        ):
            flash(f"Permissions for '{target_name}' have been updated.", "success")
        else:
            flash("Collaborator not found.", "error")
        return redirect(
            url_for("hosting_instance_collaborators", instance_id=instance_id)
        )

    @app.route("/instances/<instance_id>/transfer", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_initiate_transfer(instance_id):
        """Owner: initiate an ownership transfer to another user."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if not _is_owner_or_admin(inst, account_id, is_admin):
            flash("Only the instance owner can transfer ownership.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        target_username = (request.form.get("username") or "").strip()
        if not target_username:
            flash("Username is required.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )
        target = get_account_by_username(target_username)
        if target is None:
            flash(f"Account '{target_username}' not found.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )
        if target["id"] == account_id:
            flash("You already own this instance.", "error")
            return redirect(
                url_for("hosting_instance_collaborators", instance_id=instance_id)
            )

        transfer = create_ownership_transfer(instance_id, account_id, target["id"])
        if transfer:
            flash(
                f"Ownership transfer to '{target_username}' has been initiated. "
                "They must accept it from their dashboard.",
                "success",
            )
        else:
            flash("Failed to create transfer request.", "error")
        return redirect(
            url_for("hosting_instance_collaborators", instance_id=instance_id)
        )

    @app.route("/transfers/<int:transfer_id>/accept", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_accept_transfer(transfer_id):
        """Recipient: accept an ownership transfer."""
        account_id = session["hosting_account_id"]
        ok, result = accept_ownership_transfer(transfer_id, account_id)
        if ok:
            flash(
                "Ownership transfer accepted. You are now the owner of this instance.",
                "success",
            )
            return redirect(url_for("hosting_instance_detail", instance_id=result))
        error_messages = {
            "transfer_not_found": "Transfer request not found or already resolved.",
            "not_recipient": "You are not the recipient of this transfer.",
            "instance_not_found": "The instance no longer exists.",
            "owner_changed": "The instance ownership has already changed.",
        }
        flash(error_messages.get(result, "Transfer failed."), "error")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/transfers/<int:transfer_id>/decline", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_decline_transfer(transfer_id):
        """Recipient: decline an ownership transfer."""
        account_id = session["hosting_account_id"]
        if decline_ownership_transfer(transfer_id, account_id):
            flash("Ownership transfer has been declined.", "success")
        else:
            flash("Transfer request not found or already resolved.", "error")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/transfers/<int:transfer_id>/cancel", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_cancel_transfer(transfer_id):
        """Owner: cancel a pending ownership transfer."""
        account_id = session["hosting_account_id"]
        if cancel_ownership_transfer(transfer_id, account_id):
            flash("Ownership transfer has been cancelled.", "success")
        else:
            flash("Transfer request not found or already resolved.", "error")
        return redirect(url_for("hosting_dashboard"))
