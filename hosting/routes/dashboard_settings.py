"""Hosting dashboard: settings."""

import logging
import re
from flask import (
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
)
from helpers._translations import t as translate

from .. import config
from .auth import hosting_admin_required, hosting_rate_limit
from ..instance_manager import (
    provision_demo_instances,
    stop_instance,
    get_admin_instances,
    get_platform_stats,
    force_restart_instance,
    cleanup_expired_instance_suspensions,
    apply_upload_policy_to_all_instances,
)
from ..db import (
    get_all_accounts,
    list_invite_codes,
    get_signup_mode,
    get_hosting_settings,
    cleanup_expired_account_suspensions,
    get_pending_accounts,
    get_hosting_activation_required,
    get_hosting_activation_denied_timeout_seconds,
    get_hosting_activation_denied_deletion_enabled,
    approve_all_pending_hosting_accounts,
    FEATURE_LABELS,
    list_pending_instance_feature_requests,
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def register_hosting_settings_routes(app):
    """Register hosting routes for settings."""

    @app.route("/admin")
    @hosting_admin_required
    def hosting_admin():
        """Admin dashboard showing all users and instances."""
        cleanup_expired_instance_suspensions()
        cleanup_expired_account_suspensions()
        accounts = get_all_accounts()
        instances = get_admin_instances()
        stats = get_platform_stats()
        signup_mode = get_signup_mode()
        invite_codes = list_invite_codes()
        pending_accounts = get_pending_accounts()
        activation_required = get_hosting_activation_required()
        denied_timeout = get_hosting_activation_denied_timeout_seconds()
        denied_deletion_enabled = get_hosting_activation_denied_deletion_enabled()
        settings = get_hosting_settings()
        signup_use_case_required = bool(
            settings and settings.get("signup_use_case_required")
        )
        pending_feature_requests = list_pending_instance_feature_requests()
        return render_template(
            "admin_dashboard.html",
            accounts=accounts,
            instances=instances,
            stats=stats,
            storage_limit_mb=config.INSTANCE_STORAGE_LIMIT_MB,
            hosting_mode=config.HOSTING_MODE,
            base_domain=config.BASE_DOMAIN,
            instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            duration_days=config.INSTANCE_DURATION_DAYS,
            signup_mode=signup_mode,
            invite_codes=invite_codes,
            pending_accounts=pending_accounts,
            activation_required=activation_required,
            denied_timeout=denied_timeout,
            denied_deletion_enabled=denied_deletion_enabled,
            signup_use_case_required=signup_use_case_required,
            pending_feature_requests=pending_feature_requests,
            feature_labels=FEATURE_LABELS,
        )

    @app.route("/admin/settings", methods=["GET", "POST"])
    @app.route("/global-settings", methods=["GET", "POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_admin_settings():
        """Admin: view and update hosting platform settings."""
        from ..db import get_hosting_settings, update_hosting_settings

        if request.method == "POST":
            action = request.form.get("action", "save")

            if action == "save_limits":
                # Save global capacity limits
                enabled = 1 if request.form.get("global_limit_enabled") else 0
                try:
                    max_instances = int(
                        request.form.get("global_limit_max_instances") or 50
                    )
                    if max_instances < 1:
                        raise ValueError
                except (ValueError, TypeError):
                    flash("Maximum instances must be at least 1.", "error")
                    settings = get_hosting_settings()
                    return render_template("admin_settings.html", settings=settings)

                try:
                    max_storage = int(
                        request.form.get("global_limit_max_storage_mb") or 5000
                    )
                    if max_storage < 1:
                        raise ValueError
                except (ValueError, TypeError):
                    flash("Maximum storage must be at least 1 MB.", "error")
                    settings = get_hosting_settings()
                    return render_template("admin_settings.html", settings=settings)

                update_hosting_settings(
                    global_limit_enabled=enabled,
                    global_limit_max_instances=max_instances,
                    global_limit_max_storage_mb=max_storage,
                )
                flash("Global capacity limits have been saved successfully.", "success")

            elif action == "save_bot_protection":
                # Save bot protection toggle
                update_hosting_settings(
                    bot_protection_enabled=1
                    if request.form.get("bot_protection_enabled")
                    else 0,
                )
                flash(
                    "Bot protection settings have been saved successfully.", "success"
                )

            elif action == "save_account_communications":
                try:
                    resend_cooldown = int(
                        request.form.get("email_verification_cooldown_seconds") or 60
                    )
                    if resend_cooldown < 30 or resend_cooldown > 3600:
                        raise ValueError
                except (TypeError, ValueError):
                    flash(
                        "Email resend cooldown must be between 30 and 3600 seconds.",
                        "error",
                    )
                    settings = get_hosting_settings()
                    return render_template(
                        "admin_settings.html", settings=settings
                    ), 400
                previous_settings = get_hosting_settings() or {}
                approval_notify_email = (
                    request.form.get("approval_notify_email") or ""
                ).strip()
                if approval_notify_email and (
                    len(approval_notify_email) > 254
                    or not _EMAIL_RE.match(approval_notify_email)
                ):
                    flash(
                        "Notification email must be a valid email address.",
                        "error",
                    )
                    settings = get_hosting_settings()
                    return render_template(
                        "admin_settings.html", settings=settings
                    ), 400
                approval_notify_mode = (
                    request.form.get("approval_notify_mode") or "digest"
                ).strip().lower()
                if approval_notify_mode not in ("immediate", "digest"):
                    flash(
                        "Notification mode must be immediate or digest.",
                        "error",
                    )
                    settings = get_hosting_settings()
                    return render_template(
                        "admin_settings.html", settings=settings
                    ), 400
                try:
                    digest_hours = int(
                        request.form.get("approval_notify_digest_hours") or 6
                    )
                    if digest_hours < 1 or digest_hours > 72:
                        raise ValueError
                except (TypeError, ValueError):
                    flash(
                        "Digest interval must be between 1 and 72 hours.",
                        "error",
                    )
                    settings = get_hosting_settings()
                    return render_template(
                        "admin_settings.html", settings=settings
                    ), 400
                forbid_public_wikis = (
                    1 if request.form.get("forbid_non_admin_public_wikis") else 0
                )
                forbid_page_builder = (
                    1 if request.form.get("forbid_non_admin_page_builder") else 0
                )
                auto_approve_public = (
                    1 if request.form.get("auto_approve_public_access_requests") else 0
                )
                auto_approve_page_builder = (
                    1 if request.form.get("auto_approve_page_builder_requests") else 0
                )
                policy_changed = bool(
                    previous_settings.get("forbid_non_admin_public_wikis", 1)
                ) != bool(forbid_public_wikis) or bool(
                    previous_settings.get("forbid_non_admin_page_builder", 1)
                ) != bool(forbid_page_builder)
                update_hosting_settings(
                    ask_email_new_signup=1
                    if request.form.get("ask_email_new_signup")
                    else 0,
                    ask_email_existing_users=1
                    if request.form.get("ask_email_existing_users")
                    else 0,
                    email_required=1 if request.form.get("email_required") else 0,
                    email_verification_required=1
                    if request.form.get("email_verification_required")
                    else 0,
                    email_verification_cooldown_seconds=resend_cooldown,
                    approval_notify_email=approval_notify_email,
                    approval_notify_mode=approval_notify_mode,
                    approval_notify_digest_hours=digest_hours,
                    forbid_non_admin_public_wikis=forbid_public_wikis,
                    forbid_non_admin_page_builder=forbid_page_builder,
                    auto_approve_public_access_requests=auto_approve_public,
                    auto_approve_page_builder_requests=auto_approve_page_builder,
                )
                flash(
                    "Account communication and wiki visibility policies saved.",
                    "success",
                )
                if policy_changed:
                    restart_failures = []
                    for managed_instance in get_admin_instances():
                        if managed_instance["status"] != "running":
                            continue
                        try:
                            ok, reason = force_restart_instance(managed_instance["id"])
                        except Exception as exc:
                            ok, reason = False, str(exc)
                        if not ok:
                            # A process that still has the previous environment
                            # must not keep running after a policy revocation.
                            if forbid_public_wikis or forbid_page_builder:
                                try:
                                    stop_instance(managed_instance["id"])
                                except Exception:
                                    pass
                            restart_failures.append(
                                f"{managed_instance['subdomain']}: {reason or 'restart failed'}"
                            )
                    if restart_failures:
                        flash(
                            "Policy saved, but some running wikis could not be restarted: "
                            + "; ".join(restart_failures),
                            "error",
                        )
                    else:
                        flash(
                            "Running wikis were restarted to apply the new feature policy.",
                            "info",
                        )

            elif action == "save_wiki_upload_policy":
                try:
                    upload_max_size_mb = int(
                        request.form.get("global_wiki_upload_max_size_mb") or 100
                    )
                    if upload_max_size_mb < 1 or upload_max_size_mb > 2048:
                        raise ValueError
                except (ValueError, TypeError):
                    flash(
                        "Wiki attachment size must be between 1 and 2048 MB.", "error"
                    )
                    settings = get_hosting_settings()
                    return render_template("admin_settings.html", settings=settings)
                blocked_extensions = (
                    request.form.get("global_wiki_blocked_extensions") or ""
                ).strip()[:2000]
                update_hosting_settings(
                    global_wiki_upload_max_size_mb=upload_max_size_mb,
                    global_wiki_blocked_extensions=blocked_extensions,
                )
                result = apply_upload_policy_to_all_instances(restart_running=True)
                flash(
                    "Wiki upload policy saved. "
                    f"Synced {result['synced']} active wiki(s); restarted {result['restarted']} running wiki(s).",
                    "success" if result["failed"] == 0 else "warning",
                )

            elif action == "save_grace_period":
                # Save grace period (days) for terminated/expired instance data retention
                try:
                    grace_days = int(request.form.get("grace_period_days") or 30)
                    if grace_days < 0 or grace_days > 3650:
                        raise ValueError
                except (ValueError, TypeError):
                    flash("Grace period must be between 0 and 3650 days.", "error")
                    settings = get_hosting_settings()
                    return render_template("admin_settings.html", settings=settings)

                update_hosting_settings(grace_period_days=grace_days)
                flash(
                    f"Grace period saved: terminated instance data will be kept for {grace_days} day(s) before deletion.",
                    "success",
                )

            elif action == "save_activation":
                old_activation = get_hosting_activation_required()
                activation_on = (
                    1 if request.form.get("hosting_activation_required") else 0
                )

                # Parse timeout: hours, minutes, seconds
                try:
                    td_hours = int(request.form.get("act_timeout_hours") or 0)
                    td_minutes = int(request.form.get("act_timeout_minutes") or 0)
                    td_seconds = int(request.form.get("act_timeout_seconds") or 0)
                    if td_hours < 0 or td_minutes < 0 or td_seconds < 0:
                        raise ValueError
                    total_seconds = td_hours * 3600 + td_minutes * 60 + td_seconds
                except (ValueError, TypeError):
                    flash("Invalid timeout values.", "error")
                    return redirect(url_for("hosting_admin"))

                deletion_enabled = (
                    1
                    if request.form.get("hosting_activation_denied_deletion_enabled")
                    else 0
                )
                use_case_on = 1 if request.form.get("signup_use_case_required") else 0
                if deletion_enabled:
                    update_hosting_settings(
                        hosting_activation_required=activation_on,
                        hosting_activation_denied_timeout_seconds=total_seconds,
                        signup_use_case_required=use_case_on,
                    )
                else:
                    # -1 means never delete
                    update_hosting_settings(
                        hosting_activation_required=activation_on,
                        hosting_activation_denied_timeout_seconds=-1,
                        signup_use_case_required=use_case_on,
                    )

                # If activation was turned off, auto-approve all pending accounts
                if old_activation and not activation_on:
                    admin_id = session.get("hosting_account_id")
                    approved = approve_all_pending_hosting_accounts(admin_id=admin_id)
                    if approved:
                        flash(f"Auto-approved {approved} pending account(s).", "info")

                flash("Activation approval settings saved.", "success")
                return redirect(url_for("hosting_admin"))

            elif action == "save_instance_url_suffix":
                import re as _re

                raw_suffix = (
                    (request.form.get("instance_url_suffix") or "").strip().lower()
                )
                suffix_disabled = (
                    1 if request.form.get("instance_suffix_disabled") else 0
                )
                # Validate suffix if provided
                if raw_suffix and not _re.match(
                    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", raw_suffix
                ):
                    flash(
                        "Invalid suffix. Use only lowercase letters, numbers, and hyphens.",
                        "error",
                    )
                elif len(raw_suffix) > 30:
                    flash("Suffix too long (max 30 characters).", "error")
                else:
                    update_hosting_settings(
                        instance_url_suffix=raw_suffix or "hosting",
                        instance_suffix_disabled=suffix_disabled,
                    )
                    # Apply immediately to the running process
                    if suffix_disabled:
                        config.INSTANCE_URL_SUFFIX = ""
                        flash(
                            "Instance suffix disabled. All new instances will use bare subdomains.",
                            "success",
                        )
                    else:
                        effective = raw_suffix or "hosting"
                        config.INSTANCE_URL_SUFFIX = effective
                        config.RESERVED_SUBDOMAINS.add(effective)
                        flash(
                            "Instance URL suffix set to '{}'. New instances: <name>-{}.domain.com".format(
                                effective, effective
                            ),
                            "success",
                        )

            elif action == "save_tour":
                tour_enabled = 1 if request.form.get("global_tour_enabled") else 0
                update_hosting_settings(global_tour_enabled=tour_enabled)
                flash("Guided tour setting has been saved successfully.", "success")

            elif action == "save_oauth":
                oauth_enabled = 1 if request.form.get("platform_oauth_enabled") else 0
                update_hosting_settings(platform_oauth_enabled=oauth_enabled)
                if oauth_enabled:
                    flash(
                        "Platform OAuth SSO enabled. Each wiki will need to be "
                        "restarted to generate its OAuth credentials.",
                        "success",
                    )
                else:
                    flash("Platform OAuth SSO disabled.", "success")

            elif action == "save_api":
                api_enabled = 1 if request.form.get("api_enabled") else 0
                update_hosting_settings(api_enabled=api_enabled)
                flash(
                    translate("hosting.platform_settings.api.enabled")
                    if api_enabled
                    else translate("hosting.platform_settings.api.disabled"),
                    "success",
                )

            elif action == "save_arabic_mirror":
                arabic_mirror = 1 if request.form.get("arabic_mirror_enabled") else 0
                update_hosting_settings(arabic_mirror_enabled=arabic_mirror)
                flash(
                    "Arabic mirror mode enabled. The platform will display in RTL layout."
                    if arabic_mirror
                    else "Arabic mirror mode disabled.",
                    "success",
                )

            elif action == "save_expired_wiki_permissions":
                update_hosting_settings(
                    allow_owner_delete_expired=1
                    if request.form.get("allow_owner_delete_expired")
                    else 0,
                    allow_owner_download_expired=1
                    if request.form.get("allow_owner_download_expired")
                    else 0,
                    email_flag_block_reentry=1
                    if request.form.get("email_flag_block_reentry")
                    else 0,
                )
                flash("Expired wiki owner permissions saved.", "success")

            elif action == "save_tts_policy":
                tts_enabled = 1 if request.form.get("global_tts_enabled") else 0
                tts_mode = (
                    (request.form.get("global_tts_mode") or "all").strip().lower()
                )
                if tts_mode not in ("all", "whitelist", "blacklist"):
                    tts_mode = "all"
                tts_list = (request.form.get("global_tts_list") or "").strip()
                update_hosting_settings(
                    global_tts_enabled=tts_enabled,
                    global_tts_mode=tts_mode,
                    global_tts_list=tts_list,
                )
                flash("TTS generation policy saved.", "success")

            elif action == "gdrive_save":
                from ..db._settings import update_hosting_settings
                import re as _re

                gdrive_enabled = 1 if request.form.get("gdrive_backup_enabled") else 0
                gdrive_credentials_path = (
                    request.form.get("gdrive_credentials_path") or ""
                ).strip()
                gdrive_folder_id = (request.form.get("gdrive_folder_id") or "").strip()
                gdrive_retention_days = max(
                    1, min(365, int(request.form.get("gdrive_retention_days") or 7))
                )
                gdrive_backup_time = (
                    request.form.get("gdrive_backup_time") or "03:00"
                ).strip()[:5]
                if not _re.match(r"^\d{2}:\d{2}$", gdrive_backup_time):
                    gdrive_backup_time = "03:00"
                else:
                    _h, _m = (
                        int(gdrive_backup_time.split(":")[0]),
                        int(gdrive_backup_time.split(":")[1]),
                    )
                    if not (0 <= _h <= 23 and 0 <= _m <= 59):
                        gdrive_backup_time = "03:00"
                update_hosting_settings(
                    gdrive_backup_enabled=gdrive_enabled,
                    gdrive_credentials_path=gdrive_credentials_path,
                    gdrive_folder_id=gdrive_folder_id,
                    gdrive_retention_days=gdrive_retention_days,
                    gdrive_backup_time=gdrive_backup_time,
                )
                flash("Google Drive backup settings saved.", "success")
                return redirect(url_for("hosting_admin_settings"))

            elif action == "gdrive_test":
                from ..gdrive_backup import test_connection

                ok, msg = test_connection()
                if ok:
                    flash(f"Google Drive connection successful: {msg}", "success")
                else:
                    flash(f"Google Drive connection failed: {msg}", "error")
                return redirect(url_for("hosting_admin_settings"))

            elif action == "gdrive_backup_now":
                from ..gdrive_backup import create_backup, is_configured

                if not is_configured():
                    flash(
                        "Google Drive backup is not configured. Please set credentials and folder ID first.",
                        "error",
                    )
                    return redirect(url_for("hosting_admin_settings"))
                ok, msg = create_backup(reason="manual")
                if ok:
                    flash(f"Backup created successfully: {msg}", "success")
                else:
                    flash(f"Backup failed: {msg}", "error")
                return redirect(url_for("hosting_admin_settings"))

            elif action == "gdrive_upload_credentials":
                import json as _json

                cred_file = request.files.get("gdrive_credentials_file")
                if not cred_file or not cred_file.filename:
                    flash("No credentials file selected.", "error")
                    return redirect(url_for("hosting_admin_settings"))
                try:
                    content = cred_file.read()
                    _json.loads(content)  # validate it's valid JSON
                except Exception:
                    flash("Invalid JSON credentials file.", "error")
                    return redirect(url_for("hosting_admin_settings"))
                from ..gdrive_backup import CREDENTIALS_PATH_DEFAULT
                import os

                os.makedirs(os.path.dirname(CREDENTIALS_PATH_DEFAULT), exist_ok=True)
                with open(CREDENTIALS_PATH_DEFAULT, "wb") as f:
                    f.write(content)
                os.chmod(CREDENTIALS_PATH_DEFAULT, 0o600)
                from ..db._settings import update_hosting_settings

                update_hosting_settings(
                    gdrive_credentials_path=CREDENTIALS_PATH_DEFAULT
                )
                flash("Google Drive credentials uploaded successfully.", "success")
                return redirect(url_for("hosting_admin_settings"))

            else:
                flash("Unknown settings action.", "error")

            return redirect(url_for("hosting_admin_settings"))

        settings = get_hosting_settings()
        return render_template("admin_settings.html", settings=settings)

    @app.route("/admin/spawn-demos", methods=["GET", "POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_spawn_demos():
        """Admin: spawn demo instances from curated presets."""
        from ..demo_presets import DEMO_PRESETS

        if request.method == "GET":
            return render_template(
                "spawn_demos.html",
                presets=DEMO_PRESETS,
                hosting_mode=config.HOSTING_MODE,
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            )

        selected = request.form.getlist("presets")
        if not selected:
            flash("Please select at least one preset.", "error")
            return render_template(
                "spawn_demos.html",
                presets=DEMO_PRESETS,
                hosting_mode=config.HOSTING_MODE,
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
            ), 400

        prefix = (request.form.get("subdomain_prefix") or "demo").strip().lower()
        if not prefix:
            prefix = "demo"

        account_id = session["hosting_account_id"]
        results = provision_demo_instances(
            account_id, selected, subdomain_prefix=prefix
        )

        created = []
        errors = []
        for pid, inst, error in results:
            if error:
                errors.append(f"{pid}: {error}")
            else:
                created.append(inst)

        if created:
            names = ", ".join(i["subdomain"] for i in created)
            flash(
                f"{len(created)} demo instance(s) created successfully: {names}",
                "success",
            )
        if errors:
            for err in errors:
                flash(err, "error")

        return render_template(
            "spawn_demos_results.html",
            created=created,
            errors=errors,
            hosting_mode=config.HOSTING_MODE,
            base_domain=config.BASE_DOMAIN,
        )
