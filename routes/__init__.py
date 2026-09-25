"""
BananaWiki: Route registration package.

Each sub-module groups related route handlers. Call :func:`register_all_routes`
to register every route group on the Flask application instance.

:func:`register_core_routes` registers only the core routes required for a
functional wiki.  Feature routes (chat, groups, etc.) are loaded by the
plugin system.
"""

from routes.auth import register_auth_routes
from routes.wiki import register_wiki_routes
from routes.users import register_user_routes
from routes.admin import register_admin_routes
from routes.chat import register_chat_routes
from routes.groups import register_group_routes
from routes.api import register_api_routes
from routes.uploads import register_upload_routes
from routes.plugins import register_plugin_routes
from routes.onboarding import register_onboarding_routes
from routes.errors import register_error_handlers
from routes.kanban import register_kanban_routes, user_has_kanban_sidebar_access
from routes.temporary import register_temporary_routes
from routes.custom_pages import register_custom_pages_routes
from routes.deletion_slowdown import register_deletion_slowdown_routes
from routes.canvas import register_canvas_routes, user_has_canvas_sidebar_access
from routes.assessments import register_assessment_routes
from routes.tts import register_tts_routes
from routes.bulk import register_bulk_routes
from routes.api_service import register_api_service_routes
from routes.leaderboard import register_leaderboard_routes
from routes.platform_oauth import register_platform_oauth_routes
from routes.page_builder import register_page_builder_routes
from routes.federation import register_federation_routes


def register_core_routes(app):
    """Register core route groups that are always available.

    Feature routes (chat, groups) are included here because they ship with
    the BananaWiki monolith.  The plugin system manages external additions
    and tracks first-party feature state for the admin UI.
    """
    register_auth_routes(app)
    register_wiki_routes(app)
    register_page_builder_routes(app)
    register_federation_routes(app)
    register_user_routes(app)
    register_admin_routes(app)
    register_chat_routes(app)
    register_group_routes(app)
    register_api_routes(app)
    register_upload_routes(app)
    register_plugin_routes(app)
    register_onboarding_routes(app)
    register_kanban_routes(app)
    register_temporary_routes(app)
    register_custom_pages_routes(app)
    register_deletion_slowdown_routes(app)
    register_canvas_routes(app)
    register_assessment_routes(app)
    register_tts_routes(app)
    register_bulk_routes(app)
    register_leaderboard_routes(app)
    register_error_handlers(app)
    # API Service routes: always registered; gated by _BUILTIN_PLUGIN_PATH_MATCHERS
    register_api_service_routes(app)
    # Platform OAuth SSO routes, only active when environment says so
    register_platform_oauth_routes(app)


def register_all_routes(app):
    """Register all route groups and error handlers on *app*.

    .. deprecated::
        Use :func:`register_core_routes` + the plugin loader instead.
        Retained for backward compatibility.
    """
    register_core_routes(app)
