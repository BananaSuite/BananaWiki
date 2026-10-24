"""Page builder: a visual block editor for wiki pages (switched on under Admin → Plugins).

Its switch is the site setting ``page_builder_enabled``; a host can forbid the
builder with ``BW_FORBID_PAGE_BUILDER``, which keeps it off whatever the switch
says.
"""

from flask import current_app

from ... import auth
from ...registry import Feature, NavItem
from . import custom  # noqa: F401 - registers the custom page editor on the blueprint
from .routes import bp, header_action
from .service import render_page


def _admin_link_visible(user) -> bool:
    return bool(user) and auth.is_admin(user) and not current_app.config["BW"].forbid_page_builder

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
                 visible=_admin_link_visible)],
    interceptors={"page.render": render_page},
    slots={"page.header_actions": header_action},
)
