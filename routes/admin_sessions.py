"""Administration: sessions."""

from flask import render_template, request, redirect, url_for, session, flash, abort
import re, uuid, threading
from datetime import datetime, timezone, timedelta
import db
from bananawiki_sdk import emit_hook
from helpers import (
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    get_site_timezone,
    MAX_PASSWORD_LENGTH,
    _DUMMY_HASH,
    _check_login_rate_limit,
    _record_login_attempt,
    _clear_login_attempts,
    generate_form_token,
    check_bot_protection,
    is_bot_protection_enabled,
    t,
)
from helpers._passwords import check_password_hash
from helpers._auth_sessions import establish_user_session
from wiki_logger import log_action, get_logger
from sync import notify_change


def register_admin_sessions_routes(app):
    """Register administration routes for sessions."""

    @app.route("/admin/codes")
    @login_required
    @admin_required
    def admin_codes():
        """Display the active invite codes management page."""
        codes = db.list_invite_codes(active_only=True)
        custom_roles = db.list_custom_roles()
        return render_template(
            "admin/codes.html", codes=codes, custom_roles=custom_roles
        )

    @app.route("/admin/codes/expired")
    @login_required
    @admin_required
    def admin_codes_expired():
        """Display the expired/used invite codes management page."""
        codes = db.list_expired_codes()
        return render_template("admin/codes_expired.html", codes=codes)

    @app.route("/admin/codes/generate", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_generate_code():
        """Generate a new invite code and redirect to the codes list."""
        user = get_current_user()
        max_uses = request.form.get("max_uses", "1")
        expiry_mode = request.form.get("expiry_mode", "48h")
        custom_expiry = request.form.get("custom_expiry", "")
        custom_code = request.form.get("custom_code", "").strip()

        if custom_code and not re.match(r"^[A-Za-z0-9]{1,32}$", custom_code):
            flash(t("flash.custom_invite_code_must_be_between_1_and"), "error")
            return redirect(url_for("admin_codes"))

        try:
            max_uses = int(max_uses)
            if max_uses < 0:
                max_uses = 0
        except ValueError:
            max_uses = 1

        expires_at = None
        now = datetime.now(timezone.utc)

        if expiry_mode == "24h":
            expires_at = (now + timedelta(hours=24)).isoformat()
        elif expiry_mode == "48h":
            expires_at = (now + timedelta(hours=48)).isoformat()
        elif expiry_mode == "72h":
            expires_at = (now + timedelta(hours=72)).isoformat()
        elif expiry_mode == "custom" and custom_expiry:
            from helpers._time import local_datetime_to_utc

            try:
                # Expecting YYYY-MM-DDTHH:MM in site-local timezone
                expires_at = local_datetime_to_utc(custom_expiry)
            except ValueError:
                flash(t("flash.invalid_custom_expiry_date_format"), "error")
                return redirect(url_for("admin_codes"))
        elif expiry_mode == "never":
            expires_at = None

        # Role assignment for the invite code
        assigned_role = request.form.get("assigned_role", "").strip() or None
        if assigned_role not in ("user", "editor", "admin"):
            assigned_role = None
        assigned_custom_role_id = (
            request.form.get("assigned_custom_role_id", "").strip() or None
        )
        if assigned_custom_role_id:
            try:
                assigned_custom_role_id = int(assigned_custom_role_id)
            except (ValueError, TypeError):
                assigned_custom_role_id = None

        try:
            code = db.generate_invite_code(
                user["id"],
                max_uses=max_uses,
                expires_at=expires_at,
                assigned_role=assigned_role,
                assigned_custom_role_id=assigned_custom_role_id,
                custom_code=custom_code or None,
            )
        except db.IntegrityError:
            flash(t("flash.that_invite_code_is_already_in_use_please"), "error")
            return redirect(url_for("admin_codes"))
        log_action("generate_invite_code", request, user=user, code=code)
        notify_change("invite_code_generate", f"Invite code '{code}' generated")
        flash(t("flash.invite_code_generated_code", code=code), "success")
        return redirect(url_for("admin_codes"))

    @app.route("/admin/codes/<int:code_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_delete_code(code_id):
        """Soft-delete (deactivate) an active invite code."""
        user = get_current_user()
        db.delete_invite_code(code_id)
        log_action("delete_invite_code", request, user=user, code_id=code_id)
        notify_change("invite_code_delete", f"Invite code {code_id} deleted")
        flash(t("flash.invite_code_deleted"), "success")
        return redirect(url_for("admin_codes"))

    @app.route("/admin/codes/expired/<int:code_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_hard_delete_code(code_id):
        """Permanently remove an expired or used invite code from the database."""
        user = get_current_user()
        db.hard_delete_invite_code(code_id)
        log_action("hard_delete_invite_code", request, user=user, code_id=code_id)
        notify_change(
            "invite_code_hard_delete", f"Invite code {code_id} permanently removed"
        )
        flash(t("flash.invite_code_permanently_removed"), "success")
        return redirect(url_for("admin_codes_expired"))

    @app.route("/admin", methods=["GET", "POST"])
    @rate_limit(10, 60)
    def admin_login():
        """Dedicated admin sign-in page.

        Replaces the legacy ``/lockdown`` admin-login link.  GET returns a
        minimal login form that only accepts ``admin`` / ``owner``
        credentials; admins who are already authenticated are forwarded to
        the global settings dashboard.  The route stays reachable while the
        wiki is in maintenance mode so administrators can always get back
        in.
        """
        user = get_current_user()
        if user and user["role"] in ("admin", "owner"):
            return redirect(url_for("admin_settings"))

        if request.method == "POST":
            if not _check_login_rate_limit():
                log_action("admin_login_rate_limited", request)
                flash(
                    t("flash.too_many_login_attempts_were_detected_please_wait"),
                    "error",
                )
                return render_template(
                    "auth/admin_login.html",
                    bot_form_token=generate_form_token(),
                ), 429

            if is_bot_protection_enabled():
                blocked, _reason = check_bot_protection(request)
                if blocked:
                    log_action("bot_blocked_admin_login", request)
                    flash(
                        t("flash.submission_rejected_please_reload_the_page_and_try"),
                        "error",
                    )
                    return render_template(
                        "auth/admin_login.html",
                        bot_form_token=generate_form_token(),
                    ), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if len(password) > MAX_PASSWORD_LENGTH:
                _record_login_attempt()
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template(
                    "auth/admin_login.html",
                    bot_form_token=generate_form_token(),
                )

            target = db.get_user_by_username(username)
            if not target:
                # Constant-time path against the dummy hash to avoid
                # leaking username existence via response timing.
                check_password_hash(_DUMMY_HASH(), password)
                _record_login_attempt()
                log_action("admin_login_failed", request, username=username)
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template(
                    "auth/admin_login.html",
                    bot_form_token=generate_form_token(),
                )

            if not check_password_hash(target["password"], password):
                _record_login_attempt()
                log_action("admin_login_failed", request, username=username)
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template(
                    "auth/admin_login.html",
                    bot_form_token=generate_form_token(),
                )

            if target["suspended"] and not db.check_suspension_expired(target["id"]):
                log_action("admin_login_suspended", request, username=username)
                session.clear()
                session.permanent = False
                session["user_id"] = target["id"]
                session["restricted_suspension_session"] = True
                return redirect(url_for("account_suspended"))

            if target["role"] not in ("admin", "owner"):
                # Non-admin credentials are rejected here so the page
                # cannot double as a regular login.
                _record_login_attempt()
                log_action("admin_login_not_admin", request, username=username)
                flash(
                    t(
                        "flash.this_wiki_is_temporarily_restricted_to_administrators_please"
                    ),
                    "error",
                )
                return render_template(
                    "auth/admin_login.html",
                    bot_form_token=generate_form_token(),
                )

            session.clear()
            session.permanent = True
            remember = request.form.get("remember_me") in ("1", "on", "true", "yes")
            if remember:
                from flask import current_app

                lifetime = current_app.config.get("BW_REMEMBER_ME_LIFETIME")
                if lifetime is not None:
                    session["_remember_me"] = True
            session["user_id"] = target["id"]
            session["_logged_in_at"] = datetime.now(timezone.utc).isoformat()
            settings = db.get_site_settings()
            if settings and settings.get("session_limit_enabled"):
                token = uuid.uuid4().hex
                session["session_token"] = token
                db.update_user(target["id"], session_token=token)
            establish_user_session(
                target["id"],
                remember_me=remember,
                revoke_existing=bool(
                    settings and settings.get("session_limit_enabled")
                ),
            )
            _clear_login_attempts()
            db.update_user(
                target["id"],
                last_login_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
            log_action("admin_login_success", request, user=target)
            notify_change(
                "user_login", f"Admin '{target['username']}' logged in via /admin"
            )
            emit_hook("after_login", user=target)
            if target.get("force_password_change"):
                return redirect(url_for("force_change_password"))
            if target.get("onboarding_required"):
                return redirect(url_for("onboarding_setup"))
            if target.get("intro_required"):
                return redirect(url_for("onboarding_intro"))
            return redirect(url_for("admin_settings"))

        return render_template(
            "auth/admin_login.html",
            bot_form_token=generate_form_token(),
        )

    @app.route("/admin/settings")
    @login_required
    @admin_required
    def admin_settings_redirect():
        """Redirect ``/admin/settings`` to the global settings dashboard."""
        return redirect(url_for("admin_settings"), code=301)

    @app.route("/admin/mass-logout", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_mass_logout():
        """Invalidate every user's session token, forcing a re-login on next request."""
        admin = get_current_user()
        count = db.mass_logout_all_users()
        # Re-issue a fresh token for the admin who performed the mass logout so
        # they stay authenticated (all other sessions remain invalidated).
        new_token = uuid.uuid4().hex
        db.update_user(admin["id"], session_token=new_token)
        session["session_token"] = new_token
        establish_user_session(admin["id"], revoke_existing=False)
        log_action("admin_mass_logout", request, user=admin, users_affected=count)
        notify_change(
            "admin_mass_logout",
            f"Mass logout performed by {admin['username']} ({count} sessions invalidated)",
        )
        flash(
            t("flash.all_users_have_been_successfully_logged_out_count", count=count),
            "success",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<string:user_id>/impersonate", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def admin_impersonate_user(user_id):
        """Allow an admin to login as another user."""
        current_user = get_current_user()
        if session.get("impersonator_id"):
            flash(t("flash.stop_impersonating_before_starting_another"), "error")
            return redirect(url_for("home"))

        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)

        if target["id"] == current_user["id"]:
            flash(t("flash.you_cannot_impersonate_yourself"), "error")
            return redirect(url_for("admin_users"))

        if target["role"] == "owner" and not current_user["is_superuser"]:
            flash(t("flash.only_superusers_can_impersonate_owners"), "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and not current_user["is_superuser"]:
            flash(t("flash.only_superusers_can_impersonate_admin_accounts"), "error")
            return redirect(url_for("admin_users"))
        # Refuse to impersonate accounts that the target user could not
        # currently log into themselves: suspended, scheduled-for-deletion,
        # or with a pending temporary-role swap.  Otherwise impersonation
        # would silently bypass those administrative gates.
        if target["suspended"]:
            flash(t("flash.you_cannot_impersonate_a_suspended_user"), "error")
            return redirect(url_for("admin_users"))
        if db.get_user_expiry(target["id"]) is not None:
            flash(t("flash.you_cannot_impersonate_a_user_that_is_scheduled"), "error")
            return redirect(url_for("admin_users"))

        # Store the original admin ID in the session
        session["impersonator_id"] = current_user["id"]
        session["user_id"] = target["id"]
        # Invalidate session token constraint for impersonation
        session.pop("session_token", None)

        log_id = db.log_impersonation_start(current_user["id"], target["id"])
        session["impersonation_log_id"] = log_id

        log_action(
            "admin_impersonate_start",
            request,
            user=current_user,
            target_user=target["username"],
        )
        emit_hook("after_impersonate_start", admin=current_user, target=target)
        flash(
            t("flash.you_are_now_impersonating_username", username=target["username"]),
            "success",
        )
        return redirect(url_for("home"))

    @app.route("/admin/stop-impersonating", methods=["POST"])
    @rate_limit(10, 60)
    def admin_stop_impersonating():
        """Return to the original admin account.

        Note: @admin_required is intentionally omitted: during impersonation
        the session user is the target (not an admin), so admin_required would
        reject the request.  The route checks for ``impersonator_id`` in the
        session instead.
        """
        if "impersonator_id" not in session:
            flash(t("flash.you_are_not_impersonating_anyone"), "error")
            return redirect(url_for("home"))

        log_id = session.pop("impersonation_log_id", None)
        db.log_impersonation_stop(log_id)

        admin_id = session.pop("impersonator_id")
        admin = db.get_user_by_id(admin_id)
        if not admin:
            session.clear()
            flash(t("flash.original_admin_account_not_found"), "error")
            return redirect(url_for("login"))
        if admin["suspended"] and not db.check_suspension_expired(admin["id"]):
            log_action("admin_impersonate_stop", request, user=admin)
            emit_hook("after_impersonate_stop", admin=admin, reason="admin_suspended")
            session.clear()
            flash(t("flash.original_admin_account_is_suspended"), "error")
            return redirect(url_for("login"))
        if admin["role"] not in ("admin", "owner"):
            log_action("admin_impersonate_stop", request, user=admin)
            emit_hook(
                "after_impersonate_stop", admin=admin, reason="admin_role_revoked"
            )
            session.clear()
            flash(
                t("flash.your_admin_session_was_invalidated_while_impersonating_pleas"),
                "info",
            )
            return redirect(url_for("login"))

        # If the admin's session_token in the database is NULL while the
        # session-limit feature is enabled, a peer admin ran a mass-logout
        # (or the account was otherwise invalidated) while this impersonation
        # was active.  Allowing the impersonator to silently resume their
        # admin session in that case would defeat the mass-logout, so force
        # a fresh login instead.  When session-limit is disabled, every
        # admin's session_token is legitimately NULL. That is not an
        # invalidation signal and must not kick the admin out.
        settings = db.get_site_settings()
        session_limit_enabled = bool(settings and settings["session_limit_enabled"])
        if session_limit_enabled and not admin["session_token"]:
            log_action("admin_impersonate_stop", request, user=admin)
            emit_hook(
                "after_impersonate_stop", admin=admin, reason="session_invalidated"
            )
            session.clear()
            flash(
                t("flash.your_admin_session_was_invalidated_while_impersonating_pleas"),
                "info",
            )
            return redirect(url_for("login"))

        session["user_id"] = admin["id"]
        if admin["session_token"]:
            session["session_token"] = admin["session_token"]

        log_action("admin_impersonate_stop", request, user=admin)
        emit_hook("after_impersonate_stop", admin=admin, reason="manual")
        flash(t("flash.you_have_stopped_impersonating_and_returned_to_your"), "success")
        return redirect(url_for("admin_users"))

    _auto_logout_timer_holder = [None]

    def _schedule_auto_logout():
        """Schedule the next daily auto-logout run.

        Reads ``auto_logout_enabled`` and ``auto_logout_hour`` from site settings
        at schedule-time so changes take effect without a server restart.
        """
        if _auto_logout_timer_holder[0] is not None:
            _auto_logout_timer_holder[0].cancel()
            _auto_logout_timer_holder[0] = None

        try:
            settings = db.get_site_settings()
            if not settings or not settings.get("auto_logout_enabled"):
                return

            auto_logout_hour = int(settings.get("auto_logout_hour") or 0)
            site_tz = get_site_timezone()
            now = datetime.now(site_tz)

            target = now.replace(
                hour=auto_logout_hour, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target += timedelta(days=1)

            delay = (target - now).total_seconds()
            _auto_logout_timer_holder[0] = threading.Timer(delay, _run_auto_logout)
            _auto_logout_timer_holder[0].daemon = True
            _auto_logout_timer_holder[0].start()
        except Exception:
            try:
                get_logger().error("Auto-logout scheduling failed", exc_info=True)
            except Exception:
                pass
            # Retry after 10 s so the chain recovers from transient first-boot failures
            # (e.g. DB not yet initialised when the scheduler first fires).
            t = threading.Timer(10, _schedule_auto_logout)
            t.daemon = True
            t.start()
            _auto_logout_timer_holder[0] = t

    def _run_auto_logout():
        """Execute the scheduled auto-logout: invalidate all session tokens."""
        try:
            settings = db.get_site_settings()
            if not settings or not settings.get("auto_logout_enabled"):
                return

            count = db.mass_logout_all_users()
            try:
                get_logger().info(
                    f"Scheduled auto-logout completed: {count} session(s) invalidated"
                )
            except Exception:
                pass
            notify_change(
                "auto_logout",
                f"Scheduled auto-logout completed ({count} session(s) invalidated)",
            )
        except Exception:
            try:
                get_logger().error("Scheduled auto-logout failed", exc_info=True)
            except Exception:
                pass
        finally:
            # Always reschedule for the next day, even if this run failed
            _schedule_auto_logout()

    _schedule_auto_logout()

    return _schedule_auto_logout
