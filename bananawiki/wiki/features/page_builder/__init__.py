"""Page builder: a visual block editor for wiki pages (switched on under Admin → Plugins).

Its switch is the site setting ``page_builder_enabled``; a host can forbid the
builder with ``BW_FORBID_PAGE_BUILDER``, which keeps it off whatever the switch
says.
"""

from ...registry import Feature
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
    interceptors={"page.render": render_page},
    slots={"page.header_actions": header_action},
)
