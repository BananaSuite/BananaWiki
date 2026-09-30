"""Custom pages: administrator-made pages at arbitrary unreserved paths (1.4 built-in plugin)."""

from ...registry import Feature, NavItem
from .routes import bp, register_catch_all
from .service import can_manage

FEATURE = Feature(
    id="custom_pages",
    name="feature.custom_pages.name",
    description="feature.custom_pages.description",
    toggle="plugin",
    default_enabled=False,
    easy_wiki=False,
    blueprints=[bp],
    nav=[NavItem("custom_pages.nav", "custom_pages.admin_list", icon="file", area="admin", order=60,
                 visible=lambda user: can_manage(user))],
    init_app=register_catch_all,
)
