"""Page history: earlier versions, diffs, revert and attribution (1.4 plugin ``page_history``)."""

from ...registry import Feature, feature_blueprint
from ..pages.blueprint import bp as pages_bp
from . import routes

# Templates and translations only; the routes live on the pages blueprint.
bp = feature_blueprint("page_history", "page_history", __name__, template_folder="templates")
routes.attach(pages_bp)

FEATURE = Feature(
    id="page_history",
    name="feature.page_history.name",
    description="feature.page_history.description",
    toggle="plugin",
    blueprints=[bp],
    order=20,
)
