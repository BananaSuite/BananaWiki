"""Administration: settings."""

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    abort,
    g,
)
import os, io, json
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
import db
import config
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    _is_valid_hex_color,
    rate_limit,
    BUILTIN_INTERFACE_LANGUAGES,
    normalize_language_selection,
    t,
    is_hosted_instance,
)
from helpers._tts import remote_gpu_settings_managed_by_host
from wiki_logger import log_action
from sync import notify_change
from .admin_common import (
    MAX_THEME_FILE_BYTES,
    THEME_FILE_EXTENSION,
    _build_theme_payload,
    _normalize_future_settings_datetime,
    _parse_theme_payload,
    _revert_expired_public_access_settings,
    _safe_int,
    _theme_download_name,
    favicon_upload_folder,
)


def register_admin_settings_routes(app, _schedule_auto_logout):
    """Register administration routes for settings."""
    _VALID_FAVICON_TYPES = {
        "yellow",
        "green",
        "blue",
        "red",
        "orange",
        "cyan",
        "purple",
        "lime",
        "custom",
    }

    def _build_protected_pages_for_admin():
        """Return template-ready protected page rows with unlock timing metadata."""
        now = datetime.now(timezone.utc)
        items = []
        for row in db.list_protected_pages():
            item = dict(row)
            unlock_ready_at = None
            unlock_wait_seconds = None
            unlock_ready = False
            if item.get("protection_unlock_requested_at"):
                try:
                    requested_datetime = datetime.fromisoformat(
                        item["protection_unlock_requested_at"]
                    )
                    if requested_datetime.tzinfo is None:
                        requested_datetime = requested_datetime.replace(
                            tzinfo=timezone.utc
                        )
                    ready_datetime = requested_datetime + timedelta(
                        seconds=db.PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS
                    )
                    unlock_ready_at = ready_datetime.isoformat()
                    unlock_wait_seconds = max(
                        0, int((ready_datetime - now).total_seconds())
                    )
                    unlock_ready = unlock_wait_seconds == 0
                except ValueError:
                    pass
            item["unlock_ready_at"] = unlock_ready_at
            item["unlock_wait_seconds"] = unlock_wait_seconds
            item["unlock_ready"] = unlock_ready
            items.append(item)
        return items

    @app.route("/global-settings", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_settings():
        """Global/site settings page: site name, theme, security, and feature toggles."""
        if request.method == "POST":
            site_name = request.form.get("site_name", "").strip() or "BananaWiki"
            if len(site_name) > 100:
                flash(t("flash.site_name_cannot_exceed_100_characters"), "error")
                return redirect(url_for("admin_settings"))
            default_theme_mode = (
                request.form.get("default_theme_mode", "dark").strip().lower()
            )
            if default_theme_mode not in {"dark", "light"}:
                default_theme_mode = "dark"
            color_fields = {
                "primary_color": request.form.get("primary_color", "#7c8dc6"),
                "secondary_color": request.form.get("secondary_color", "#151520"),
                "accent_color": request.form.get("accent_color", "#6e8aca"),
                "text_color": request.form.get("text_color", "#b8bcc8"),
                "sidebar_color": request.form.get("sidebar_color", "#111118"),
                "bg_color": request.form.get("bg_color", "#0d0d14"),
                "light_primary_color": request.form.get(
                    "light_primary_color", "#4b63b6"
                ),
                "light_secondary_color": request.form.get(
                    "light_secondary_color", "#ffffff"
                ),
                "light_accent_color": request.form.get("light_accent_color", "#3553c7"),
                "light_text_color": request.form.get("light_text_color", "#202534"),
                "light_sidebar_color": request.form.get(
                    "light_sidebar_color", "#e9edf5"
                ),
                "light_bg_color": request.form.get("light_bg_color", "#f6f7fb"),
            }
            for name, val in color_fields.items():
                if not _is_valid_hex_color(val):
                    flash(t("flash.invalid_color_value_for_name", name=name), "error")
                    return redirect(url_for("admin_settings"))
            tz_name = request.form.get("timezone", "UTC").strip() or "UTC"
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, KeyError):
                flash(t("flash.invalid_time_zone_selected"), "error")
                return redirect(url_for("admin_settings"))
            current_settings = db.get_site_settings() or {}
            interface_language_fallback = (
                request.form.get("interface_language_fallback", "en").strip().lower()
            )
            if interface_language_fallback not in BUILTIN_INTERFACE_LANGUAGES:
                interface_language_fallback = "en"
            interface_language = normalize_language_selection(
                request.form.get("interface_language", interface_language_fallback),
                current_settings,
                default=interface_language_fallback,
            )
            intro_role_switching_roles = [
                role
                for role in ("user", "editor", "admin")
                if role in set(request.form.getlist("intro_role_switching_roles"))
            ]
            if not intro_role_switching_roles:
                intro_role_switching_roles = ["user", "editor", "admin"]

            # Determine which plugins are enabled to gate their settings.
            # g.enabled_plugins is populated by before_request_hook and uses the
            # same fallback logic as the context processor, so it correctly handles
            # databases that have not yet seeded the plugins table.
            _ep = getattr(g, "enabled_plugins", {})
            page_governance_plugin_enabled = bool(_ep.get("page_governance"))
            # Legacy alias kept for the surrounding logic block.
            page_reservations_plugin_enabled = page_governance_plugin_enabled
            chat_plugin_enabled = bool(_ep.get("chat"))
            user_profiles_plugin_enabled = bool(_ep.get("user_profiles"))
            kanban_plugin_enabled = bool(_ep.get("kanban"))
            canvas_plugin_enabled = bool(_ep.get("canvas"))
            assessments_plugin_enabled = bool(_ep.get("assessments"))
            custom_pages_plugin_enabled = bool(_ep.get("custom_pages"))
            attachments_plugin_enabled = bool(_ep.get("attachments"))
            tts_plugin_enabled = bool(_ep.get("tts"))

            page_reservation_duration_hours = config.PAGE_RESERVATION_DURATION_HOURS
            page_reservation_cooldown_hours = config.PAGE_RESERVATION_COOLDOWN_HOURS
            default_reserved_pages_quota = 5
            reservation_quota_auto_approve_max = 0
            if page_reservations_plugin_enabled:
                try:
                    page_reservation_duration_hours = max(
                        1,
                        min(
                            8760,
                            int(
                                request.form.get(
                                    "page_reservation_duration_hours",
                                    config.PAGE_RESERVATION_DURATION_HOURS,
                                )
                                or config.PAGE_RESERVATION_DURATION_HOURS
                            ),
                        ),
                    )
                    page_reservation_cooldown_hours = max(
                        0,
                        min(
                            8760,
                            int(
                                request.form.get(
                                    "page_reservation_cooldown_hours",
                                    config.PAGE_RESERVATION_COOLDOWN_HOURS,
                                )
                                or config.PAGE_RESERVATION_COOLDOWN_HOURS
                            ),
                        ),
                    )
                    default_reserved_pages_quota = max(
                        1,
                        min(
                            1000,
                            int(
                                request.form.get(
                                    "default_reserved_pages_quota",
                                    5,
                                )
                                or 5
                            ),
                        ),
                    )
                except ValueError:
                    flash(
                        t("flash.reservation_settings_must_use_wholenumber_values"),
                        "error",
                    )
                    return redirect(url_for("admin_settings"))
                reservation_quota_auto_approve_max = _safe_int(
                    request.form.get("reservation_quota_auto_approve_max"),
                    0,
                    lo=0,
                    hi=1000000,
                )

            # Favicon settings (only favicon_enabled is submitted via form;
            # favicon_type/favicon_custom are managed by AJAX endpoints).
            favicon_enabled = 1 if request.form.get("favicon_enabled") else 0
            current_settings = db.get_site_settings()
            favicon_type = current_settings.get("favicon_type", "yellow")
            valid_presets = _VALID_FAVICON_TYPES - {"custom"}
            if favicon_type not in valid_presets and favicon_type != "custom":
                favicon_type = "yellow"
            favicon_custom = current_settings.get("favicon_custom", "")
            if favicon_type == "custom" and favicon_custom:
                custom_path = os.path.join(favicon_upload_folder(), favicon_custom)
                if not os.path.isfile(custom_path) or not favicon_custom.startswith(
                    "custom_"
                ):
                    favicon_type = "yellow"
                    favicon_custom = ""

            public_mode_enabled = 1 if request.form.get("public_mode") else 0
            if public_mode_enabled and getattr(config, "FORBID_PUBLIC_MODE", False):
                public_mode_enabled = 0
                flash(
                    "Public access is disabled for this managed trial by the hosting platform.",
                    "error",
                )
            page_builder_enabled = 1 if request.form.get("page_builder_enabled") else 0
            if page_builder_enabled and getattr(config, "FORBID_PAGE_BUILDER", False):
                page_builder_enabled = 0
                flash(
                    "The visual page builder has not been approved by the hosting platform.",
                    "error",
                )
            page_builder_access = (
                request.form.get("page_builder_access", "admin").strip().lower()
            )
            if page_builder_access not in {"admin", "editor", "user"}:
                page_builder_access = "admin"
            open_signup_enabled = 1 if request.form.get("open_signup") else 0
            approval_required = 1 if request.form.get("approval_required") else 0
            try:
                approval_denied_timeout_hours = max(
                    1,
                    min(
                        8760,
                        int(
                            request.form.get("approval_denied_timeout_hours", 24) or 24
                        ),
                    ),
                )
            except (TypeError, ValueError):
                approval_denied_timeout_hours = 24
            try:
                approval_pending_timeout_hours = max(
                    0,
                    min(
                        8760,
                        int(request.form.get("approval_pending_timeout_hours", 0) or 0),
                    ),
                )
            except (TypeError, ValueError):
                approval_pending_timeout_hours = 0
            try:
                public_mode_until = (
                    _normalize_future_settings_datetime(
                        request.form.get("public_mode_until")
                    )
                    if public_mode_enabled
                    else ""
                )
                open_signup_until = (
                    _normalize_future_settings_datetime(
                        request.form.get("open_signup_until")
                    )
                    if open_signup_enabled
                    else ""
                )
            except ValueError as exc:
                if str(exc) == "past":
                    flash(t("flash.expiry_date_must_be_in_the_future"), "error")
                else:
                    flash(t("flash.invalid_expiration_date_format"), "error")
                return redirect(url_for("admin_settings"))

            settings_update_kwargs = dict(
                site_name=site_name,
                interface_language=interface_language,
                interface_language_fallback=interface_language_fallback,
                timezone=tz_name,
                favicon_enabled=favicon_enabled,
                favicon_type=favicon_type,
                favicon_custom=favicon_custom,
                favicon_order=current_settings.get("favicon_order", "[]"),
                default_theme_mode=default_theme_mode,
                maintenance_mode=1 if request.form.get("maintenance_mode") else 0,
                maintenance_message=request.form.get("maintenance_message", "").strip()[
                    :1000
                ],
                session_limit_enabled=1
                if request.form.get("session_limit_enabled")
                else 0,
                suspended_account_deletion_enabled=1
                if request.form.get("suspended_account_deletion_enabled")
                else 0,
                auto_logout_enabled=1 if request.form.get("auto_logout_enabled") else 0,
                auto_logout_hour=_safe_int(
                    request.form.get("auto_logout_hour"), 0, lo=0, hi=23
                ),
                pdf_export_enabled=1 if request.form.get("pdf_export_enabled") else 0,
                markdown_export_enabled=1
                if request.form.get("markdown_export_enabled")
                else 0,
                contributor_leaderboard_enabled=1
                if request.form.get("contributor_leaderboard_enabled")
                else 0,
                sidebar_apps_order=request.form.get("sidebar_apps_order", "").strip()[
                    :500
                ],
                new_user_intro_enabled=1
                if request.form.get("new_user_intro_enabled")
                else 0,
                onboarding_replay_disabled=1
                if request.form.get("onboarding_replay_disabled")
                else 0,
                intro_role_switching_enabled=1
                if request.form.get("intro_role_switching_enabled")
                else 0,
                intro_role_switching_roles=",".join(intro_role_switching_roles),
                # Public access mode
                public_mode=public_mode_enabled,
                public_mode_until=public_mode_until,
                public_mode_message=request.form.get("public_mode_message", "").strip()[
                    :1000
                ],
                public_mode_show_message=1
                if request.form.get("public_mode_show_message")
                else 0,
                page_builder_enabled=page_builder_enabled,
                page_builder_access=page_builder_access,
                # Open sign-up (no invite code)
                open_signup=open_signup_enabled,
                open_signup_until=open_signup_until,
                approval_required=approval_required,
                approval_denied_timeout_hours=approval_denied_timeout_hours,
                approval_pending_timeout_hours=approval_pending_timeout_hours,
                # Bot protection
                bot_protection_enabled=1
                if request.form.get("bot_protection_enabled")
                else 0,
                **color_fields,
            )
            if attachments_plugin_enabled:
                upload_mode = (
                    request.form.get("upload_mode", "allow_all").strip().lower()
                )
                if upload_mode not in ("whitelist", "blacklist", "allow_all"):
                    upload_mode = "allow_all"
                upload_whitelist = request.form.get("upload_whitelist", "").strip()[
                    :2000
                ]
                upload_blacklist = request.form.get("upload_blacklist", "").strip()[
                    :2000
                ]
                upload_max_size_mb = _safe_int(
                    request.form.get("upload_max_size_mb"), 100, lo=1, hi=2048
                )
                settings_update_kwargs.update(
                    upload_mode=upload_mode,
                    upload_whitelist=upload_whitelist,
                    upload_blacklist=upload_blacklist,
                    upload_max_size_mb=upload_max_size_mb,
                )
            if page_governance_plugin_enabled:
                settings_update_kwargs.update(
                    page_protection_enabled=1
                    if request.form.get("page_protection_enabled")
                    else 0,
                    page_reservations_enabled=1
                    if request.form.get("page_reservations_enabled")
                    else 0,
                    page_reservation_duration_hours=page_reservation_duration_hours,
                    page_reservation_cooldown_hours=page_reservation_cooldown_hours,
                    default_reserved_pages_quota=default_reserved_pages_quota,
                    reservation_quota_auto_approve_max=reservation_quota_auto_approve_max,
                )
            if tts_plugin_enabled:
                tts_generation_mode = (
                    request.form.get("tts_generation_mode") or "on_demand"
                ).strip()
                settings_update_kwargs.update(
                    tts_page_panel_enabled=1
                    if request.form.get("tts_page_panel_enabled")
                    else 0,
                    tts_public_access_enabled=1
                    if request.form.get("tts_public_access_enabled")
                    else 0,
                    tts_auto_generate_enabled=1
                    if tts_generation_mode == "auto_save"
                    else 0,
                    tts_performance_mode=(
                        request.form.get("tts_performance_mode") or "auto"
                    )
                    .strip()
                    .lower()
                    if request.form.get("tts_performance_mode")
                    in ("auto", "balanced", "fast")
                    else "auto",
                )
                # GPU settings: only save where the wiki reads them. On hosted
                # instances the GPU server comes from the server environment.
                if not remote_gpu_settings_managed_by_host():
                    settings_update_kwargs.update(
                        tts_gpu_enabled=1 if request.form.get("tts_gpu_enabled") else 0,
                        tts_gpu_url=(request.form.get("tts_gpu_url") or "")
                        .strip()
                        .rstrip("/"),
                        tts_gpu_timeout=_safe_int(
                            request.form.get("tts_gpu_timeout"), 120, lo=10, hi=600
                        ),
                    )
                    gpu_token = (request.form.get("tts_gpu_auth_token") or "").strip()
                    if gpu_token and not gpu_token.endswith("..."):
                        settings_update_kwargs["tts_gpu_auth_token"] = gpu_token
                    elif not gpu_token:
                        settings_update_kwargs["tts_gpu_auth_token"] = (
                            current_settings.get("tts_gpu_auth_token", "")
                        )
            # Draft expiration is independent of the Page Governance plugin.
            settings_update_kwargs.update(
                draft_expiration_hours=_safe_int(
                    request.form.get("draft_expiration_hours"), 0, lo=0, hi=8760
                ),
            )
            # Contribution approval is gated behind the Page Governance plugin.
            if page_governance_plugin_enabled:
                settings_update_kwargs.update(
                    contribution_approval_enabled=1
                    if request.form.get("contribution_approval_enabled")
                    else 0,
                    default_contribution_quota=_safe_int(
                        request.form.get("default_contribution_quota"), 5, lo=1, hi=1000
                    ),
                    contribution_quota_auto_approve_max=_safe_int(
                        request.form.get("contribution_quota_auto_approve_max"),
                        0,
                        lo=0,
                        hi=1000000,
                    ),
                    quota_request_cooldown_hours=_safe_int(
                        request.form.get("quota_request_cooldown_hours"),
                        0,
                        lo=0,
                        hi=8760,
                    ),
                )
            if chat_plugin_enabled:
                settings_update_kwargs.update(
                    # Global chat settings
                    chat_max_message_length=_safe_int(
                        request.form.get("chat_max_message_length"),
                        5000,
                        lo=100,
                        hi=50000,
                    ),
                    chat_attachments_enabled=1
                    if request.form.get("chat_attachments_enabled")
                    else 0,
                    chat_max_attachment_size_mb=_safe_int(
                        request.form.get("chat_max_attachment_size_mb"), 5, lo=1, hi=100
                    ),
                    chat_attachments_per_day_limit=_safe_int(
                        request.form.get("chat_attachments_per_day_limit"),
                        10,
                        lo=1,
                        hi=1000,
                    ),
                    # DM-specific settings
                    chat_dm_enabled=1 if request.form.get("chat_dm_enabled") else 0,
                    chat_allow_dm_creation=1
                    if request.form.get("chat_allow_dm_creation")
                    else 0,
                    chat_dm_message_retention_days=_safe_int(
                        request.form.get("chat_dm_message_retention_days"),
                        30,
                        lo=1,
                        hi=3650,
                    ),
                    chat_dm_attachment_retention_days=_safe_int(
                        request.form.get("chat_dm_attachment_retention_days"),
                        7,
                        lo=1,
                        hi=3650,
                    ),
                )
                # Cleanup scheduler settings (formerly gated on the separate
                # chat_cleanup plugin; now part of the chat plugin).
                settings_update_kwargs.update(
                    chat_dm_auto_clear_messages=1
                    if request.form.get("chat_dm_auto_clear_messages")
                    else 0,
                    chat_dm_auto_clear_attachments=1
                    if request.form.get("chat_dm_auto_clear_attachments")
                    else 0,
                    chat_cleanup_enabled=1
                    if request.form.get("chat_cleanup_enabled")
                    else 0,
                    chat_cleanup_frequency_days=_safe_int(
                        request.form.get("chat_cleanup_frequency_days"), 7, lo=1, hi=365
                    ),
                    chat_cleanup_hour=_safe_int(
                        request.form.get("chat_cleanup_hour"), 3, lo=0, hi=23
                    ),
                    chat_cleanup_split_configured=1,
                )
            # Groups settings (groups is merged into the chat plugin).
            if chat_plugin_enabled:
                settings_update_kwargs.update(
                    # Group-specific settings
                    chat_group_enabled=1
                    if request.form.get("chat_group_enabled")
                    else 0,
                    chat_allow_group_creation=1
                    if request.form.get("chat_allow_group_creation")
                    else 0,
                    chat_group_message_retention_days=_safe_int(
                        request.form.get("chat_group_message_retention_days"),
                        30,
                        lo=1,
                        hi=3650,
                    ),
                    chat_group_attachment_retention_days=_safe_int(
                        request.form.get("chat_group_attachment_retention_days"),
                        7,
                        lo=1,
                        hi=3650,
                    ),
                )
                # Group auto-deletion toggles (cleanup scheduler is part of chat).
                settings_update_kwargs.update(
                    chat_group_auto_clear_messages=1
                    if request.form.get("chat_group_auto_clear_messages")
                    else 0,
                    chat_group_auto_clear_attachments=1
                    if request.form.get("chat_group_auto_clear_attachments")
                    else 0,
                )
            if user_profiles_plugin_enabled:
                settings_update_kwargs.update(
                    profile_contribution_chart_enabled=1
                    if request.form.get("profile_contribution_chart_enabled")
                    else 0,
                )
            # Profile group badges toggle (requires both chat and user_profiles)
            if user_profiles_plugin_enabled and chat_plugin_enabled:
                settings_update_kwargs.update(
                    profile_group_badges_enabled=1
                    if request.form.get("profile_group_badges_enabled")
                    else 0,
                )
            if kanban_plugin_enabled:
                kanban_access = request.form.get("kanban_access", "admin")
                if kanban_access not in ("admin", "editor", "all"):
                    kanban_access = "admin"
                kanban_write_access = request.form.get("kanban_write_access", "admin")
                if kanban_write_access not in ("admin", "editor", "all"):
                    kanban_write_access = "admin"
                settings_update_kwargs.update(
                    kanban_access=kanban_access,
                    kanban_write_access=kanban_write_access,
                    kanban_public_access_enabled=1
                    if request.form.get("kanban_public_access_enabled")
                    else 0,
                    kanban_open_access=1
                    if request.form.get("kanban_open_access")
                    else 0,
                )
                # Revoke role-based board shares that are no longer valid
                db.kanban_revoke_role_shares_for_restricted_roles(kanban_access)
                # Clean up assignees across all boards whose users lost access
                db.kanban_remove_all_invalid_assignees(kanban_access)
            if canvas_plugin_enabled:
                canvas_access = request.form.get("canvas_access", "admin")
                if canvas_access not in ("admin", "editor", "all"):
                    canvas_access = "admin"
                canvas_write_access = request.form.get("canvas_write_access", "admin")
                if canvas_write_access not in ("admin", "editor", "all"):
                    canvas_write_access = "admin"
                settings_update_kwargs.update(
                    canvas_access=canvas_access,
                    canvas_write_access=canvas_write_access,
                    canvas_public_access_enabled=1
                    if request.form.get("canvas_public_access_enabled")
                    else 0,
                    canvas_open_access=1
                    if request.form.get("canvas_open_access")
                    else 0,
                )
                # Revoke role-based canvas permissions that are no longer valid
                db.canvas_revoke_role_shares_for_restricted_roles(canvas_access)
            if assessments_plugin_enabled:
                settings_update_kwargs.update(
                    assessment_points_badge_enabled=1
                    if request.form.get("assessment_points_badge_enabled")
                    else 0,
                )
            if custom_pages_plugin_enabled:
                try:
                    custom_pages_max_video_size_mb = max(
                        1,
                        min(
                            2048,
                            int(
                                request.form.get("custom_pages_max_video_size_mb", 100)
                                or 100
                            ),
                        ),
                    )
                except ValueError:
                    flash(t("flash.custom_pages_video_size_limit_must_be_a"), "error")
                    return redirect(url_for("admin_settings"))
                settings_update_kwargs.update(
                    custom_pages_max_video_size_mb=custom_pages_max_video_size_mb,
                )
            db.update_site_settings(**settings_update_kwargs)
            user = get_current_user()
            log_action("update_settings", request, user=user, site_name=site_name)
            notify_change(
                "settings_update", f"Site settings updated (name='{site_name}')"
            )
            flash(t("flash.settings_updated"), "success")
            # Re-schedule the auto-logout timer to pick up any changes
            _schedule_auto_logout()
            return redirect(url_for("admin_settings"))

        settings = _revert_expired_public_access_settings(db.get_site_settings())
        # Pass enabled_plugins explicitly so the template has direct access to the
        # plugin state without relying solely on the global context processor.
        # g.enabled_plugins is set by before_request_hook for every non-static request
        # using the same fallback-aware logic as inject_globals().
        enabled_plugins = getattr(g, "enabled_plugins", {})
        # Parse favicon_order JSON, build favicon list for the grid view
        try:
            favicon_order_list = json.loads(settings.get("favicon_order", "[]"))
        except (json.JSONDecodeError, TypeError):
            favicon_order_list = []
        favicon_order_list = [
            f
            for f in favicon_order_list
            if isinstance(f, str) and f.startswith("custom_")
        ]
        presets = sorted(_VALID_FAVICON_TYPES - {"custom"})
        # Build ordered list of custom favicons known to the DB. Do not scan
        # arbitrary custom_* files from disk; test artifacts or stale files in
        # the static folder should not appear as user-selectable icons.
        existing_custom = []
        seen = set()
        for fname in favicon_order_list:
            fpath = os.path.join(favicon_upload_folder(), fname)
            if os.path.isfile(fpath) and fname not in seen:
                existing_custom.append(fname)
                seen.add(fname)
        current_custom = settings.get("favicon_custom", "")
        if (
            settings.get("favicon_type") == "custom"
            and isinstance(current_custom, str)
            and current_custom.startswith("custom_")
            and current_custom not in seen
        ):
            fpath = os.path.join(favicon_upload_folder(), current_custom)
            if os.path.isfile(fpath):
                existing_custom.append(current_custom)
        # Fetch Lab Camera config from the plugin's own table (if the plugin
        # is enabled) so the Integrations tab can display the current values.
        return render_template(
            "admin/settings.html",
            settings=settings,
            timezones=sorted(available_timezones()),
            favicon_types=presets,
            favicon_order_list=existing_custom,
            enabled_plugins=enabled_plugins,
            server_restart_cooldown_seconds=config.SERVER_RESTART_COOLDOWN_SECONDS,
            protected_pages=_build_protected_pages_for_admin(),
            is_hosted=is_hosted_instance(),
            tts_gpu_managed_by_host=remote_gpu_settings_managed_by_host(),
        )

    @app.route("/global-settings/theme/export")
    @login_required
    @admin_required
    def admin_theme_export():
        """Download the current site theme as a portable .bwtheme file."""
        settings = db.get_site_settings() or {}
        payload = json.dumps(
            _build_theme_payload(settings),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        user = get_current_user()
        log_action("theme_export", request, user=user)
        return send_file(
            io.BytesIO(payload.encode("utf-8")),
            mimetype="application/json",
            as_attachment=True,
            download_name=_theme_download_name(settings),
        )

    @app.route("/global-settings/theme/import", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_theme_import():
        """Restore site theme settings from a .bwtheme file."""
        upload = request.files.get("theme_file")
        if not upload or not upload.filename:
            flash(t("flash.theme_file_required"), "error")
            return redirect(url_for("admin_settings"))

        filename = secure_filename(upload.filename)
        if not filename.lower().endswith(THEME_FILE_EXTENSION):
            flash(t("flash.theme_file_extension_required"), "error")
            return redirect(url_for("admin_settings"))

        raw = upload.read(MAX_THEME_FILE_BYTES + 1)
        if len(raw) > MAX_THEME_FILE_BYTES:
            flash(t("flash.theme_file_too_large"), "error")
            return redirect(url_for("admin_settings"))

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            flash(t("flash.theme_file_invalid"), "error")
            return redirect(url_for("admin_settings"))

        updates, error = _parse_theme_payload(payload)
        if error:
            flash(error, "error")
            return redirect(url_for("admin_settings"))

        db.update_site_settings(**updates)
        user = get_current_user()
        log_action("theme_import", request, user=user, filename=filename)
        notify_change("settings_update", "Site theme imported from .bwtheme file")
        flash(t("flash.theme_imported"), "success")
        return redirect(url_for("admin_settings"))

    @app.route("/admin/settings/restart-server", methods=["POST"])
    @app.route("/global-settings/restart-server", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_restart_server():
        """Trigger a full restart of this wiki's Gunicorn (and best-effort
        nginx) stack.

        The restart is **isolated to the calling wiki**:

        * On the main wiki the action SIGTERMs the local Gunicorn master
          and lets ``bananawiki.service`` (``Restart=always``) bring it
          back: sibling wikis on the same host are never touched.
        * On a hosted ``*-hosting.example.com`` instance the action does
          a Gunicorn graceful re-exec (``SIGUSR2`` → drain → exit) on
          the per-instance master discovered via ``os.getppid()``.

        A code-only cooldown (``config.SERVER_RESTART_COOLDOWN_SECONDS``,
        default 60s) is enforced via
        :func:`db.check_and_claim_server_restart`.  The constant cannot
        be overridden from the UI, the database, or environment
        variables: the only way to change it is to edit the source.
        """
        from helpers import (
            get_restart_cooldown_remaining,
            trigger_server_restart,
        )

        user = get_current_user()

        # Pre-check the cooldown to give the admin a useful "wait N seconds"
        # message before we touch the database.  ``check_and_claim`` below
        # is still the source of truth (atomic against concurrent workers).
        remaining = get_restart_cooldown_remaining()
        if remaining > 0:
            log_action(
                "server_restart_cooldown_blocked",
                request,
                user=user,
                remaining_seconds=remaining,
            )
            flash(
                t("flash.server_restart_cooldown").format(seconds=remaining),
                "error",
            )
            return redirect(url_for("admin_settings"))

        if not db.check_and_claim_server_restart(
            config.SERVER_RESTART_COOLDOWN_SECONDS
        ):
            # Lost the race with another worker that just claimed the slot.
            log_action(
                "server_restart_cooldown_blocked",
                request,
                user=user,
                race=True,
            )
            flash(
                t("flash.server_restart_cooldown").format(
                    seconds=config.SERVER_RESTART_COOLDOWN_SECONDS,
                ),
                "error",
            )
            return redirect(url_for("admin_settings"))

        ok, reason = trigger_server_restart()
        if not ok:
            log_action(
                "server_restart_failed",
                request,
                user=user,
                reason=reason,
            )
            flash(
                t("flash.server_restart_failed").format(reason=reason),
                "error",
            )
            return redirect(url_for("admin_settings"))

        log_action("server_restart_triggered", request, user=user)
        notify_change(
            "server_restart",
            f"Server restart triggered by {user['username'] if user else 'unknown admin'}",
        )
        flash(t("flash.server_restart_triggered"), "success")
        return redirect(url_for("admin_settings"))

    @app.route(
        "/admin/settings/page-protection/<int:page_id>/request-unlock", methods=["POST"]
    )
    @app.route(
        "/global-settings/page-protection/<int:page_id>/request-unlock",
        methods=["POST"],
    )
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_request_page_protection_unlock(page_id):
        """Start a 72-hour timer to allow admin unlock for a protected page."""
        page = db.get_page(page_id)
        if not page:
            abort(404)
        if not page["protected_by"]:
            flash(t("flash.this_page_is_not_currently_protected"), "info")
            return redirect(url_for("admin_settings"))
        user = get_current_user()
        if page["protected_by"] == user["id"]:
            db.clear_page_protection(page_id)
            log_action(
                "admin_unprotect_page_owner", request, user=user, page=page["slug"]
            )
            flash(t("flash.page_protection_has_been_successfully_removed"), "success")
            return redirect(url_for("admin_settings"))
        db.request_page_protection_unlock(page_id, user["id"])
        log_action(
            "admin_request_page_unprotect", request, user=user, page=page["slug"]
        )
        flash(t("flash.admin_unlock_request_has_been_scheduled_this_page"), "success")
        return redirect(url_for("admin_settings"))

    @app.route(
        "/admin/settings/page-protection/<int:page_id>/force-unlock", methods=["POST"]
    )
    @app.route(
        "/global-settings/page-protection/<int:page_id>/force-unlock", methods=["POST"]
    )
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def admin_force_page_protection_unlock(page_id):
        """Force-unlock protection after the 72-hour admin cooldown."""
        page = db.get_page(page_id)
        if not page:
            abort(404)
        if not page["protected_by"]:
            flash(t("flash.this_page_is_not_currently_protected"), "info")
            return redirect(url_for("admin_settings"))
        user = get_current_user()
        if page["protected_by"] == user["id"]:
            db.clear_page_protection(page_id)
            log_action(
                "admin_unprotect_page_owner", request, user=user, page=page["slug"]
            )
            flash(t("flash.page_protection_has_been_successfully_removed"), "success")
            return redirect(url_for("admin_settings"))
        requested_at = page["protection_unlock_requested_at"]
        if not requested_at:
            flash(
                t("flash.an_unlock_request_is_required_before_forceunlock_is"), "error"
            )
            return redirect(url_for("admin_settings"))
        try:
            ready_datetime = datetime.fromisoformat(requested_at) + timedelta(
                seconds=db.PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS
            )
            if ready_datetime.tzinfo is None:
                ready_datetime = ready_datetime.replace(tzinfo=timezone.utc)
        except ValueError:
            flash(
                t("flash.this_unlock_request_is_invalid_please_request_unlock"), "error"
            )
            return redirect(url_for("admin_settings"))
        if datetime.now(timezone.utc) < ready_datetime:
            flash(t("flash.this_page_cannot_be_forceunlocked_yet_please_wait"), "error")
            return redirect(url_for("admin_settings"))
        db.clear_page_protection(page_id)
        log_action("admin_force_unprotect_page", request, user=user, page=page["slug"])
        notify_change(
            "page_unprotect", f"Page '{page['slug']}' protection removed by admin"
        )
        flash(t("flash.page_protection_has_been_successfully_removed"), "success")
        return redirect(url_for("admin_settings"))
