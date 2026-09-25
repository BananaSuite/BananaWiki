"""Register the Wiki page, category, and contribution features."""

from .wiki_search import register_wiki_search_routes
from .wiki_pages import register_wiki_pages_routes
from .wiki_governance import register_wiki_governance_routes
from .wiki_history import register_wiki_history_routes
from .wiki_editing import register_wiki_editing_routes
from .wiki_categories import register_wiki_categories_routes
from .wiki_contributions import register_wiki_contributions_routes
from .wiki_presence import register_wiki_presence_routes


def register_wiki_routes(app):
    """Attach the page, category, and contribution endpoints."""
    register_wiki_search_routes(app)
    register_wiki_pages_routes(app)
    register_wiki_governance_routes(app)
    register_wiki_history_routes(app)
    register_wiki_editing_routes(app)
    register_wiki_categories_routes(app)
    register_wiki_contributions_routes(app)
    register_wiki_presence_routes(app)
