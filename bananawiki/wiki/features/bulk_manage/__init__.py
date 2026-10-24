"""Administrators delete many categories, pages, canvases or kanban boards at once."""

from ... import auth
from ...registry import Feature, NavItem
from .routes import bp

FEATURE = Feature(
    id="bulk_manage",
    name="bulk_manage.feature.name",
    description="bulk_manage.feature.description",
    toggle="always",
    blueprints=[bp],
    nav=[NavItem("bulk_manage.nav", "bulk_manage.index", icon="trash", area="admin", bm_hide="admin.short_menu", order=85,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    order=90,
)
