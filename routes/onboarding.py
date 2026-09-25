"""
BananaWiki: First-run wiki onboarding and visual introduction routes.
"""

from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import (
    current_app, flash, g, redirect, render_template, request, session, url_for,
)

import db
from bananawiki_sdk import emit_hook
from helpers import (
    _is_valid_username,
    admin_required,
    get_current_user,
    login_required,
    rate_limit,
    t,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
)
from helpers._passwords import generate_password_hash
from plugin_loader import trigger_plugin_enable
from sync import notify_change
from wiki_logger import log_action


CORE_PLUGIN_IDS = {"audit", "page_history", "user_data_export"}
EASY_PLUGIN_IDS = CORE_PLUGIN_IDS | {"kanban", "canvas", "drafts", "tts"}
# Only plugins that seed_builtin_plugins() actually installs belong here.
# Experimental built-ins are not seeded, so offering them at onboarding
# would show a checkbox that silently does nothing.
OPTIONAL_PLUGIN_IDS = [
    "kanban",
    "canvas",
    "drafts",
    "page_governance",
    "chat",
    "attachments",
    "custom_pages",
    "assessments",
    "user_profiles",
    "tts",
    "api_service",
    "deletion_slowdown",
    "temporary_accounts",
]

FEATURE_COPY = {
    "kanban": ("Kanban", "Boards, columns, tickets, and lightweight work tracking."),
    "canvas": ("Canvas", "Visual knowledge maps linked to wiki pages."),
    "drafts": ("Draft sync", "Autosaved page drafts while editors write."),
    "page_governance": ("Page governance", "Reservations, protection, and contribution approval."),
    "chat": ("Chats and Groups", "Direct messages and group spaces inside the wiki."),
    "attachments": ("Attachments", "Authenticated page and ticket file storage."),
    "custom_pages": ("Custom pages", "Structured custom content types for richer wikis."),
    "assessments": ("Assessments", "Page quizzes and reader checks."),
    "user_profiles": ("User profiles", "Member pages, avatars, and profile fields."),
    "tts": ("Text to speech", "Generate and play page audio."),
    "api_service": ("API service", "Token-based REST API access."),
    "deletion_slowdown": ("Deletion slowdown", "Grace period for destructive page deletes."),
    "temporary_accounts": ("Temporary accounts", "Time-limited users, pages, and roles."),
}

TOUR_ROLES = ("user", "editor", "admin")
TOUR_ROLE_LABELS = {
    "user": "User",
    "editor": "Editor",
    "admin": "Admin",
}
DEFAULT_TOUR_SWITCHING_ROLES = "user,editor,admin"


def _feature_label(plugin_id, field):
    """Return translated onboarding feature copy with the English constant as fallback."""
    idx = 0 if field == "name" else 1
    return t(
        f"onboarding.feature.{plugin_id}.{field}",
        default=FEATURE_COPY[plugin_id][idx],
    )


def _tour_role_label(role):
    """Return the translated label for a guided-tour perspective role."""
    return t(f"onboarding.role.{role}", default=TOUR_ROLE_LABELS.get(role, role.title()))


def _tour_text(step_id, field, default):
    """Return translated copy for a guided-tour step."""
    return t(f"onboarding.tour.{step_id}.{field}", default=default)


def _tour_step(step_id, url, selector, presenter_id, title, body, target_label):
    """Build a guided-tour step with translated user-facing text."""
    return {
        "url": url,
        "selector": selector,
        "presenter_id": presenter_id,
        "title": _tour_text(step_id, "title", title),
        "body": _tour_text(step_id, "body", body),
        "target_label": _tour_text(step_id, "target_label", target_label),
    }

def _utc_now():
    """Return the current UTC timestamp as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _existing_plugin_ids():
    """Return ids for plugins currently known to the database."""
    return {row["id"] for row in db.list_plugins()}


def _configurable_builtin_plugin_ids():
    """Return builtin plugin ids that onboarding may enable or disable."""
    return {row["id"] for row in db.list_plugins() if row["builtin"] and row["id"] not in CORE_PLUGIN_IDS}


def _sync_plugin_selection(selected_ids, *, disable_unselected):
    """Apply onboarding plugin selections and trigger enable hooks."""
    existing = _existing_plugin_ids()
    selected = {pid for pid in selected_ids if pid in existing}
    core = {pid for pid in CORE_PLUGIN_IDS if pid in existing}
    selected |= core

    configurable = _configurable_builtin_plugin_ids()
    if disable_unselected:
        db.set_plugins_enabled(sorted(configurable - selected), enabled=False)
    db.set_plugins_enabled(sorted(selected), enabled=True)

    for plugin_id in selected:
        try:
            trigger_plugin_enable(plugin_id, current_app)
        except Exception:
            current_app.logger.exception("Failed to activate plugin during onboarding: %s", plugin_id)

    if hasattr(g, "enabled_plugins"):
        del g.enabled_plugins


def _create_onboarding_users(intro_required):
    """Create additional users submitted from the onboarding form."""
    usernames = request.form.getlist("new_username")
    passwords = request.form.getlist("new_password")
    roles = request.form.getlist("new_role")
    force_password_indexes = set(request.form.getlist("new_force_password_change"))
    created = []
    seen = set()

    for idx, raw_username in enumerate(usernames):
        username = (raw_username or "").strip()
        password = passwords[idx] if idx < len(passwords) else ""
        role = roles[idx] if idx < len(roles) else "user"
        if not username and not password:
            continue
        if not username or not password:
            raise ValueError(t("onboarding.error.user_needs_username_password"))
        if len(username) < 3:
            raise ValueError(t("onboarding.error.username_too_short"))
        if len(username) > 50:
            raise ValueError(t("onboarding.error.username_too_long"))
        if not _is_valid_username(username):
            raise ValueError(t("onboarding.error.username_invalid_chars"))
        if username.lower() in seen or db.get_user_by_username(username):
            raise ValueError(t("onboarding.error.username_taken", username=username))
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(t("onboarding.error.password_too_short", min=MIN_PASSWORD_LENGTH))
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError(t("onboarding.error.password_too_long", max=MAX_PASSWORD_LENGTH))
        if role not in ("user", "editor", "admin"):
            raise ValueError(t("onboarding.error.invalid_role"))

        user_id = db.create_user(username, generate_password_hash(password), role=role)
        updates = {}
        if intro_required:
            updates["intro_required"] = 1
        if str(idx) in force_password_indexes:
            updates["force_password_change"] = 1
        if updates:
            db.update_user(user_id, **updates)
        emit_hook("after_user_create", user=db.get_user_by_id(user_id))
        created.append(username)
        seen.add(username.lower())

    return created


def _onboarding_required(user):
    """Return whether the current admin must complete onboarding."""
    return bool(user and user["role"] in ("admin", "owner") and user.get("onboarding_required"))


def _intro_required(user):
    """Return whether the current user must see the intro screen."""
    return bool(user and user.get("intro_required"))


def _onboarding_replay_allowed(settings=None):
    """Return whether completed users may replay onboarding and intro flows."""
    settings = settings or db.get_site_settings() or {}
    return not bool(settings.get("onboarding_replay_disabled", 0))


def _role_at_least(user, roles):
    """Return whether a user role is present in an allowed-role collection."""
    return bool(user and user["role"] in roles)


def _tour_real_role(user):
    """Return the user's non-preview role for guided-tour decisions."""
    if not user:
        return "user"
    if user.get("tour_real_role") in TOUR_ROLES:
        return user.get("tour_real_role")
    if user["role"] in ("admin", "owner"):
        return "admin"
    if user["role"] == "editor":
        return "editor"
    return "user"


def _tour_role_at_least(role, roles):
    """Return whether a tour preview role reaches any required role."""
    role_rank = {"user": 0, "editor": 1, "admin": 2}
    return any(role_rank.get(role, 0) >= role_rank.get(r, 0) for r in roles)


def _tour_switching_allowed_for_user(user, settings=None):
    """Return whether a user may switch preview roles during the tour."""
    if not user:
        return False
    settings = settings or db.get_site_settings() or {}
    if not settings.get("intro_role_switching_enabled", 1):
        return False
    allowed = {
        part.strip().lower()
        for part in (settings.get("intro_role_switching_roles") or DEFAULT_TOUR_SWITCHING_ROLES).split(",")
        if part.strip()
    }
    return _tour_real_role(user) in allowed


def get_tour_role_options(user, settings=None):
    """Return selectable guided-tour role options for a user."""
    real_role = _tour_real_role(user)
    if _tour_switching_allowed_for_user(user, settings):
        return [
            {
                "id": role,
                "label": _tour_role_label(role),
                "is_current_role": role == real_role,
            }
            for role in TOUR_ROLES
        ]
    return [{
        "id": real_role,
        "label": _tour_role_label(real_role),
        "is_current_role": True,
    }]


def normalize_tour_role(value, user, settings=None):
    """Return a valid guided-tour role for the submitted value."""
    role = (value or "").strip().lower()
    allowed = {option["id"] for option in get_tour_role_options(user, settings)}
    if role in allowed:
        return role
    return _tour_real_role(user)


def _current_tour_role(user, settings=None):
    """Return the guided-tour role currently stored in the session."""
    return normalize_tour_role(session.get("guided_tour_role"), user, settings)


def _tour_path_for_url(url):
    """Extract the path component used for guided-tour preview routing."""
    parsed = urlparse(url or "")
    return parsed.path or "/"


def _store_guided_tour_step(steps, step_index):
    """Persist the active guided-tour step and preview path in the session."""
    if not steps:
        session.pop("guided_tour_preview_path", None)
        return
    step_index = max(0, min(step_index, len(steps) - 1))
    session["guided_tour_step"] = step_index
    session["guided_tour_preview_path"] = _tour_path_for_url(steps[step_index]["url"])


def build_live_tour_steps(user, perspective_role=None, settings=None):
    """Return tour steps for the selected role perspective.

    The selected role perspective always points at real application pages. When
    the perspective differs from the user's real role, auth applies a temporary
    read-only role lens only to the exact current tour page.
    """
    settings = settings or db.get_site_settings() or {}
    role = normalize_tour_role(perspective_role, user, settings)
    is_role_preview = role != _tour_real_role(user)
    steps = [
        _tour_step(
            "home",
            url_for("home"),
            "[data-tour-id='site-logo']",
            "site-logo",
            "Start from the wiki home",
            "This takes everyone back to the main wiki space. It is the safest reset point when someone gets deep into pages or tools.",
            "Click the highlighted site name to continue.",
        ),
        _tour_step(
            "search",
            url_for("home"),
            "[data-tour-id='sidebar-search']",
            "sidebar-search",
            "Find pages from the explorer",
            "The sidebar search helps users jump straight to pages and categories. It can also search page content when the scope button is enabled.",
            "Click the highlighted search box to continue.",
        ),
        _tour_step(
            "personal_settings",
            url_for("user_settings"),
            "[data-tour-id='personal-settings-panel'], [data-tour-id='topbar-settings-link']",
            "personal-settings-panel",
            "Personal settings stay separate",
            "Each user can tune their own language, accessibility preferences, profile settings, and account options without changing the whole wiki.",
            "Click the highlighted account settings area to continue.",
        ),
    ]

    if _tour_role_at_least(role, ("editor",)):
        steps.extend([
            _tour_step(
                "create_page",
                url_for("create_page"),
                "[data-tour-id='create-page-title']",
                "create-page-title",
                "Create pages from the real editor flow",
                "Editors and admins can create pages, choose categories, and then write with draft protection once the page exists.",
                "Click the title field to continue.",
            ),
            _tour_step(
                "quick_create",
                url_for("create_page"),
                "[data-tour-id='sidebar-new-page']",
                "sidebar-new-page",
                "The sidebar has quick authoring shortcuts",
                "The page and folder buttons keep content creation close to the explorer, which is useful when organizing a wiki as you write.",
                "Click the highlighted quick-create button to continue.",
            ),
        ])

    if db.is_plugin_enabled("kanban") and _tour_role_at_least(role, ("editor",)):
        steps.append(_tour_step(
            "kanban",
            url_for("kanban_list"),
            "[data-tour-id='kanban-new-board'], [data-tour-id='kanban-page']",
            "kanban-new-board",
            "Kanban turns plans into boards",
            "Kanban boards are where teams can track work, decisions, bugs, and tasks without leaving the wiki.",
            "Click the highlighted Kanban area to continue.",
        ))

    if db.is_plugin_enabled("canvas") and _tour_role_at_least(role, ("editor",)):
        steps.append(_tour_step(
            "canvas",
            url_for("canvas_list"),
            "[data-tour-id='canvas-new-canvas'], [data-tour-id='canvas-page']",
            "canvas-new-canvas",
            "Canvas maps ideas visually",
            "Canvas gives the wiki a spatial view for diagrams, knowledge maps, planning, and connected notes.",
            "Click the highlighted Canvas area to continue.",
        ))

    if _tour_role_at_least(role, ("admin",)):
        steps.extend([
            _tour_step(
                "admin_settings",
                url_for("admin_settings"),
                "[data-tour-id='admin-site-name']",
                "admin-site-name",
                "Admin settings shape the whole wiki",
                "This is where admins manage the site name, language defaults, onboarding options, security, appearance, and operational behavior.",
                "Click the highlighted settings section to continue.",
            ),
            _tour_step(
                "admin_users",
                url_for("admin_users"),
                "[data-tour-id='admin-create-user']",
                "admin-create-user",
                "Users and roles are managed here",
                "Admins can add people, assign roles, reset access, and decide who should read, edit, or administer the workspace.",
                "Click the highlighted user management area to continue.",
            ),
            _tour_step(
                "admin_plugins",
                url_for("admin_plugins"),
                "[data-tour-id='admin-plugins-list'] thead, [data-tour-id='admin-plugins-list']",
                "admin-plugins-list",
                "Plugins control the feature surface",
                "Admins can enable or disable features later, so Easy and Advanced onboarding choices are never permanent.",
                "Click the highlighted plugin area to finish.",
            ),
        ])

    for step in steps:
        step["tour_role"] = role
        step["tour_role_label"] = _tour_role_label(role)
        step["real_role"] = _tour_real_role(user)
        step["real_role_label"] = _tour_role_label(step["real_role"])
        step["is_presenter"] = False
        step["is_role_preview"] = is_role_preview

    return steps


def build_live_tour_state(user, step_index=None):
    """Build the template state for the active guided tour step."""
    settings = db.get_site_settings() or {}
    tour_role = _current_tour_role(user, settings)
    steps = build_live_tour_steps(user, tour_role, settings=settings)
    if not steps:
        return None
    if step_index is None:
        step_index = int(session.get("guided_tour_step", 0) or 0)
    step_index = max(0, min(step_index, len(steps) - 1))
    step = dict(steps[step_index])
    step.update({
        "active": bool(session.get("guided_tour_active")),
        "step_index": step_index,
        "step_number": step_index + 1,
        "total_steps": len(steps),
        "is_first": step_index == 0,
        "is_last": step_index == len(steps) - 1,
        "prev_url": url_for("tour_step", step_index=step_index - 1) if step_index > 0 else "",
        "next_url": url_for("tour_step", step_index=step_index + 1) if step_index < len(steps) - 1 else url_for("tour_finish"),
        "finish_url": url_for("tour_finish"),
        "role_options": get_tour_role_options(user, settings),
        "role_switch_url_base": url_for("tour_role", role="__ROLE__"),
    })
    return step


def register_onboarding_routes(app):
    """Register first-run onboarding and visual introduction routes."""

    @app.route("/onboarding", methods=["GET", "POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def onboarding_setup():
        """Show and process the first-run onboarding setup form."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        required = _onboarding_required(user)
        is_replay = not required
        if not required:
            if _intro_required(user):
                return redirect(url_for("onboarding_intro"))
            if not _onboarding_replay_allowed(settings):
                return redirect(url_for("home"))

        if request.method == "POST":
            mode = request.form.get("mode", "easy")
            if mode not in ("easy", "advanced"):
                mode = "easy"

            site_name = request.form.get("site_name", "").strip() or "BananaWiki"
            if len(site_name) > 100:
                flash(t("onboarding.error.site_name_too_long"), "error")
                return redirect(url_for("onboarding_setup"))

            new_user_intro_enabled = 1 if request.form.get("new_user_intro_enabled") else 0
            default_theme_mode = request.form.get("default_theme_mode", settings.get("default_theme_mode", "dark"))
            if default_theme_mode not in ("dark", "light"):
                default_theme_mode = "dark"

            if mode == "easy":
                selected_plugins = EASY_PLUGIN_IDS
                kanban_access = "editor"
                canvas_access = "editor"
            else:
                selected_plugins = set(request.form.getlist("plugins"))
                kanban_access = request.form.get("kanban_access", "admin")
                canvas_access = request.form.get("canvas_access", "admin")
            if kanban_access not in ("admin", "editor", "all"):
                kanban_access = "admin"
            if canvas_access not in ("admin", "editor", "all"):
                canvas_access = "admin"

            try:
                created_users = _create_onboarding_users(bool(new_user_intro_enabled))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("onboarding_setup"))

            _sync_plugin_selection(selected_plugins, disable_unselected=True)
            db.update_site_settings(
                site_name=site_name,
                default_theme_mode=default_theme_mode,
                new_user_intro_enabled=new_user_intro_enabled,
                kanban_access=kanban_access,
                kanban_write_access=kanban_access,
                canvas_access=canvas_access,
                canvas_write_access=canvas_access,
            )
            now = _utc_now()
            db.update_user(
                user["id"],
                onboarding_required=0,
                onboarding_completed_at=now,
                intro_required=1,
            )
            log_action(
                "onboarding_complete",
                request,
                user=user,
                mode=mode,
                created_users=len(created_users),
            )
            notify_change("onboarding_complete", f"Wiki onboarding completed by '{user['username']}'")
            flash(t("onboarding.flash.completed"), "success")
            return redirect(url_for("home"))

        existing = _existing_plugin_ids()
        feature_options = [
            {
                "id": plugin_id,
                "name": _feature_label(plugin_id, "name"),
                "description": _feature_label(plugin_id, "description"),
                "enabled": bool(db.is_plugin_enabled(plugin_id)),
            }
            for plugin_id in OPTIONAL_PLUGIN_IDS
            if plugin_id in existing and plugin_id in FEATURE_COPY
        ]
        return render_template(
            "onboarding/setup.html",
            feature_options=feature_options,
            easy_plugin_ids=sorted(EASY_PLUGIN_IDS & existing),
            is_replay=is_replay,
        )

    @app.route("/intro", methods=["GET", "POST"])
    @login_required
    @rate_limit(20, 60)
    def onboarding_intro():
        """Show the intro page or mark the intro flow as completed."""
        user = get_current_user()
        settings = db.get_site_settings() or {}
        preview = request.args.get("preview") == "1" and user["role"] in ("admin", "owner")
        replay_allowed = _onboarding_replay_allowed(settings)
        intro_required = _intro_required(user)
        onboarding_required = bool(user.get("onboarding_required"))
        can_start_tour = bool(intro_required or onboarding_required or replay_allowed)
        if request.method == "POST":
            if not intro_required and not onboarding_required and not replay_allowed:
                return redirect(url_for("home"))
            now = _utc_now()
            updates = {}
            if intro_required:
                updates["intro_required"] = 0
                updates["intro_completed_at"] = now
            if onboarding_required:
                updates["onboarding_required"] = 0
                updates["onboarding_completed_at"] = now
            if updates:
                db.update_user(user["id"], **updates)
            return redirect(url_for("home"))

        if not preview and not intro_required and not onboarding_required and not replay_allowed:
            return redirect(url_for("home"))

        return render_template(
            "onboarding/intro.html",
            preview=preview,
            can_start_tour=can_start_tour,
            tour_steps=build_live_tour_steps(user, settings=settings),
            tour_role_options=get_tour_role_options(user, settings),
            default_tour_role=_tour_real_role(user),
        )

    @app.route("/tour/start", methods=["POST"])
    @login_required
    @rate_limit(20, 60)
    def tour_start():
        """Start the guided tour for the selected preview role."""
        user = get_current_user()
        if user.get("onboarding_required"):
            return redirect(url_for("onboarding_setup"))
        settings = db.get_site_settings() or {}
        if not user.get("intro_required") and not _onboarding_replay_allowed(settings):
            return redirect(url_for("home"))
        tour_role = normalize_tour_role(request.form.get("tour_role"), user, settings)
        steps = build_live_tour_steps(user, tour_role, settings=settings)
        if not steps:
            return redirect(url_for("home"))
        session["guided_tour_active"] = 1
        session["guided_tour_role"] = tour_role
        _store_guided_tour_step(steps, 0)
        return redirect(steps[0]["url"])

    @app.route("/tour/role/<role>", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def tour_role(role):
        """Switch the active guided-tour preview role."""
        user = get_current_user()
        if not session.get("guided_tour_active"):
            return redirect(url_for("onboarding_intro"))
        settings = db.get_site_settings() or {}
        tour_role = normalize_tour_role(role, user, settings)
        steps = build_live_tour_steps(user, tour_role, settings=settings)
        if not steps:
            return redirect(url_for("home"))
        session["guided_tour_role"] = tour_role
        _store_guided_tour_step(steps, 0)
        return redirect(steps[0]["url"])

    @app.route("/tour/presenter/<role>/<int:step_index>")
    @login_required
    @rate_limit(60, 60)
    def tour_presenter(role, step_index):
        """Jump to a guided-tour presenter step for admins."""
        user = get_current_user()
        if not session.get("guided_tour_active"):
            return redirect(url_for("onboarding_intro"))
        settings = db.get_site_settings() or {}
        tour_role = normalize_tour_role(role, user, settings)
        steps = build_live_tour_steps(user, tour_role, settings=settings)
        if not steps:
            return redirect(url_for("home"))
        step_index = max(0, min(step_index, len(steps) - 1))
        session["guided_tour_role"] = tour_role
        _store_guided_tour_step(steps, step_index)
        return redirect(steps[step_index]["url"])

    @app.route("/tour/step/<int:step_index>", methods=["POST"])
    @login_required
    @rate_limit(60, 60)
    def tour_step(step_index):
        """Move to a neighboring guided-tour step."""
        user = get_current_user()
        if not session.get("guided_tour_active"):
            return redirect(url_for("onboarding_intro"))
        settings = db.get_site_settings() or {}
        tour_role = _current_tour_role(user, settings)
        steps = build_live_tour_steps(user, tour_role, settings=settings)
        if not steps:
            session.pop("guided_tour_active", None)
            session.pop("guided_tour_step", None)
            session.pop("guided_tour_role", None)
            return redirect(url_for("home"))
        current_step = int(session.get("guided_tour_step", 0) or 0)
        if step_index not in {current_step - 1, current_step, current_step + 1}:
            return redirect(steps[max(0, min(current_step, len(steps) - 1))]["url"])
        step_index = max(0, min(step_index, len(steps) - 1))
        _store_guided_tour_step(steps, step_index)
        return redirect(steps[step_index]["url"])

    @app.route("/tour/finish", methods=["POST"])
    @login_required
    @rate_limit(20, 60)
    def tour_finish():
        """Finish the guided tour and clear its session state."""
        user = get_current_user()
        if not session.get("guided_tour_active"):
            return redirect(url_for("home"))
        session.pop("guided_tour_active", None)
        session.pop("guided_tour_step", None)
        session.pop("guided_tour_role", None)
        session.pop("guided_tour_preview_path", None)
        updates = {}
        now = _utc_now()
        if user.get("intro_required"):
            updates["intro_required"] = 0
            updates["intro_completed_at"] = now
        if updates:
            db.update_user(user["id"], **updates)
        flash(t("onboarding.flash.tour_completed"), "success")
        return redirect(url_for("home"))
