"""Wiki pages and categories: reading, editing, navigation, search and images.

The shared page logic lives in :mod:`.service` and :mod:`.categories`; the
route modules below only add the web interface on top of it.
"""

from flask import render_template, request

from ... import auth, settings
from ...registry import Feature, Job
from . import (  # noqa: F401 - register routes
    api,
    category_routes,
    editing,
    navigation,
    presence,
    uploads,
    views,
)
from .access import can_create_somewhere
from .blueprint import bp


def _sidebar_navigation() -> str:
    """Search box and category tree for the sidebar (``sidebar.navigation`` slot)."""
    user = auth.current_user()
    if user is None and not settings.public_mode_active():
        return ""
    nav = navigation.tree(user)
    current_slug = (request.view_args or {}).get("slug") if request.blueprint == "pages" else None
    current = None
    if current_slug:
        from . import service

        current = service.get_by_slug(current_slug, with_content=False)
        if current is not None and not service.can_view(current, user):
            current = None
    return render_template(
        "pages/_sidebar.html", nav=nav, current=current, current_slug=current_slug,
        open_ids=navigation.branch_ids(nav, current["category_id"]) if current else set(),
        current_listed=bool(current) and current["id"] in navigation.page_ids(nav),
        can_create=can_create_somewhere(user),
        can_create_category=bool(user) and auth.has_role("editor", user)
        and auth.has_permission("category.create", user),
        reorder=api.may_reorder_pages(),
    )


FEATURE = Feature(
    id="pages",
    name="feature.pages.name",
    description="feature.pages.description",
    toggle="always",
    blueprints=[bp],
    jobs=[
        Job("pages.cleanup_uploads", 6 * 3600, uploads.cleanup_unused, initial_delay=600),
        Job("pages.prune_editing_sessions", 900, presence.prune),
    ],
    slots={"sidebar.navigation": _sidebar_navigation},
    order=10,
)
