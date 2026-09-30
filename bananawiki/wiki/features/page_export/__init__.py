"""Page export: PDF and Markdown downloads of pages, and bulk Markdown import/export for administrators.

The single-page downloads honour the ``pdf_export_enabled`` and
``markdown_export_enabled`` site settings and the ``page.export_pdf``
permission; the bulk tools are for administrators only.
"""

from ... import auth
from ...registry import Feature, NavItem
from .routes import bp
from .slots import editor_import, header_actions

FEATURE = Feature(
    id="page_export",
    name="feature.page_export.name",
    description="feature.page_export.description",
    toggle="always",
    blueprints=[bp],
    nav=[NavItem("page_export.nav", "page_export.bulk_page", icon="download", area="admin", order=95,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    slots={"page.header_actions": header_actions, "editor.below_form": editor_import},
)
