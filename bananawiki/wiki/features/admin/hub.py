"""The administration menu: every admin page, grouped, in the sidebar of each admin page.

Features add their pages with ``NavItem(area="admin")``; this module only decides which group each one is
listed under.
"""

from __future__ import annotations

from typing import Any

from .blueprint import bp

OVERVIEW = "admin:admin.dashboard"
# Each group by feature; a feature whose pages belong to different groups names them one by one.
PEOPLE = {"admin", "access_requests", "users", "user_profiles", "badges", "temporary_accounts"}
CONTENT = {"announcements", "bulk_manage", "custom_pages", "contributions", "deletion_slowdown", "page_export",
           "page_governance"}
FEATURES = {"plugin_manager", "chat", "assessments", "tts", "api_service", "federation"}
APPEARANCE = {"bm_mode"}
SYSTEM = {"attention", "audit"}
ITEMS = {
    "site_admin:site_admin.general": "system",
    "site_admin:site_admin.appearance": "appearance",
    "site_admin:site_admin.languages_page": "appearance",
    "site_admin:site_admin.docs_page": "content",
    "site_admin:site_admin.migration_page": "system",
    "site_admin:site_admin.server_page": "system",
}
_BY_FEATURE = {**dict.fromkeys(PEOPLE, "people"), **dict.fromkeys(CONTENT, "content"),
               **dict.fromkeys(FEATURES, "features"), **dict.fromkeys(APPEARANCE, "appearance"),
               **dict.fromkeys(SYSTEM, "system")}
# The order the groups are listed in; features the lists above do not know about land in "extensions".
GROUPS = ("people", "content", "features", "appearance", "system", "extensions")


def group_of(item: dict[str, Any]) -> str:
    if item["id"] in ITEMS:
        return ITEMS[item["id"]]
    return _BY_FEATURE.get(item["id"].split(":")[0], "extensions")


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
