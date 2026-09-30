"""Canvas: visual boards of notes, shapes, images, videos, code and wiki-page links.

See :mod:`.access` for who may see what, :mod:`.service` for storage,
concurrency and retention, and :mod:`.model` for the document format.
Canvases are embedded in wiki pages with ``[[canvas slug="…"]]``; the embed
script is added to every page through the ``page.scripts`` slot.
"""

from flask import render_template
from markupsafe import Markup

from ...registry import Feature, Job, NavItem
from . import access, service
from .routes import bp


def _embed_scripts() -> Markup:
    return Markup(render_template("canvas/_embed_scripts.html"))


def _prune() -> None:
    service.prune()


FEATURE = Feature(
    id="canvas",
    name="feature.canvas.name",
    description="feature.canvas.description",
    toggle="plugin",
    default_enabled=True,
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("canvas.nav", "canvas.index", icon="shapes", area="apps", order=40,
                visible=access.can_open_canvas),
        NavItem("canvas.admin.nav", "canvas.admin_settings", icon="shapes", area="admin", order=62,
                visible=access.is_admin),
    ],
    jobs=[Job("canvas.prune", 3600, _prune)],
    events={
        "page.renamed": [service.on_page_renamed],
        "page.updated": [service.on_page_updated],
        "page.deleted": [service.on_page_deleted],
        "page.restored": [service.on_page_restored],
    },
    slots={"page.scripts": _embed_scripts},
)
