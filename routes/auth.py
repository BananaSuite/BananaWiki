"""
BananaWiki: Authentication routes (login, signup, logout, setup, maintenance).
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

from flask import (
    render_template, request, redirect, url_for, session, flash,
)

import db
from bootstrap import authorize_setup, setup_authorized
from bananawiki_sdk import emit_hook
from helpers import (
    _DUMMY_HASH, _check_login_rate_limit, _record_login_attempt,
    _clear_login_attempts, get_current_user,
    _is_valid_username, rate_limit,
    is_open_signup_active, is_approval_required_active,
    BUILTIN_INTERFACE_LANGUAGES,
    get_enabled_interface_languages,
    normalize_language_selection,
    normalize_docs_language,
    get_safe_next_url,
    MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH,
    generate_form_token, check_bot_protection, is_bot_protection_enabled,
    t,
)
from helpers._passwords import generate_password_hash, check_password_hash
from helpers._auth_sessions import establish_user_session, SESSION_TOKEN_KEY
from helpers._time import format_datetime
from wiki_logger import log_action
from sync import notify_change


def register_auth_routes(app):
    """Register authentication-related routes on the Flask app."""
    app.jinja_env.globals["setup_authorized"] = setup_authorized


    @app.route("/setup", methods=["GET", "POST"])
    @rate_limit(5, 60)
    def setup():
        """Initial admin-account setup wizard (only accessible before setup is complete)."""
        settings = db.get_site_settings()
        if settings and settings["setup_done"]:
            return redirect(url_for("home"))

        if request.method == "GET" and request.args.get("setup_token") and authorize_setup():
            return redirect(url_for("setup"))

        def setup_language(default="en"):
            """Resolve the effective interface language for the setup form."""
            env_default = normalize_language_selection(
                os.environ.get("BW_DEFAULT_INTERFACE_LANGUAGE", default),
                settings or {},
                default=default,
            )
            return normalize_language_selection(
                request.form.get("interface_language")
                or session.get("interface_language")
                or env_default,
                settings or {},
                default=env_default,
            )

        # Compute effective theme for setup page
        site_tm = settings.get("default_theme_mode") if settings and settings.get("default_theme_mode") in ("dark", "light") else "dark"
        setup_effective_theme = "light" if site_tm == "light" else "dark"

        def render_setup_form(status=200):
            """Render the initial setup page with the current form state."""
            selected_language = setup_language()
            selected_docs_language = normalize_docs_language(
                request.form.get("docs_language") or selected_language,
                settings or {},
                default=selected_language,
            )
            return render_template(
                "auth/setup.html",
                setup_interface_languages=get_enabled_interface_languages(settings or {}),
                setup_docs_languages=BUILTIN_INTERFACE_LANGUAGES,
                selected_setup_language=selected_language,
                selected_docs_language=selected_docs_language,
                bot_form_token=generate_form_token(),
                effective_theme_mode=setup_effective_theme,
                theme_primary=(settings or {}).get("light_primary_color") if setup_effective_theme == "light" else (settings or {}).get("primary_color", "#8fa0d4"),
                theme_secondary=(settings or {}).get("light_secondary_color") if setup_effective_theme == "light" else (settings or {}).get("secondary_color", "#1e1e2c"),
                theme_accent=(settings or {}).get("light_accent_color") if setup_effective_theme == "light" else (settings or {}).get("accent_color", "#7e9ada"),
                theme_text=(settings or {}).get("light_text_color") if setup_effective_theme == "light" else (settings or {}).get("text_color", "#c8ccd8"),
                theme_sidebar=(settings or {}).get("light_sidebar_color") if setup_effective_theme == "light" else (settings or {}).get("sidebar_color", "#1a1a24"),
                theme_bg=(settings or {}).get("light_bg_color") if setup_effective_theme == "light" else (settings or {}).get("bg_color", "#16161f"),
            ), status

        if request.method == "POST":
            if not authorize_setup():
                flash("Enter the installation token to create the first administrator.", "error")
                return render_setup_form(403)
            # Bot protection check (honeypot + timing).  Setup always runs
            # before site_settings has a ``bot_protection_enabled`` row, so
            # we call check_bot_protection() directly (always enabled here).
            blocked, _reason = check_bot_protection(request)
            if blocked:
                log_action("bot_blocked_setup", request)
                flash(t("flash.submission_rejected_please_reload_the_page_and_try"), "error")
                return render_setup_form(400)

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            interface_language = normalize_language_selection(
                request.form.get("interface_language") or setup_language(),
                settings or {},
                default=setup_language(),
            )

            if not username or not password:
                flash(t("flash.enter_both_a_username_and_password_to_continue"), "error")
                return render_setup_form()
            if len(username) < 3:
                flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
                return render_setup_form()
            if len(username) > 50:
                flash(t("flash.username_cannot_exceed_50_characters"), "error")
                return render_setup_form()
            if not _is_valid_username(username):
                flash(t("flash.username_can_only_contain_letters_digits_underscores_and"), "error")
                return render_setup_form()
            if password != confirm:
                flash(t("flash.passwords_do_not_match_please_try_again"), "error")
                return render_setup_form()
            if len(password) < MIN_PASSWORD_LENGTH:
                flash(t("flash.password_must_contain_at_least_minpasswordlength_characters", MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH), "error")
                return render_setup_form()
            elif len(password) > MAX_PASSWORD_LENGTH:
                flash(t("flash.password_cannot_exceed_maxpasswordlength_characters", MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH), "error")
                return render_setup_form()

            hashed = generate_password_hash(password)
            try:
                user_id = db.complete_initial_setup(username, hashed, interface_language)
            except ValueError:
                flash(t("flash.initial_setup_has_already_been_completed"), "info")
                return redirect(url_for("login"))
            except db.IntegrityError:
                flash(t("flash.that_username_is_already_taken_please_choose_another"), "error")
                return render_setup_form()
            session.pop("_setup_authorized", None)

            # Optionally spawn built-in documentation
            spawn_docs = request.form.get("spawn_docs") == "1"
            if spawn_docs:
                simplified = request.form.get("simplified_docs") == "1"
                docs_language = normalize_docs_language(
                    request.form.get("docs_language") or interface_language,
                    settings or {},
                    default=interface_language,
                )
                db.spawn_wiki_docs(user_id, simplified=simplified, language=docs_language)

            log_action("setup_complete", request, username=username)
            notify_change("setup_complete", f"Admin account '{username}' created")
            flash(t("flash.admin_account_created_you_can_sign_in_now"), "success")
            return redirect(url_for("login"))

        return render_setup_form()

    @app.route("/login", methods=["GET", "POST"])
    @rate_limit(20, 60)
    def login():
        """Login page: authenticate the user and start a session.

        The ``@rate_limit`` decorator caps non-navigation requests to this
        endpoint so malformed POST bursts are still bounded.  Ordinary
        browser GET reloads are allowed through.  The stricter per-credential
        limit (``_check_login_rate_limit``, 5 failed attempts / 60s) is
        enforced separately inside the POST branch and counts only failed
        password checks.
        """
        if get_current_user() and request.method == "GET":
            return redirect(url_for("home"))

        settings = db.get_site_settings()
        if not settings or not settings["setup_done"]:
            return redirect(url_for("setup"))

        # Compute effective theme (light/dark) for public-facing auth pages
        site_tm = settings.get("default_theme_mode") or "dark"
        effective_theme = "light" if site_tm == "light" else "dark"

        maintenance = bool(settings["maintenance_mode"])

        # When maintenance mode is on, regular users are redirected to the
        # informational maintenance page; admins must use the dedicated
        # ``/admin`` login route instead of ``/login``.
        if maintenance and request.method == "GET":
            return redirect(url_for("maintenance"))

        if request.method == "POST":
            if not _check_login_rate_limit():
                log_action("login_rate_limited", request)
                flash(t("flash.too_many_login_attempts_were_detected_please_wait"), "error")
                return render_template("auth/login.html",
                                       bot_form_token=generate_form_token(),
                                       effective_theme_mode=effective_theme,
                                       theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                                       theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                                       theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                                       theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                                       theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                                       theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f")), 429

            # Bot protection check (honeypot + timing) when enabled
            if is_bot_protection_enabled():
                blocked, _reason = check_bot_protection(request)
                if blocked:
                    log_action("bot_blocked_login", request)
                    flash(t("flash.submission_rejected_please_reload_the_page_and_try"), "error")
                    return render_template("auth/login.html",
                                           bot_form_token=generate_form_token(),
                                           effective_theme_mode=effective_theme,
                                           theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                                           theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                                           theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                                           theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                                           theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                                           theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f")), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if len(password) > MAX_PASSWORD_LENGTH:
                _record_login_attempt()
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template("auth/login.html",
                                       bot_form_token=generate_form_token(),
                                       effective_theme_mode=effective_theme,
                                       theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                                       theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                                       theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                                       theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                                       theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                                       theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f"))

            user = db.get_user_by_username(username)

            if not user:
                # Constant-time: check against dummy hash to prevent timing enumeration
                check_password_hash(_DUMMY_HASH(), password)
                _record_login_attempt()
                log_action("login_failed", request, username=username)
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template("auth/login.html",
                                       bot_form_token=generate_form_token(),
                                       effective_theme_mode=effective_theme,
                                       theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                                       theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                                       theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                                       theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                                       theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                                       theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f"))

            if not check_password_hash(user["password"], password):
                _record_login_attempt()
                log_action("login_failed", request, username=username)
                flash(t("flash.invalid_username_or_password"), "error")
                return render_template("auth/login.html",
                                       bot_form_token=generate_form_token(),
                                       effective_theme_mode=effective_theme,
                                       theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                                       theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                                       theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                                       theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                                       theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                                       theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f"))

            if user["suspended"]:
                # Check if timed suspension has expired
                if db.check_suspension_expired(user["id"]):
                    # Suspension expired: continue login with a fresh user object
                    user = db.get_user_by_id(user["id"])
                else:
                    log_action("login_suspended", request, username=username)
                    # Create a minimal session so the dedicated suspension
                    # page can look up the user and display details.
                    session.clear()
                    session.permanent = False
                    session["user_id"] = user["id"]
                    session["restricted_suspension_session"] = True
                    return redirect(url_for("account_suspended"))

            if maintenance and user["role"] not in ("admin", "owner"):
                log_action("login_blocked_maintenance", request, username=username)
                flash(t("flash.this_wiki_is_temporarily_restricted_to_administrators_please"), "error")
                return redirect(url_for("maintenance"))

            session.clear()
            session.permanent = True
            # When the user ticks "Remember me", extend the session
            # lifetime to the configured remember-me window (default 30
            # days).  Otherwise the default app.permanent_session_lifetime
            # (7 days) applies.  Storing the chosen lifetime on the
            # request-scoped ``g`` lets ``set_security_headers`` (or any
            # session-cookie hook that reads it) honour the value when
            # serializing the cookie.
            remember = request.form.get("remember_me") in ("1", "on", "true", "yes")
            if remember:
                from flask import current_app
                lifetime = current_app.config.get("BW_REMEMBER_ME_LIFETIME")
                if lifetime is not None:
                    # Flask reads ``app.permanent_session_lifetime`` when
                    # serializing the cookie; setting it on ``session`` via
                    # the documented ``session.permanent = True`` is not
                    # enough to override per-session.  Instead we expose a
                    # session marker the auto-secure interface inspects.
                    session["_remember_me"] = True
            session["user_id"] = user["id"]
            session["_logged_in_at"] = datetime.now(timezone.utc).isoformat()
            # Issue a session token only when the one-session-per-user
            # limit is active.  When it is disabled the user row's
            # session_token stays NULL so multiple concurrent sessions
            # work without interference.
            settings = db.get_site_settings()
            if settings and settings.get("session_limit_enabled"):
                token = uuid.uuid4().hex
                session["session_token"] = token
                db.update_user(user["id"], session_token=token)
            else:
                # Wipe any stale token left over from when session-limit
                # was previously enabled, or from an admin password reset
                # / mass-logout invalidation.  Without this, the very next
                # request after a successful login would see
                # ``stored_token != session_token`` in
                # ``before_request_hook`` (DB has the old token, the new
                # session has none) and bounce the user back to /login
                # with the "another device signed in" flash. The symptom
                # users reported as "I just logged in and was immediately
                # logged back out".
                if user["session_token"] is not None:
                    db.update_user(user["id"], session_token=None)
            establish_user_session(
                user["id"],
                remember_me=remember,
                revoke_existing=bool(settings and settings.get("session_limit_enabled")),
            )
            _clear_login_attempts()
            db.update_user(user["id"], last_login_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

            # Check and award auto-triggered badges
            db.check_and_award_auto_badges(user["id"])

            # Check for unnotified badges
            unnotified = db.get_unnotified_badges(user["id"])
            if unnotified:
                session["badge_notifications"] = len(unnotified)

            log_action("login_success", request, user=user)
            notify_change("user_login", f"User '{user['username']}' logged in")
            emit_hook("after_login", user=user)

            # Hosted instances with random credentials force the admin to
            # set a new password on first login (and optionally change the
            # username).  Check after login and redirect to the forced
            # change page; the target page clears the flag on success.
            if user.get("force_password_change"):
                return redirect(url_for("force_change_password"))
            if user["role"] in ("admin", "owner") and user.get("onboarding_required"):
                return redirect(url_for("onboarding_setup"))

            # If login app selector is enabled, redirect to the app picker
            if settings.get("login_app_selector"):
                return redirect(url_for("app_selector"))

            next_url = get_safe_next_url(request.args.get("next") or request.form.get("next"))
            return redirect(next_url or url_for("home"))

        return render_template("auth/login.html",
                               bot_form_token=generate_form_token(),
                               effective_theme_mode=effective_theme,
                               theme_primary=settings.get("light_primary_color") if effective_theme == "light" else settings.get("primary_color", "#8fa0d4"),
                               theme_secondary=settings.get("light_secondary_color") if effective_theme == "light" else settings.get("secondary_color", "#1e1e2c"),
                               theme_accent=settings.get("light_accent_color") if effective_theme == "light" else settings.get("accent_color", "#7e9ada"),
                               theme_text=settings.get("light_text_color") if effective_theme == "light" else settings.get("text_color", "#c8ccd8"),
                               theme_sidebar=settings.get("light_sidebar_color") if effective_theme == "light" else settings.get("sidebar_color", "#1a1a24"),
                               theme_bg=settings.get("light_bg_color") if effective_theme == "light" else settings.get("bg_color", "#16161f"))

    @app.route("/app-selector")
    def app_selector():
        """Show the app selector page after login."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))

        s = db.get_site_settings()
        apps = []

        # Wiki is always available
        apps.append({
            "id": "wiki",
            "name": t("app_selector.wiki"),
            "description": t("app_selector.wiki_desc"),
            "url": url_for("home"),
            "icon": "📄",
        })

        # Kanban: check access
        _can_kanban = False
        if db.has_permission(user, "kanban.view"):
            from routes.kanban import _has_global_kanban_access
            if _has_global_kanban_access(user, s) or db.kanban_user_has_any_share(user["id"]):
                _can_kanban = True
        if _can_kanban:
            apps.append({
                "id": "kanban",
                "name": t("app_selector.kanban"),
                "description": t("app_selector.kanban_desc"),
                "url": url_for("kanban_list"),
                "icon": "📋",
            })

        # Canvas: check access
        _can_canvas = False
        if db.has_permission(user, "canvas.view"):
            from routes.canvas import _has_global_canvas_access
            if _has_global_canvas_access(user, s) or db.canvas_user_has_any_permission(user["id"]):
                _can_canvas = True
        if _can_canvas:
            apps.append({
                "id": "canvas",
                "name": t("app_selector.canvas"),
                "description": t("app_selector.canvas_desc"),
                "url": url_for("canvas_list"),
                "icon": "🎨",
            })

        # If only one app is available, skip the picker
        if len(apps) == 1:
            return redirect(apps[0]["url"])

        theme_mode = s.get("default_theme_mode", "dark")
        effective_theme = "light" if theme_mode == "light" else "dark"

        return render_template(
            "auth/app_selector.html",
            apps=apps,
            effective_theme_mode=effective_theme,
            theme_primary=s.get("light_primary_color") if effective_theme == "light" else s.get("primary_color", "#8fa0d4"),
            theme_secondary=s.get("light_secondary_color") if effective_theme == "light" else s.get("secondary_color", "#1e1e2c"),
            theme_accent=s.get("light_accent_color") if effective_theme == "light" else s.get("accent_color", "#7e9ada"),
            theme_text=s.get("light_text_color") if effective_theme == "light" else s.get("text_color", "#c8ccd8"),
            theme_sidebar=s.get("light_sidebar_color") if effective_theme == "light" else s.get("sidebar_color", "#1a1a24"),
            theme_bg=s.get("light_bg_color") if effective_theme == "light" else s.get("bg_color", "#16161f"),
        )

    @app.route("/signup", methods=["GET", "POST"])
    @rate_limit(10, 60)
    def signup():
        """Signup page: register a new account using a valid invite code.

        When *open signup* is active the invite code field is hidden and the
        backend skips invite-code validation entirely.
        """
        if get_current_user():
            return redirect(url_for("home"))

        settings = db.get_site_settings()
        if not settings or not settings["setup_done"]:
            return redirect(url_for("setup"))
        if settings["maintenance_mode"]:
            return redirect(url_for("maintenance"))

        open_signup = is_open_signup_active()
        approval_required = is_approval_required_active()

        # Compute effective theme for signup page
        site_tm = settings.get("default_theme_mode") if settings.get("default_theme_mode") in ("dark", "light") else "dark"
        signup_effective_theme = "light" if site_tm == "light" else "dark"

        def signup_theme_vars():
            """Return theme CSS variable kwargs for the signup page."""
            lt = signup_effective_theme
            return {
                "effective_theme_mode": lt,
                "theme_primary": settings.get("light_primary_color") if lt == "light" else settings.get("primary_color", "#8fa0d4"),
                "theme_secondary": settings.get("light_secondary_color") if lt == "light" else settings.get("secondary_color", "#1e1e2c"),
                "theme_accent": settings.get("light_accent_color") if lt == "light" else settings.get("accent_color", "#7e9ada"),
                "theme_text": settings.get("light_text_color") if lt == "light" else settings.get("text_color", "#c8ccd8"),
                "theme_sidebar": settings.get("light_sidebar_color") if lt == "light" else settings.get("sidebar_color", "#1a1a24"),
                "theme_bg": settings.get("light_bg_color") if lt == "light" else settings.get("bg_color", "#16161f"),
            }

        if request.method == "POST":
            # Bot protection check (honeypot + timing) when enabled
            if is_bot_protection_enabled():
                blocked, _reason = check_bot_protection(request)
                if blocked:
                    log_action("bot_blocked_signup", request)
                    flash(t("flash.submission_rejected_please_reload_the_page_and_try"), "error")
                    return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                           approval_required=approval_required,
                                           bot_form_token=generate_form_token()), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            invite = request.form.get("invite_code", "").strip().upper().replace(" ", "")
            code_row = None

            if not username or not password:
                flash(t("flash.all_fields_are_required_to_create_your_account"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if not open_signup and not invite:
                flash(t("flash.all_fields_are_required_to_create_your_account"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if len(username) < 3:
                flash(t("flash.username_must_be_at_least_3_characters_long"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if len(username) > 50:
                flash(t("flash.username_cannot_exceed_50_characters"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if not _is_valid_username(username):
                flash(t("flash.username_can_only_contain_letters_digits_underscores_and"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if password != confirm:
                flash(t("flash.passwords_do_not_match_please_try_again"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            if len(password) < MIN_PASSWORD_LENGTH:
                flash(t("flash.password_must_contain_at_least_minpasswordlength_characters", MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            elif len(password) > MAX_PASSWORD_LENGTH:
                flash(t("flash.password_cannot_exceed_maxpasswordlength_characters", MAX_PASSWORD_LENGTH=MAX_PASSWORD_LENGTH), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())

            if not open_signup:
                code_row = db.validate_invite_code(invite)
                # Allow generated codes entered without hyphen, while still
                # preserving 8-character custom codes like BANANA42.
                if not code_row and len(invite) == 8 and "-" not in invite:
                    hyphenated_invite = invite[:4] + "-" + invite[4:]
                    code_row = db.validate_invite_code(hyphenated_invite)
                    if code_row:
                        invite = hyphenated_invite
                if not code_row:
                    log_action("signup_invalid_code", request, code=invite, username=username)
                    flash(t("flash.the_invite_code_you_entered_is_invalid_or"), "error")
                    return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                           approval_required=approval_required,
                                           bot_form_token=generate_form_token())

            if db.get_user_by_username(username):
                flash(t("flash.that_username_is_already_taken_please_choose_another"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())

            hashed = generate_password_hash(password)
            try:
                user_id = db.create_signup_user(
                    username, hashed, invite_code=invite if not open_signup else None,
                    approval_required=approval_required,
                )
            except db.IntegrityError:
                flash(t("flash.that_username_is_already_taken_please_choose_another"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            except ValueError:
                flash(t("flash.that_invite_code_was_just_used_please_request"), "error")
                return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                                       approval_required=approval_required,
                                       bot_form_token=generate_form_token())
            signup_role = db.get_user_by_id(user_id)["role"]

            # When approval is required, set the user's status to pending
            if approval_required:
                log_action("signup_pending_approval", request, username=username,
                           invite_code=invite if not open_signup else "(open signup)",
                           role=signup_role)
                notify_change("user_signup_pending",
                              f"New user '{username}' registered: pending approval")
                flash(t("flash.account_created_pending_approval"), "success")
                return redirect(url_for("login"))
            else:
                log_action("signup_success", request, username=username,
                           invite_code=invite if not open_signup else "(open signup)",
                           role=signup_role)
                notify_change("user_signup", f"New user '{username}' registered")
                flash(t("flash.account_created_you_can_sign_in_now"), "success")
                return redirect(url_for("login"))

        return render_template("auth/signup.html", open_signup=open_signup, **signup_theme_vars(),
                               approval_required=approval_required,
                               bot_form_token=generate_form_token())

    @app.route("/logout", methods=["POST"])
    @rate_limit(30, 60)
    def logout():
        """Log out the current user by clearing the session."""
        user = get_current_user()
        if user:
            log_action("logout", request, user=user)
            if user.get("session_token") == session.get("session_token"):
                db.update_user(user["id"], session_token=None)
        db.revoke_user_session_token(session.get(SESSION_TOKEN_KEY))
        session.clear()
        flash(t("flash.you_have_been_logged_out"), "info")
        return redirect(url_for("login"))

    @app.route("/session-conflict")
    def session_conflict():
        """Inform the user that their session is active elsewhere."""
        settings = db.get_site_settings()
        if not settings:
            return redirect(url_for("login"))
        site_tm = settings.get("default_theme_mode") if settings.get("default_theme_mode") in ("dark", "light") else "dark"
        etm = "light" if site_tm == "light" else "dark"
        return render_template("auth/session_conflict.html", settings=settings,
                               effective_theme_mode=etm,
                               theme_primary=settings.get("light_primary_color") if etm == "light" else settings.get("primary_color"),
                               theme_secondary=settings.get("light_secondary_color") if etm == "light" else settings.get("secondary_color"),
                               theme_accent=settings.get("light_accent_color") if etm == "light" else settings.get("accent_color"),
                               theme_text=settings.get("light_text_color") if etm == "light" else settings.get("text_color"),
                               theme_sidebar=settings.get("light_sidebar_color") if etm == "light" else settings.get("sidebar_color"),
                               theme_bg=settings.get("light_bg_color") if etm == "light" else settings.get("bg_color"))

    @app.route("/session-conflict/force", methods=["POST"])
    @rate_limit(10, 60)
    def session_conflict_force():
        """Log out all other sessions by clearing the stored session token."""
        settings = db.get_site_settings()
        if not settings:
            flash(t("flash.enter_your_username_and_password_to_continue"), "error")
            return redirect(url_for("session_conflict"))
        # Apply the honeypot/bot-form-token check used by /login so automated
        # scripts cannot use this endpoint as an alternative brute-force path
        # that avoids the bot protection on the main login form.
        if is_bot_protection_enabled():
            blocked, _reason = check_bot_protection(request)
            if blocked:
                log_action("session_conflict_force_bot_blocked", request)
                flash(t("flash.invalid_username_or_password"), "error")
                return redirect(url_for("session_conflict"))
        # Apply the same per-client login rate limit used by /login so this
        # endpoint cannot be used as a credential-stuffing bypass.
        if not _check_login_rate_limit():
            log_action("session_conflict_force_rate_limited", request)
            flash(t("flash.too_many_login_attempts_were_detected_please_wait"), "error")
            return redirect(url_for("session_conflict"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash(t("flash.enter_your_username_and_password_to_continue"), "error")
            return redirect(url_for("session_conflict"))
        if len(password) > MAX_PASSWORD_LENGTH:
            _record_login_attempt()
            flash(t("flash.invalid_username_or_password"), "error")
            return redirect(url_for("session_conflict"))
        user = db.get_user_by_username(username)
        if not user:
            # Constant-time: still run the (expensive) scrypt verification
            # against the dummy hash so attackers cannot enumerate usernames
            # by observing response timing on this endpoint.
            check_password_hash(_DUMMY_HASH(), password)
            _record_login_attempt()
            flash(t("flash.invalid_username_or_password"), "error")
            return redirect(url_for("session_conflict"))
        if not check_password_hash(user["password"], password):
            _record_login_attempt()
            flash(t("flash.invalid_username_or_password"), "error")
            return redirect(url_for("session_conflict"))
        if user["suspended"] and not db.check_suspension_expired(user["id"]):
            session.clear()
            session.permanent = False
            session["user_id"] = user["id"]
            return redirect(url_for("account_suspended"))
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        if settings["session_limit_enabled"]:
            # Issue a new session token so all other sessions are invalidated
            token = uuid.uuid4().hex
            db.update_user(user["id"], session_token=token)
            session["session_token"] = token
        establish_user_session(
            user["id"],
            revoke_existing=True,
        )
        _clear_login_attempts()
        log_action("session_conflict_force", request, user=user)
        flash(t("flash.your_other_active_sessions_have_been_signed_out"), "info")

        next_url = get_safe_next_url(request.args.get("next") or request.form.get("next"))
        return redirect(next_url or url_for("home"))

    @app.route("/maintenance")
    def maintenance():
        """Display the maintenance page when the site is in maintenance mode."""
        settings = db.get_site_settings()
        if not settings or not settings["maintenance_mode"]:
            return redirect(url_for("login"))
        site_tm = settings.get("default_theme_mode") if settings.get("default_theme_mode") in ("dark", "light") else "dark"
        etm = "light" if site_tm == "light" else "dark"
        return render_template("auth/maintenance.html", settings=settings,
                               effective_theme_mode=etm,
                               theme_primary=settings.get("light_primary_color") if etm == "light" else settings.get("primary_color"),
                               theme_secondary=settings.get("light_secondary_color") if etm == "light" else settings.get("secondary_color"),
                               theme_accent=settings.get("light_accent_color") if etm == "light" else settings.get("accent_color"),
                               theme_text=settings.get("light_text_color") if etm == "light" else settings.get("text_color"),
                               theme_sidebar=settings.get("light_sidebar_color") if etm == "light" else settings.get("sidebar_color"),
                               theme_bg=settings.get("light_bg_color") if etm == "light" else settings.get("bg_color"))

    @app.route("/activation-pending")
    def activation_pending():
        """Show the pending activation page for users awaiting admin approval."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if user.get("approval_status") != "pending":
            return redirect(url_for("home"))
        settings = db.get_site_settings()
        site_tm = settings.get("default_theme_mode") if settings and settings.get("default_theme_mode") in ("dark", "light") else "dark"
        etm = "light" if site_tm == "light" else "dark"
        return render_template("auth/activation_pending.html", settings=settings,
                               effective_theme_mode=etm,
                               theme_primary=settings.get("light_primary_color") if etm == "light" else settings.get("primary_color"),
                               theme_secondary=settings.get("light_secondary_color") if etm == "light" else settings.get("secondary_color"),
                               theme_accent=settings.get("light_accent_color") if etm == "light" else settings.get("accent_color"),
                               theme_text=settings.get("light_text_color") if etm == "light" else settings.get("text_color"),
                               theme_sidebar=settings.get("light_sidebar_color") if etm == "light" else settings.get("sidebar_color"),
                               theme_bg=settings.get("light_bg_color") if etm == "light" else settings.get("bg_color")), 200

    @app.route("/activation-denied")
    def activation_denied():
        """Show the denied activation page for users whose account was denied."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if user.get("approval_status") != "denied":
            return redirect(url_for("home"))
        settings = db.get_site_settings()
        timeout_hours = (settings or {}).get("approval_denied_timeout_hours", 24)
        denied_at = user.get("denied_at")
        deletion_time = None
        if denied_at:
            try:
                denied_dt = datetime.fromisoformat(denied_at.replace("Z", "+00:00"))
                if denied_dt.tzinfo is None:
                    denied_dt = denied_dt.replace(tzinfo=timezone.utc)
                deletion_dt = denied_dt + timedelta(hours=timeout_hours)
                deletion_time = format_datetime(deletion_dt.isoformat())
            except (ValueError, TypeError):
                pass
        # Mark as notified
        if not user.get("denied_notified"):
            db.update_user(user["id"], denied_notified=1)
        site_tm = settings.get("default_theme_mode") if settings and settings.get("default_theme_mode") in ("dark", "light") else "dark"
        etm = "light" if site_tm == "light" else "dark"
        return render_template("auth/activation_denied.html",
                               timeout_hours=timeout_hours,
                               deletion_time=deletion_time,
                               effective_theme_mode=etm,
                               theme_primary=settings.get("light_primary_color") if etm == "light" else settings.get("primary_color"),
                               theme_secondary=settings.get("light_secondary_color") if etm == "light" else settings.get("secondary_color"),
                               theme_accent=settings.get("light_accent_color") if etm == "light" else settings.get("accent_color"),
                               theme_text=settings.get("light_text_color") if etm == "light" else settings.get("text_color"),
                               theme_sidebar=settings.get("light_sidebar_color") if etm == "light" else settings.get("sidebar_color"),
                               theme_bg=settings.get("light_bg_color") if etm == "light" else settings.get("bg_color")), 200

    @app.route("/account-suspended")
    def account_suspended():
        """Show a dedicated page for suspended accounts with reason and expiry details."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["suspended"]:
            session.clear()
            return redirect(url_for("login"))
        # Auto-unsuspend if timed suspension has expired
        if db.check_suspension_expired(user["id"]):
            session.clear()
            return redirect(url_for("login"))

        settings = db.get_site_settings()
        site_tm = settings.get("default_theme_mode") if settings and settings.get("default_theme_mode") in ("dark", "light") else "dark"
        etm = "light" if site_tm == "light" else "dark"

        # Build context for the template
        reason = user["suspend_reason"] if user["suspend_reason_visible"] else None
        suspended_until_str = None
        is_permanent = not user["suspended_until"]
        if user["suspended_until"] and user["suspend_time_visible"]:
            suspended_until_str = format_datetime(user["suspended_until"])
        elif user["suspended_until"] and not user["suspend_time_visible"]:
            # There is an expiry but the admin chose not to show it
            is_permanent = False

        return render_template("auth/account_suspended.html",
                               settings=settings,
                               can_delete_account=bool(
                                   settings.get("suspended_account_deletion_enabled")
                                   and not user["is_superuser"]
                               ),
                               reason=reason,
                               suspended_until=suspended_until_str,
                               is_permanent=is_permanent,
                               effective_theme_mode=etm,
                               theme_primary=settings.get("light_primary_color") if etm == "light" else settings.get("primary_color"),
                               theme_secondary=settings.get("light_secondary_color") if etm == "light" else settings.get("secondary_color"),
                               theme_accent=settings.get("light_accent_color") if etm == "light" else settings.get("accent_color"),
                               theme_text=settings.get("light_text_color") if etm == "light" else settings.get("text_color"),
                               theme_sidebar=settings.get("light_sidebar_color") if etm == "light" else settings.get("sidebar_color"),
                               theme_bg=settings.get("light_bg_color") if etm == "light" else settings.get("bg_color")), 403

    @app.route("/lockdown")
    def lockdown_legacy_redirect():
        """Legacy redirect: ``/lockdown`` was renamed to ``/maintenance``."""
        return redirect(url_for("maintenance"), code=301)
