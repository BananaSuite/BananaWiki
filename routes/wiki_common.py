"""Shared page access, reservations, and protection checks."""

from datetime import datetime, timedelta, timezone
from flask import (
    redirect, url_for, flash, abort, g,
)
import db
from helpers import (
    editor_has_category_access, user_can_view_page, _safe_referrer, t,
)


_EDITOR_ROLES = ("editor", "admin", "owner")


_ADMIN_ROLES = ("admin", "owner")


_MAX_PAGE_CONTENT_LENGTH = 1_000_000  # 1 MB


_SEARCH_FIELDS = {"title", "content", "category", "slug", "type"}


_SEARCH_SCOPES = {"all", "title", "content", "category"}


_SEARCH_TYPES = {"all", "page", "category"}


def _page_governance_plugin_enabled():
    """Return True when the bundling Page Governance plugin is enabled."""
    enabled_plugins = getattr(g, "enabled_plugins", None) or {}
    return bool(enabled_plugins.get("page_governance"))


def _page_reservations_enabled():
    """Return True when the page reservation feature is enabled.

    Both the bundling :mod:`page_governance` plugin **and** the per-feature
    ``page_reservations_enabled`` setting must be on.
    """
    if not _page_governance_plugin_enabled():
        return False
    return db.reservations_enabled()


def _page_protection_enabled():
    """Return True when page protection is enabled.

    Both the bundling :mod:`page_governance` plugin **and** the per-feature
    ``page_protection_enabled`` setting must be on.
    """
    if not _page_governance_plugin_enabled():
        return False
    settings = db.get_site_settings() or {}
    return bool(settings.get("page_protection_enabled"))


def _contribution_approval_enabled():
    """Return True when contribution approval is enabled.

    Both the bundling :mod:`page_governance` plugin **and** the per-feature
    ``contribution_approval_enabled`` setting must be on.
    """
    if not _page_governance_plugin_enabled():
        return False
    settings = db.get_site_settings() or {}
    return bool(settings.get("contribution_approval_enabled"))


def _get_page_protection_context(page, user):
    """Return protection metadata for *page* as seen by *user*."""
    if not page or not user or user["role"] not in _EDITOR_ROLES:
        return None
    if not _page_protection_enabled():
        return {"enabled": False, "is_protected": False, "edit_locked": False}
    protected_by = page["protected_by"]
    if not protected_by:
        return {"enabled": True, "is_protected": False, "edit_locked": False}
    protected_user = db.get_user_by_id(protected_by)
    protected_by_username = protected_user["username"] if protected_user else "[deleted user]"
    is_controller = protected_by == user["id"]
    requested_at = page["protection_unlock_requested_at"]
    requested_by = page["protection_unlock_requested_by"]
    requested_by_user = db.get_user_by_id(requested_by) if requested_by else None
    requested_by_username = requested_by_user["username"] if requested_by_user else None
    unlock_ready_at = None
    unlock_wait_seconds = None
    unlock_ready = False
    if requested_at:
        try:
            requested_datetime = datetime.fromisoformat(requested_at)
            if requested_datetime.tzinfo is None:
                requested_datetime = requested_datetime.replace(tzinfo=timezone.utc)
            unlock_ready_datetime = requested_datetime + timedelta(seconds=db.PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS)
            unlock_ready_at = unlock_ready_datetime.isoformat()
            unlock_wait_seconds = max(0, int((unlock_ready_datetime - datetime.now(timezone.utc)).total_seconds()))
            unlock_ready = unlock_wait_seconds == 0
        except ValueError:
            pass
    return {
        "enabled": True,
        "is_protected": True,
        "protected_by": protected_by,
        "protected_by_username": protected_by_username,
        "is_controller": is_controller,
        "edit_locked": not is_controller,
        "unlock_requested_at": requested_at,
        "unlock_requested_by": requested_by,
        "unlock_requested_by_username": requested_by_username,
        "unlock_ready_at": unlock_ready_at,
        "unlock_wait_seconds": unlock_wait_seconds,
        "unlock_ready": unlock_ready,
    }


def _get_reservation_context(page, user):
    """Return reservation metadata for *page* as seen by *user*."""
    if not page or not user or user["role"] not in _EDITOR_ROLES:
        return None
    if not _page_reservations_enabled():
        return None
    status = db.get_page_reservation_status(page["id"], user["id"])
    reserved_by_other = status["is_reserved"] and status["reserved_by"] != user["id"]
    admin_override = reserved_by_other and user["role"] in _ADMIN_ROLES
    return {
        "status": status,
        "reserved_by_other": reserved_by_other,
        "edit_locked": reserved_by_other and not admin_override,
        "admin_override": admin_override,
    }


def _reservation_block_message(status, action_text):
    """Build the flash message shown when a reservation blocks an action."""
    reserved_by = (
        status["reserved_by_username"]
        or t("flash.action.another_editor_fallback")
    )
    return t(
        "flash.reservation_blocks_action",
        action=action_text,
        reserver=reserved_by,
    )


def _page_protection_block_message(protection_context, action_text):
    """Build the flash message shown when page protection blocks an action."""
    protector = (
        protection_context.get("protected_by_username")
        or t("flash.action.another_editor_fallback")
    )
    return t(
        "flash.page_protection_blocks_action",
        action=action_text,
        protector=protector,
    )


def _guard_destructive_page_edit(page, user, action_text):
    """Redirect away from a destructive edit action when reservations forbid it."""
    protection_context = _get_page_protection_context(page, user)
    if protection_context and protection_context["edit_locked"]:
        flash(_page_protection_block_message(protection_context, action_text), "error")
        return redirect(url_for("view_page", slug=page["slug"]))
    reservation_context = _get_reservation_context(page, user)
    if reservation_context and reservation_context["edit_locked"]:
        flash(_reservation_block_message(reservation_context["status"], action_text), "error")
        return redirect(url_for("view_page", slug=page["slug"]))
    return None


def _abort_if_page_hidden(page, user):
    """Abort when the current user cannot view *page* under the current policy."""
    if not user_can_view_page(user, page):
        abort(403)


def _guard_category_management_permission(user, permission_key, error_message):
    """Redirect when *user* lacks the current permission for a category action."""
    if db.has_permission(user, permission_key):
        return None
    flash(error_message, "error")
    return redirect(_safe_referrer() or url_for("home"))


def _build_reservable_page(page, category_label, user):
    """Return template-ready reservation data for a page list entry."""
    reservation_context = _get_reservation_context(page, user)
    return {
        "id": page["id"],
        "slug": page["slug"],
        "title": page["title"],
        "category_label": category_label,
        "reservation_status": reservation_context["status"] if reservation_context else None,
    }


def _iter_reservable_pages(category_nodes, uncategorized, user):
    """Flatten category trees into a filtered list of reservable pages."""
    # Accessible pages are collected first so their reservation statuses can
    # be fetched in two queries.  Asking _get_reservation_context() per page
    # turns a large wiki into hundreds of round trips.
    candidates = []  # list of (page_row, category_label)

    def visit(nodes, parent_label=""):
        """Recursively collect reservable pages from nested category nodes."""
        for node in nodes:
            category_label = f"{parent_label} / {node['name']}" if parent_label else node["name"]
            for page in node["pages"]:
                if editor_has_category_access(user, page["category_id"]):
                    candidates.append((page, category_label))
            visit(node["children"], category_label)

    visit(category_nodes)
    for page in uncategorized:
        if editor_has_category_access(user, page["category_id"]):
            candidates.append((page, "Uncategorized"))

    if not candidates:
        return []

    # Bulk-fetch all reservation/cooldown statuses in two SQL queries.
    page_ids = [page["id"] for page, _ in candidates]
    status_map = db.get_directory_reservation_statuses(user["id"], page_ids)

    return [
        {
            "id": page["id"],
            "slug": page["slug"],
            "title": page["title"],
            "category_label": category_label,
            "reservation_status": status_map.get(page["id"]),
        }
        for page, category_label in candidates
    ]


def _pdf_export_allowed(user):
    """Return True when the user may export pages as PDF."""
    if not user:
        return False
    settings = db.get_site_settings()
    if not settings or not settings.get("pdf_export_enabled"):
        return False
    return db.has_permission(user, "page.export_pdf")


def _user_can_edit_page(user, page):
    """Return True when the user can directly edit this page (no contribution needed).

    Checks permissions, category access, page protection, and reservations.
    Users with page.edit_all permission in the page's category can edit directly.
    """
    if not user:
        return False
    # Admins and owners always have direct edit access
    if user["role"] in _ADMIN_ROLES:
        return True
    # User must have page.edit_all permission
    if not db.has_permission(user, "page.edit_all"):
        return False
    # Check category write access for editors with restricted categories
    if user["role"] == "editor":
        cat_id = page["category_id"]
        if not db.has_category_write_access(user, cat_id):
            return False
    return True


def _user_can_propose_edit(user, page):
    """Return True when the user should use the contribution system for this page.

    This is True when:
    - The contribution approval system is enabled
    - The user does NOT have direct edit access to this page
    - The user has the contribution.propose permission
    - The user has read access to the page's category
    - The page is not pending deletion
    """
    if not user or not _contribution_approval_enabled():
        return False
    # If user can edit directly, they don't need the contribution system
    if _user_can_edit_page(user, page):
        return False
    # User must have the propose permission
    if not db.has_permission(user, "contribution.propose"):
        return False
    # User must have read access to the page's category
    cat_id = page["category_id"]
    if not db.has_category_read_access(user, cat_id):
        return False
    # Cannot propose edits for pages pending deletion
    if page["pending_deletion"]:
        return False
    return True
