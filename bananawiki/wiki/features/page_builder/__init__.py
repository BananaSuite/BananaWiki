"""Page builder: a visual block editor for wiki pages (switched on in the site settings)."""

from ... import auth
from ...registry import Feature, NavItem
from . import custom  # noqa: F401 - registers the custom page editor on the blueprint
from .routes import bp, header_action
from .service import render_page

FEATURE = Feature(
    id="page_builder",
    name="feature.page_builder.name",
    description="feature.page_builder.description",
    toggle="setting",
    setting="page_builder_enabled",
    default_enabled=False,
    easy_wiki=False,
    blueprints=[bp],
    nav=[NavItem("page_builder.nav", "page_builder.admin_settings", icon="layout", area="admin", order=65,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    interceptors={"page.render": render_page},
    slots={"page.header_actions": header_action},
)
