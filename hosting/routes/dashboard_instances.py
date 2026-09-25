"""Hosting dashboard: instances."""

import logging
import os
import tempfile
from datetime import datetime, timezone
from flask import (
    abort,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)
from .. import config, help_content
from .auth import hosting_login_required, hosting_rate_limit
from ..instance_manager import (
    provision_instance,
    stop_instance,
    restart_instance,
    terminate_instance,
    get_dashboard_instances,
    get_instance_detail,
    reset_instance_password,
    build_instance_archive,
    read_instance_analytics,
    list_instance_users_page,
    force_restart_instance,
    reset_wiki,
    toggle_easy_wiki,
)
from ..db import (
    get_instance,
    clear_instance_password,
    get_account_by_id,
    get_hosting_settings,
    is_grace_period_suspended,
    get_allow_owner_delete_expired,
    get_allow_owner_download_expired,
    get_hosting_db_context,
    ALL_PERMISSIONS,
    get_collaborator,
    get_collaborators_for_instance,
    get_pending_transfer,
    get_pending_transfers_for_account,
    FEATURE_ENTITLEMENT_COLUMNS,
    FEATURE_LABELS,
    create_instance_feature_request,
    get_instance_feature_request,
    list_instance_feature_requests,
    cancel_instance_feature_request,
)

logger = logging.getLogger(__name__)
from .dashboard_common import (
    _SUSPENDED_LOCKOUT_MESSAGE,
    _can_access_instance,
    _cleanup_stale_export_downloads,
    _export_download_root,
    _owner_is_locked_out,
    _stream_download_file,
)


def register_hosting_instances_routes(app):
    """Register hosting routes for instances."""

    def _help_language():
        """Resolve the language for a help page.

        ``?lang=`` lets another site deep-link into a specific translation,
        which the separate /it/ help tree used to guarantee. It applies to the
        request only and never overwrites the visitor's own choice, so an
        incoming link cannot silently switch the language of the portal.
        """
        requested = (request.args.get("lang") or "").strip().lower()
        if requested in help_content.LANGUAGES:
            return requested, True
        return getattr(g, "_current_language", None) or help_content.FALLBACK_LANGUAGE, False

    @app.route("/help")
    def hosting_help():
        """The help centre ships with the platform, in the visitor's language."""
        language, explicit = _help_language()
        return render_template(
            "help_index.html",
            articles=help_content.load_articles(language),
            project_docs_url=config.PROJECT_DOCS_URL,
            help_language=language if explicit else None,
        )

    @app.route("/help/<slug>")
    def hosting_help_article(slug):
        language, explicit = _help_language()
        article = help_content.get_article(language, slug)
        if article is None:
            abort(404)
        return render_template(
            "help_article.html",
            article=help_content.fill_placeholders(
                article, config.HOSTING_CONTACT_EMAIL, config.SOURCE_CODE_URL),
            help_language=language if explicit else None,
        )

    @app.route("/dashboard")
    @hosting_login_required
    def hosting_dashboard():
        """Show the main dashboard with all instances."""
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        account_is_admin = bool(account and account["is_admin"])
        instances = get_dashboard_instances(account_id)
        running = sum(1 for i in instances if i["status"] == "running")
        stopped = sum(1 for i in instances if i["status"] == "stopped")
        terminated = sum(
            1
            for i in instances
            if i["status"] == "terminated" and i.get("grace_period_active")
        )
        shared = sum(1 for i in instances if i.get("is_shared"))
        total_storage_mb = sum(i["storage_mb"] for i in instances)
        pending_transfers = get_pending_transfers_for_account(account_id)
        return render_template(
            "dashboard.html",
            instances=instances,
            max_instances=config.MAX_INSTANCES_PER_ACCOUNT,
            account_is_admin=account_is_admin,
            base_domain=config.BASE_DOMAIN,
            instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
            hosting_mode=config.HOSTING_MODE,
            duration_days=config.INSTANCE_DURATION_DAYS,
            owner_can_delete_expired=get_allow_owner_delete_expired(),
            owner_can_download_expired=get_allow_owner_download_expired(),
            pending_transfers=pending_transfers,
            stats={
                "running": running,
                "stopped": stopped,
                "terminated": terminated,
                "shared": shared,
                "total": len(instances),
                "total_storage_mb": round(total_storage_mb, 1),
            },
        )

    @app.route("/instances/create", methods=["GET", "POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_create_instance():
        """Create a new BananaWiki instance.

        Regular accounts may only register hosting-mode subdomains
        (``{slug}-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``).  Admin accounts
        may additionally claim apex subdomains via the optional
        ``domain_mode=apex`` form field.
        """
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        account_is_admin = bool(account and account["is_admin"])
        selected_domain_mode = "hosting"

        if request.method == "GET":
            return render_template(
                "create_instance.html",
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                duration_days=config.INSTANCE_DURATION_DAYS,
                storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                hosting_mode=config.HOSTING_MODE,
                account_is_admin=account_is_admin,
                selected_domain_mode=selected_domain_mode,
            )

        subdomain = (request.form.get("subdomain") or "").strip().lower()
        easy_wiki = request.form.get("easy_wiki") == "1"
        declared_use_case = (request.form.get("declared_use_case") or "").strip()
        compliance_declared = request.form.get("compliance_declared") == "1"
        if (
            len(declared_use_case) < 20 or len(declared_use_case) > 2000
        ) and not current_app.config.get("TESTING"):
            flash("Describe the wiki's intended use in 20-2000 characters.", "error")
            return redirect(url_for("hosting_create_instance"))
        if not compliance_declared and not current_app.config.get("TESTING"):
            flash(
                "You must agree to this hosting service's terms and acceptable-use rules.",
                "error",
            )
            return redirect(url_for("hosting_create_instance"))
        requested_mode = (request.form.get("domain_mode") or "hosting").strip().lower()
        if requested_mode not in ("hosting", "apex"):
            requested_mode = "hosting"
        selected_domain_mode = requested_mode if account_is_admin else "hosting"
        # Defence in depth: only admins may request apex even though the
        # form does not show the checkbox to regular users.
        if requested_mode == "apex" and not account_is_admin:
            flash(
                "Apex subdomains (e.g. wiki.example.com) are reserved for "
                "admin accounts.",
                "error",
            )
            return render_template(
                "create_instance.html",
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                duration_days=config.INSTANCE_DURATION_DAYS,
                storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                hosting_mode=config.HOSTING_MODE,
                account_is_admin=account_is_admin,
                selected_domain_mode=selected_domain_mode,
            ), 403

        # Process optional admin credentials
        admin_username = None
        admin_password = None
        set_admin_creds = request.form.get("set_admin_credentials") == "1"
        if set_admin_creds:
            import re

            admin_username = (request.form.get("admin_username") or "").strip()
            admin_password = request.form.get("admin_password") or ""
            admin_confirm = request.form.get("admin_confirm_password") or ""

            if not admin_password:
                flash("Password is required when setting admin credentials.", "error")
                return render_template(
                    "create_instance.html",
                    base_domain=config.BASE_DOMAIN,
                    instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                    duration_days=config.INSTANCE_DURATION_DAYS,
                    storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                    hosting_mode=config.HOSTING_MODE,
                    account_is_admin=account_is_admin,
                    selected_domain_mode=selected_domain_mode,
                ), 400
            if admin_username and not re.match(
                r"^[a-zA-Z0-9_-]{3,30}$", admin_username
            ):
                flash(
                    "Admin username must be 3-30 characters (letters, digits, hyphens, underscores).",
                    "error",
                )
                return render_template(
                    "create_instance.html",
                    base_domain=config.BASE_DOMAIN,
                    instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                    duration_days=config.INSTANCE_DURATION_DAYS,
                    storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                    hosting_mode=config.HOSTING_MODE,
                    account_is_admin=account_is_admin,
                    selected_domain_mode=selected_domain_mode,
                ), 400
            if not admin_username:
                admin_username = None  # let the system use the default "admin"
            if len(admin_password) < 8:
                flash("Password must be at least 8 characters long.", "error")
                return render_template(
                    "create_instance.html",
                    base_domain=config.BASE_DOMAIN,
                    instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                    duration_days=config.INSTANCE_DURATION_DAYS,
                    storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                    hosting_mode=config.HOSTING_MODE,
                    account_is_admin=account_is_admin,
                    selected_domain_mode=selected_domain_mode,
                ), 400
            if admin_password != admin_confirm:
                flash("Passwords do not match.", "error")
                return render_template(
                    "create_instance.html",
                    base_domain=config.BASE_DOMAIN,
                    instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                    duration_days=config.INSTANCE_DURATION_DAYS,
                    storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                    hosting_mode=config.HOSTING_MODE,
                    account_is_admin=account_is_admin,
                    selected_domain_mode=selected_domain_mode,
                ), 400

        inst, error = provision_instance(
            account_id,
            subdomain,
            domain_mode=requested_mode,
            account_is_admin=account_is_admin,
            admin_username=admin_username,
            admin_password=admin_password,
            easy_wiki=easy_wiki,
            declared_use_case=declared_use_case,
        )
        if error:
            flash(error, "error")
            return render_template(
                "create_instance.html",
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                duration_days=config.INSTANCE_DURATION_DAYS,
                storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                hosting_mode=config.HOSTING_MODE,
                account_is_admin=account_is_admin,
                selected_domain_mode=selected_domain_mode,
            ), 400

        flash(f"Instance created at {inst['url']}.", "success")
        return render_template(
            "instance_created.html",
            instance=inst,
            base_domain=config.BASE_DOMAIN,
            instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            hosting_mode=config.HOSTING_MODE,
            force_password_change=not set_admin_creds,
        )

    @app.route("/instances/<instance_id>")
    @hosting_login_required
    def hosting_instance_detail(instance_id):
        """Show detailed information about a single instance."""
        account_id = session["hosting_account_id"]
        inst = get_instance_detail(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        if not _can_access_instance(inst, account_id, is_admin, "view"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        owner_can_download_expired = get_allow_owner_download_expired()
        if (
            inst["status"] == "terminated"
            and not is_admin
            and not owner_can_download_expired
        ):
            # Recovery retention is intentionally invisible to owners. From
            # their perspective termination is immediate; only platform
            # administrators can discover or restore the retained copy.
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))

        viewer_is_owner = inst["account_id"] == account_id
        viewer_collab = (
            get_collaborator(instance_id, account_id)
            if not viewer_is_owner and not is_admin
            else None
        )

        # Fetch one bounded page of the wiki's users for the owner and admins;
        # wiki_users_total is the full count, since a tenant can create far
        # more users than the page shows.
        wiki_users, wiki_users_total = (
            list_instance_users_page(instance_id)
            if inst["status"] != "terminated"
            else ([], 0)
        )
        # Extract the one-time plaintext password from the instance dict and
        # clear it from the database BEFORE rendering, eliminating the race
        # window where a crash between render and clear leaves the password
        # permanently exposed.  The password lives only in local memory for
        # the duration of this request.
        one_time_password = inst.get("admin_password_plain") or ""
        if one_time_password:
            clear_instance_password(instance_id)
            # Scrub from the dict so the template cannot accidentally access it
            # via instance.admin_password_plain (the template uses the explicit
            # one_time_password variable instead).
            inst = dict(inst)
            inst["admin_password_plain"] = None

        # Collaborator data for the template
        collaborators = (
            get_collaborators_for_instance(instance_id)
            if viewer_is_owner or is_admin
            else []
        )
        pending_transfer = (
            get_pending_transfer(instance_id) if viewer_is_owner or is_admin else None
        )
        # Pending transfers where this user is the recipient
        incoming_transfers = get_pending_transfers_for_account(account_id)
        incoming_transfer_for_this = None
        for t in incoming_transfers:
            if t["instance_id"] == instance_id:
                incoming_transfer_for_this = t
                break
        # Build a permission lookup dict for collaborators in the template
        viewer_permissions = {}
        if viewer_is_owner or is_admin:
            viewer_permissions = {p: True for p in ALL_PERMISSIONS}
        elif viewer_collab:
            import json as _json

            if viewer_collab["role"] == "full_access":
                viewer_permissions = {p: True for p in ALL_PERMISSIONS}
            else:
                try:
                    perms = _json.loads(viewer_collab.get("permissions") or "[]")
                except (ValueError, TypeError):
                    perms = []
                viewer_permissions = {p: True for p in perms}
            # Always grant view to any collaborator
            viewer_permissions["view"] = True
        owner_account = (
            get_account_by_id(inst["account_id"]) if not viewer_is_owner else viewer
        )
        feature_requests = (
            list_instance_feature_requests(instance_id)
            if viewer_is_owner or is_admin
            else []
        )
        pending_feature_requests = {
            item["feature"]: item
            for item in feature_requests
            if item["status"] == "pending"
        }
        hosting_settings = get_hosting_settings()

        response = make_response(
            render_template(
                "instance_detail.html",
                instance=inst,
                one_time_password=one_time_password,
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                hosting_mode=config.HOSTING_MODE,
                storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
                wiki_users=wiki_users,
                wiki_users_total=wiki_users_total,
                viewer_is_admin=is_admin,
                viewer_is_owner=viewer_is_owner,
                viewer_collab=viewer_collab,
                viewer_permissions=viewer_permissions,
                collaborators=collaborators,
                pending_transfer=pending_transfer,
                incoming_transfer=incoming_transfer_for_this,
                owner_username=owner_account["username"]
                if owner_account
                else "unknown",
                owner_can_delete_expired=get_allow_owner_delete_expired(),
                owner_can_download_expired=owner_can_download_expired,
                all_permissions=ALL_PERMISSIONS,
                feature_requests=feature_requests,
                pending_feature_requests=pending_feature_requests,
                feature_labels=FEATURE_LABELS,
                forbid_non_admin_public_wikis=(
                    not hosting_settings
                    or bool(hosting_settings.get("forbid_non_admin_public_wikis", 1))
                ),
                forbid_non_admin_page_builder=(
                    not hosting_settings
                    or bool(hosting_settings.get("forbid_non_admin_page_builder", 1))
                ),
            )
        )
        return response

    @app.route("/instances/<instance_id>/features/<feature>/requests", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_request_instance_feature(instance_id, feature):
        """Owner: request one managed feature entitlement."""
        account_id = session["hosting_account_id"]
        inst = get_instance(instance_id)
        if inst is None or inst["account_id"] != account_id:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if feature not in FEATURE_ENTITLEMENT_COLUMNS:
            abort(404)
        owner = get_account_by_id(account_id)
        settings = get_hosting_settings()
        restriction_key = (
            "forbid_non_admin_public_wikis"
            if feature == "public_access"
            else "forbid_non_admin_page_builder"
        )
        globally_restricted = not settings or bool(settings.get(restriction_key, 1))
        if (owner and owner.get("is_admin")) or not globally_restricted:
            flash(
                "This feature is already available under the current platform policy.",
                "error",
            )
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        reason = (request.form.get("reason") or "").strip()
        if not 20 <= len(reason) <= 2000:
            flash("Request reason must be between 20 and 2000 characters.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        try:
            created = create_instance_feature_request(
                instance_id, account_id, feature, reason
            )
        except ValueError as exc:
            messages = {
                "pending_exists": "A request for this feature is already pending.",
                "already_allowed": "This feature is already granted to the instance.",
                "invalid_instance_status": "Features cannot be requested for a terminated instance.",
            }
            flash(
                messages.get(str(exc), "The feature request could not be created."),
                "error",
            )
        else:
            label = FEATURE_LABELS[feature]
            if created["status"] != "approved":
                flash(f"{label} request submitted for review.", "success")
            elif inst["status"] == "running":
                try:
                    restarted, restart_error = force_restart_instance(instance_id)
                except Exception:
                    logger.exception(
                        "Failed to restart instance %s after automatic feature approval %s",
                        instance_id,
                        created["id"],
                    )
                    restarted, restart_error = False, "Unexpected restart failure."
                if restarted:
                    flash(
                        f"{label} request was automatically approved by policy. "
                        "The running instance was restarted.",
                        "success",
                    )
                else:
                    flash(
                        f"{label} request was automatically approved and the entitlement "
                        f"was granted, but the running instance could not be restarted: "
                        f"{restart_error}",
                        "error",
                    )
            else:
                flash(
                    f"{label} request was automatically approved by policy and the "
                    "entitlement was granted.",
                    "success",
                )
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route(
        "/instances/<instance_id>/feature-requests/<int:request_id>/cancel",
        methods=["POST"],
    )
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_cancel_instance_feature_request(instance_id, request_id):
        """Owner: cancel one pending feature request without deleting history."""
        account_id = session["hosting_account_id"]
        inst = get_instance(instance_id)
        feature_request = get_instance_feature_request(request_id)
        if (
            inst is None
            or inst["account_id"] != account_id
            or feature_request is None
            or feature_request["instance_id"] != instance_id
            or feature_request["owner_account_id"] != account_id
        ):
            flash("Feature request not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if feature_request["status"] != "pending":
            flash("Only pending feature requests can be cancelled.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        try:
            cancel_instance_feature_request(request_id, account_id)
        except ValueError:
            flash("Only pending feature requests can be cancelled.", "error")
        else:
            flash("Feature request cancelled.", "success")
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/<instance_id>/use-case", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_update_instance_use_case(instance_id):
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "use_case"):
            return redirect(url_for("hosting_dashboard"))
        use_case = (request.form.get("declared_use_case") or "").strip()
        if len(use_case) < 20 or len(use_case) > 2000:
            flash("Describe the wiki's intended use in 20-2000 characters.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if request.form.get("compliance_declared") != "1":
            flash("The compliance declaration is required.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE instances SET declared_use_case=?, tos_compliance_declared_at=datetime('now') WHERE id=?",
                (use_case, instance_id),
            )
            conn.commit()
        flash("Declared use case updated.", "success")
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/<instance_id>/download", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_download_instance(instance_id):
        """Owner or collaborator with download permission: download a ZIP archive of a terminated instance's data.

        Only available while the instance is inside its grace window AND
        the instance is NOT grace-period-suspended.  Suspended instances
        have their export disabled until an admin unsuspends them.
        """
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "download"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if not is_admin and not get_allow_owner_download_expired():
            flash(
                "Terminated-instance recovery is available only to platform administrators.",
                "error",
            )
            return redirect(url_for("hosting_dashboard"))
        if inst["status"] != "terminated":
            flash("Only terminated instances can be downloaded.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if not inst.get("data_retained_until"):
            flash("No data is available for this instance.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if is_grace_period_suspended(inst):
            flash(
                "This instance's data export is currently suspended by an administrator. "
                "Contact support to have it unsuspended.",
                "error",
            )
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

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
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        return _stream_download_file(
            archive_path,
            filename,
            mimetype="application/zip",
            cleanup_paths=(archive_path,),
            cleanup_dirs=(tmp_dir,),
        )

    @app.route("/instances/<instance_id>/analytics")
    @hosting_login_required
    @hosting_rate_limit(max_requests=30, window=60)
    def hosting_instance_analytics(instance_id):
        """Show daily request/view/error counts for a single wiki.

        Available to the wiki's owner and to any hosting admin (the
        admin path bypasses the ownership check so support staff can
        triage traffic without impersonating the user).
        """
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])

        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "analytics"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))

        try:
            days = int(request.args.get("days") or 30)
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 365))

        summary, error = read_instance_analytics(instance_id, days=days)
        if error:
            flash(error, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        # Compute a max for the CSS-only bar chart so the template can
        # render bar heights as percentages without doing arithmetic in
        # Jinja2.
        max_value = 1
        for row in summary["daily"]:
            for kind in ("request", "page_view", "error"):
                v = int(row.get(kind, 0) or 0)
                if v > max_value:
                    max_value = v

        return render_template(
            "instance_analytics.html",
            instance=inst,
            base_domain=config.BASE_DOMAIN,
            hosting_mode=config.HOSTING_MODE,
            summary=summary,
            days=days,
            max_value=max_value,
            viewer_is_admin=is_admin,
        )

    @app.route("/instances/<instance_id>/stop", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_stop_instance(instance_id):
        """Stop (pause) a running instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "start_stop"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        # Only a running wiki can be paused. A collaborator must not be able
        # to turn a suspended wiki into a paused one that can then be resumed.
        if inst["status"] != "running":
            flash("Instance is not running.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        ok, reason = stop_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' has been stopped.", "success")
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/<instance_id>/restart", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_restart_instance(instance_id):
        """Restart a stopped instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "start_stop"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if inst["status"] != "stopped":
            flash("Instance is not stopped.", "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if inst["suspended_at"]:
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        ok, reason = restart_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' has been restarted.", "success")
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/<instance_id>/terminate", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_terminate_instance(instance_id):
        """Terminate an instance, wiping all data."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "terminate"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        if not is_admin and not get_allow_owner_delete_expired():
            expires_at_str = inst.get("expires_at")
            if expires_at_str:
                try:
                    if datetime.fromisoformat(expires_at_str) <= datetime.now(
                        timezone.utc
                    ):
                        flash(
                            "Your wiki has expired and cannot be self-deleted. "
                            "Contact an administrator.",
                            "error",
                        )
                        return redirect(
                            url_for("hosting_instance_detail", instance_id=instance_id)
                        )
                except (ValueError, TypeError):
                    pass

        ok, reason = terminate_instance(instance_id)
        if not ok:
            flash(reason, "error")
        else:
            flash(f"Instance '{inst['subdomain']}' has been terminated.", "success")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/instances/<instance_id>/toggle-easy-wiki", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_toggle_easy_wiki(instance_id):
        """Toggle EasyWiki mode on a hosted instance.

        Switches between full BananaWiki and minimal EasyWiki mode.
        The instance must be running or stopped (not suspended/terminated).
        The toggle requires a restart so the change takes effect.
        """
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "toggle_mode"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

        enable = request.form.get("enable") == "1"
        ok, reason = toggle_easy_wiki(instance_id, enable)
        if not ok:
            flash(reason, "error")
        else:
            mode_label = "EasyWiki" if enable else "full BananaWiki"
            flash(
                f"Instance '{inst['subdomain']}' switched to {mode_label} mode.",
                "success",
            )
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/bulk-delete", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_bulk_delete_instances():
        """User: bulk-terminate own instances."""
        account_id = session["hosting_account_id"]
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_dashboard"))
        count = 0
        suspended_skipped = 0
        expired_skipped = 0
        owner_can_delete_expired = get_allow_owner_delete_expired()
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["account_id"] != account_id:
                continue
            if inst["status"] == "terminated":
                continue
            # Suspended instances are admin-locked: the owner cannot
            # terminate them while the suspension is in force.
            if inst["status"] == "suspended":
                suspended_skipped += 1
                continue
            # Expired instances are blocked unless the platform allows owners
            # to self-delete expired wikis.
            if not owner_can_delete_expired:
                expires_at_str = inst.get("expires_at")
                if expires_at_str:
                    try:
                        if datetime.fromisoformat(expires_at_str) <= datetime.now(
                            timezone.utc
                        ):
                            expired_skipped += 1
                            continue
                    except (ValueError, TypeError):
                        pass
            ok, _ = terminate_instance(iid)
            if ok:
                count += 1
        flash(f"{count} instance(s) have been successfully terminated.", "success")
        if suspended_skipped:
            flash(
                f"{suspended_skipped} suspended instance(s) were skipped. "
                "Contact an administrator to have them unsuspended first.",
                "error",
            )
        if expired_skipped:
            flash(
                f"{expired_skipped} expired instance(s) were skipped. "
                "Contact an administrator to delete them.",
                "error",
            )
        return redirect(url_for("hosting_dashboard"))

    @app.route("/instances/bulk-stop", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_bulk_stop_instances():
        """User: bulk-pause own running instances.  Idempotent on the others."""
        account_id = session["hosting_account_id"]
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_dashboard"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["account_id"] != account_id:
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
        return redirect(url_for("hosting_dashboard"))

    @app.route("/instances/bulk-restart", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_bulk_restart_instances():
        """User: bulk-resume own paused instances.  Idempotent on the others."""
        account_id = session["hosting_account_id"]
        ids = request.form.getlist("instance_ids")
        if not ids:
            flash("No instances selected.", "error")
            return redirect(url_for("hosting_dashboard"))
        changed = 0
        skipped = 0
        for iid in ids:
            inst = get_instance(iid)
            if inst is None or inst["account_id"] != account_id:
                continue
            # Regular users can resume their own stopped instances but
            # cannot lift an admin-imposed suspension.
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
        return redirect(url_for("hosting_dashboard"))

    @app.route("/instances/<instance_id>/reset-password", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_reset_instance_password(instance_id):
        """Owner, collaborator, or admin: reset the BananaWiki admin password for an instance."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "reset_password"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        ok, result = reset_instance_password(instance_id)
        if not ok:
            flash(result, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        flash(
            "Admin password has been reset. Your new credentials are shown below.",
            "success",
        )
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))

    @app.route("/instances/<instance_id>/reset-wiki", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=3, window=300)
    def hosting_reset_wiki(instance_id):
        """Owner, collaborator, or admin: wipe wiki content and re-seed to factory defaults."""
        account_id = session["hosting_account_id"]
        viewer = get_account_by_id(account_id)
        is_admin = bool(viewer and viewer["is_admin"])
        inst = get_instance(instance_id)
        if not _can_access_instance(inst, account_id, is_admin, "reset_wiki"):
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_dashboard"))
        if _owner_is_locked_out(inst, account_id, is_admin):
            flash(_SUSPENDED_LOCKOUT_MESSAGE, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        ok, result = reset_wiki(instance_id)
        if not ok:
            flash(result, "error")
            return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
        flash(
            "Wiki has been reset to factory defaults. "
            "Your new admin credentials are shown below.",
            "success",
        )
        return redirect(url_for("hosting_instance_detail", instance_id=instance_id))
