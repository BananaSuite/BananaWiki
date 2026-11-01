"""The administration hub: every admin page, grouped, for the settings overview and the page header.

Features add their pages with ``NavItem(area="admin")``; this module only decides which group each one is
listed under.
"""

from __future__ import annotations

from typing import Any

from .blueprint import bp

OVERVIEW = "admin:admin.dashboard"
PEOPLE = {"admin", "users", "user_profiles", "badges", "temporary_accounts"}
SYSTEM = {"audit", "api_service", "federation"}
SYSTEM_ITEMS = {"site_admin:site_admin.migration_page", "site_admin:site_admin.server_page"}
WIKI = {"site_admin", "attention", "plugin_manager", "bm_mode"}
CONTENT = {"assessments", "announcements", "bulk_manage", "custom_pages", "chat", "canvas", "deletion_slowdown",
           "page_export", "page_governance", "page_builder", "tts", "contributions"}
# The order the groups are listed in; features the lists above do not know about land in "extensions".
GROUPS = ("people", "wiki", "content", "system", "extensions")


def group_of(item: dict[str, Any]) -> str:
    feature_id = item["id"].split(":")[0]
    if feature_id in PEOPLE:
        return "people"
    if item["id"] in SYSTEM_ITEMS or feature_id in SYSTEM:
        return "system"
    if feature_id in WIKI:
        return "wiki"
    if feature_id in CONTENT:
        return "content"
    return "extensions"


def grouped(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The admin menu entries as ``{"overview": item or None, "groups": [...], "current": ..., "current_group": ...}``.

    Every group is ``{"key", "items", "badge", "active"}``; empty groups are left out.
    """
    overview = None
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in GROUPS}
    for item in items:
        if item["id"] == OVERVIEW:
            overview = item
        else:
            buckets[group_of(item)].append(item)
    groups = [{"key": name, "items": found, "badge": sum(entry["badge"] for entry in found),
               "active": any(entry["active"] for entry in found)}
              for name, found in buckets.items() if found]
    current = next((entry for entry in items if entry["active"]), None)
    current_group = next((group for group in groups if group["active"]), None)
    return {"overview": overview, "groups": groups, "current": current, "current_group": current_group}


@bp.app_context_processor
def _inject() -> dict[str, Any]:
    return {"admin_grouped": grouped}
