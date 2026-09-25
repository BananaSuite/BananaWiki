"""Authentication and authorization decorators and helpers."""

import functools
import sqlite3
from datetime import datetime, timezone as _tz

from flask import session, redirect, url_for, flash, g, request, jsonify, abort

import db
import config
from wiki_logger import get_logger


def is_public_mode_active():
    """Return True when the wiki is in public-access mode.

    Checks both the ``public_mode`` flag and an optional ``public_mode_until``
    datetime.  If the datetime has passed, the mode is considered inactive
    (the actual DB revert happens in the periodic cleanup).
    """
    if getattr(config, "FORBID_PUBLIC_MODE", False):
        return False
    try:
        settings = getattr(g, "_site_settings", None)
    except RuntimeError:
        settings = None
    if settings is None:
        settings = db.get_site_settings()
        try:
            g._site_settings = settings
        except RuntimeError:
            pass  # Outside a request context: can't store in g
    if not settings:
        return False
    if not settings.get("public_mode"):
        return False
    until = settings.get("public_mode_until")
    if until:
        try:
            exp = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            if datetime.now(_tz.utc) >= exp:
                return False
        except (ValueError, TypeError):
            pass
    return True


def is_page_builder_active():
    """Return whether the core builder is enabled locally and allowed by the host."""
    if getattr(config, "FORBID_PAGE_BUILDER", False):
        return False
    try:
        settings = getattr(g, "_site_settings", None)
    except RuntimeError:
        settings = None
    if settings is None:
        settings = db.get_site_settings()
    return bool(settings and settings.get("page_builder_enabled"))


def user_can_use_page_builder(user):
    """Apply the wiki admin's minimum-role policy to builder access."""
    if not user or not is_page_builder_active():
        return False
    settings = db.get_site_settings() or {}
    minimum = settings.get("page_builder_access", "admin")
    allowed = {
        "admin": {"admin", "owner"},
        "editor": {"editor", "admin", "owner"},
        "user": {"user", "editor", "admin", "owner"},
    }
    return user["role"] in allowed.get(minimum, allowed["admin"])


def is_approval_required_active():
    """Return True when new signups require admin approval before they can use the wiki.

    Reads from the per-request (or freshly loaded) settings and respects
    the ``approval_required`` flag.  There is no expiry mechanism on this
    setting: it is a permanent policy toggle.
    """
    try:
        settings = getattr(g, "_site_settings", None)
    except RuntimeError:
        settings = None
    if settings is None:
        settings = db.get_site_settings()
        try:
            g._site_settings = settings
        except RuntimeError:
            pass
    if not settings:
        return False
    return bool(settings.get("approval_required"))


def is_open_signup_active():
    """Return True when sign-up is allowed without an invite code.

    Checks both the ``open_signup`` flag and an optional ``open_signup_until``
    datetime.  If the datetime has passed, the mode is considered inactive.
    """
    try:
        settings = getattr(g, "_site_settings", None)
    except RuntimeError:
        settings = None
    if settings is None:
        settings = db.get_site_settings()
        try:
            g._site_settings = settings
        except RuntimeError:
            pass  # Outside a request context: can't store in g
    if not settings:
        return False
    if not settings.get("open_signup"):
        return False
    until = settings.get("open_signup_until")
    if until:
        try:
            exp = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            if datetime.now(_tz.utc) >= exp:
                return False
        except (ValueError, TypeError):
            pass
    return True


_PUBLIC_MODE_ENDPOINTS = {
    # Core read-only wiki pages.
    "home",
    "view_page",
    "view_announcement",
    "search_page",
    # Public search/navigation helpers used by the wiki shell.
    "api_pages_search",
    "api_page_preview_by_slug",
    "api_sidebar_search",
    "api_sidebar_pages",
    "navigation_pages",
    # Anonymous accessibility preferences are stored in a public cookie.
    "api_get_accessibility",
    "api_save_accessibility",
    "api_reset_accessibility",
    # Read-only TTS playback/status endpoints do their own public TTS checks.
    "tts_status",
    "tts_generate",
    "tts_audio",
    "tts_download",
}


_TOUR_PREVIEW_ROLES = {"user", "editor", "admin"}
_TOUR_PREVIEW_INTERNAL_ENDPOINTS = {
    "login", "logout", "setup", "static",
    "onboarding_setup", "onboarding_intro",
    "tour_start", "tour_step", "tour_finish", "tour_role", "tour_presenter",
    "force_change_password", "session_conflict", "session_conflict_force",
}


def _tour_real_role_for_user(user):
    """Return the effective base role used for guided-tour previews."""
    role = user["role"] if user else "user"
    if role in ("admin", "owner"):
        return "admin"
    if role == "editor":
        return "editor"
    return "user"


def _tour_preview_user_for_request(user):
    """Return a temporary read-only role lens for the active tour target."""
    if not user or request.method != "GET":
        return user
    if not session.get("guided_tour_active"):
        return user

    preview_role = (session.get("guided_tour_role") or "").strip().lower()
    real_role = _tour_real_role_for_user(user)
    if preview_role not in _TOUR_PREVIEW_ROLES or preview_role == real_role:
        return user
    if request.endpoint in _TOUR_PREVIEW_INTERNAL_ENDPOINTS:
        return user

    preview_path = session.get("guided_tour_preview_path")
    if not preview_path or request.path != preview_path:
        return user

    preview_user = dict(user)
    preview_user["role"] = preview_role
    preview_user["tour_real_role"] = real_role
    preview_user["tour_preview_role"] = preview_role
    return preview_user


def _denied_redirect_target():
    """Return where to send a user who was refused a page.

    During a guided tour this is the page the tour is currently showing, so
    that wandering off the tour returns to the step instead of dropping the
    visitor on the home page and silently ending the walkthrough.
    """
    if session.get("guided_tour_active"):
        preview_path = session.get("guided_tour_preview_path")
        if preview_path and preview_path != request.path:
            return preview_path
    return url_for("home")


def _tour_write_is_forbidden():
    """Return True for a state-changing request made during a role preview.

    The preview shows an admin page to someone who is not an admin.  Sending
    their POST onwards to a redirect would look like it might have worked, so
    refuse it outright instead.
    """
    return bool(session.get("guided_tour_active")) and request.method != "GET"


def _public_mode_allows_request():
    """Return True when public mode may bypass login for this endpoint."""
    if not is_public_mode_active():
        return False
    endpoint = request.endpoint or ""
    return endpoint in _PUBLIC_MODE_ENDPOINTS


def get_current_user():
    """Return the currently logged-in user row, or None if not authenticated.

    The result is cached on Flask's ``g`` object for the duration of the
    current request so that multiple callers (before_request hook, auth
    decorators, inject_globals, route handlers) do not each issue a separate
    ``SELECT`` against the ``users`` table.

    Transient :class:`sqlite3.OperationalError` (e.g. ``disk I/O error``
    from a Hetzner-style network-volume stall, or ``database is locked``
    from a long-running write that exhausts the retry budget in
    :func:`db.get_user_by_id`) is **caught** here and turned into
    "anonymous" rather than allowed to propagate as a 500.  This is
    deliberate: the alternative is that every request -- including
    requests from logged-out visitors who don't actually need a user
    row -- hard-fails for the duration of the storage blip, *and* the
    custom 500 error page itself fails to render because
    :func:`app.inject_globals` also calls into this function.  Degrading
    to "anonymous" briefly is far less user-hostile, and the underlying
    error is still logged with the request ID for operator triage.
    """
    # The result is cached on ``g`` because before_request_hook, the auth
    # decorators and inject_globals each ask for the current user, and without
    # the cache that is three identical reads on every request.
    try:
        if hasattr(g, "_current_user"):
            return g._current_user
    except RuntimeError:
        pass  # Outside a request context (CLI tools, bare tests): skip cache

    uid = session.get("user_id")
    try:
        user = db.get_user_by_id(uid) if uid else None
    except sqlite3.OperationalError as exc:
        # The underlying read is already wrapped by ``retry_on_busy`` with
        # a small exponential backoff (see :mod:`db._connection`).  If we
        # *still* reach this branch, the DB has been unreachable long
        # enough that retrying further in the request hot-path would only
        # make the stall more user-visible.  Treat the visitor as logged
        # out for the duration of this request so the page (or the error
        # template) can still render.
        try:
            request_id = getattr(g, "_request_id", None)
        except RuntimeError:
            request_id = None
        try:
            get_logger().warning(
                "get_current_user: transient DB failure, degrading to anonymous "
                "(request_id=%s): %s",
                request_id, exc,
            )
        except Exception:
            pass
        user = None
    try:
        try:
            g._current_real_user = user
        except RuntimeError:
            pass
        user = _tour_preview_user_for_request(user)
        g._current_user = user
    except RuntimeError:
        pass  # Outside a request context: can't store in g
    return user


def login_required(f):
    """Decorator: redirect to login if the request has no valid authenticated session.

    Public mode is intentionally narrow: anonymous visitors may only bypass
    login for explicit read-only wiki/public helper endpoints.  Account,
    admin, private API, chat, profile, export, and mutation routes still
    redirect to login even while public mode is active.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Let public mode through, otherwise send anonymous callers to login."""
        if "user_id" not in session:
            if _public_mode_allows_request():
                return f(*args, **kwargs)
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            # Only pass ?next= when the destination is not the root so the
            # login URL doesn't permanently show "?next=/" after every visit.
            if next_url == "/":
                return redirect(url_for("login"))
            return redirect(url_for("login", next=next_url))
        user = get_current_user()  # populates g._current_user cache
        if not user:
            session.clear()
            if _public_mode_allows_request():
                return f(*args, **kwargs)
            from helpers._translations import t
            flash(t("auth.error.account_not_found"), "error")
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            if next_url == "/":
                return redirect(url_for("login"))
            return redirect(url_for("login", next=next_url))
        if user["suspended"]:
            # Check if timed suspension has expired
            if db.check_suspension_expired(user["id"]):
                # Suspension expired: refresh the cached user and allow access
                try:
                    if hasattr(g, "_current_user"):
                        del g._current_user
                except RuntimeError:
                    pass
                return f(*args, **kwargs)
            return redirect(url_for("account_suspended"))
        return f(*args, **kwargs)
    return wrapper


def editor_required(f):
    """Decorator: allow only editors and admins; redirect others to home."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Resolve the caller and reject anyone below editor."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if user["role"] not in ("editor", "admin", "owner"):
            from helpers._translations import t
            flash(t("auth.error.no_permission"), "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Decorator: allow only admins; redirect others to home (or return 403 JSON for API requests)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Resolve the caller and reject anyone below admin, as JSON for API requests."""
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if user["role"] not in ("admin", "owner"):
            if request.is_json or request.content_type == "application/json":
                from helpers._translations import t
                return jsonify({"error": t("auth.error.admin_access_required")}), 403
            from helpers._translations import t
            if _tour_write_is_forbidden():
                return abort(403)
            flash(t("auth.error.admin_access_required"), "error")
            return redirect(_denied_redirect_target())
        return f(*args, **kwargs)
    return wrapper


def editor_has_category_access(user, category_id):
    """Return True if *user* may edit content in the given *category_id*.

    Admins always have access.  Editors with unrestricted access also always
    have access.  Restricted editors only have access to their explicitly
    allowed categories; uncategorised pages (category_id=None) are not
    accessible to restricted editors.

    This function now uses the new custom permission system for category write access.
    For backward compatibility, it falls back to the old editor_category_access system
    if no custom permissions are set.
    """
    if not user:
        return False

    if user["role"] in ("admin", "owner"):
        return True

    # Custom roles/permissions are required for editing.
    # Note: custom roles based on 'editor' will have user['role'] == 'editor'.
    if user["role"] != "editor":
        return False

    # Use new permission system
    return db.has_category_write_access(user, category_id)


def user_can_view_category(user, category_id):
    """Return True if *user* can view pages in the given *category_id*.

    Admins always have access. Regular users and editors check their
    custom permissions for read access. Editors may always view any category
    they are allowed to write to.

    When *user* is ``None`` and public mode is active, all categories are
    visible (public visitors have no per-user restrictions).
    """
    if not user:
        return is_public_mode_active()

    if user["role"] in ("admin", "owner"):
        return True

    # Use new permission system
    return db.has_category_read_access(user, category_id)


def user_can_view_page(user, page):
    """Return True if *user* can view the given *page*.

    Takes into account:
    - Pending deletion (requires page.delete outside administrator roles)
    - Deindexed pages (only editors/admins can see them by default)
    - Category read access restrictions
    - Custom permissions

    When *user* is ``None`` and public mode is active, non-deindexed,
    non-pending-deletion pages with unrestricted categories are visible.
    """
    if not user:
        if not is_public_mode_active():
            return False
        builder_json = (page["builder_json"] if "builder_json" in page.keys()
                        else page["has_builder"] if "has_builder" in page.keys() else False)
        builder_public = page["builder_public"] if "builder_public" in page.keys() else False
        if builder_json and (
            not builder_public or getattr(config, "FORBID_PUBLIC_BUILDER_PAGES", False)
        ):
            return False
        # Public visitors can see non-pending, non-deindexed pages
        pending_deletion = page["pending_deletion"] if "pending_deletion" in page.keys() else False
        if pending_deletion:
            return False
        is_deindexed = page["is_deindexed"] if "is_deindexed" in page.keys() else False
        if is_deindexed:
            return False
        return True

    # Admins can view all pages
    if user["role"] in ("admin", "owner"):
        return True

    # Every non-admin page view stays inside the category boundary, including
    # deletion review. A page-level capability cannot grant another category.
    category_id = page["category_id"] if "category_id" in page.keys() else None
    if not user_can_view_category(user, category_id):
        return False

    # Non-admins need delete capability to see pages pending deletion.
    # This keeps read-only users from seeing them while allowing privileged
    # editors/custom roles to review pending items.
    pending_deletion = page["pending_deletion"] if "pending_deletion" in page.keys() else False
    if pending_deletion:
        return db.has_permission(user, "page.delete")

    # Check if page is deindexed (handle both dict and sqlite3.Row)
    is_deindexed = page["is_deindexed"] if "is_deindexed" in page.keys() else False
    if is_deindexed:
        # Only users with page.view_deindexed permission can see them
        return db.has_permission(user, "page.view_deindexed")

    return True


def filter_visible_navigation(categories, uncategorized, user):
    """Return sidebar categories/pages filtered through the current visibility policy.

    When *user* is ``None`` and public mode is active, the sidebar shows all
    non-deindexed, non-pending-deletion pages (public read-only view).
    """
    if not user and not is_public_mode_active():
        return {"categories": [], "uncategorized": []}

    is_admin = user["role"] in ("admin", "owner") if user else False

    def visit(nodes):
        """Recursively keep only visible categories and pages."""
        visible_nodes = []
        for node in nodes:
            filtered_children = visit(node.get("children", []))
            filtered_pages = [page for page in node.get("pages", []) if user_can_view_page(user, page)]
            if user_can_view_category(user, node["id"]):
                # For non-admins, hide categories that have no visible
                # pages and no visible child categories (all pages may
                # be pending deletion).
                if not is_admin and not filtered_pages and not filtered_children and not node.get("page_count", 0):
                    continue
                visible_node = dict(node)
                visible_node["children"] = filtered_children
                visible_node["pages"] = filtered_pages
                visible_nodes.append(visible_node)
            else:
                # Keep accessible descendants navigable without leaking the
                # blocked parent category's name in the sidebar.
                visible_nodes.extend(filtered_children)
        return visible_nodes

    return {
        "categories": visit(categories or []),
        "uncategorized": [page for page in (uncategorized or []) if user_can_view_page(user, page)],
    }
