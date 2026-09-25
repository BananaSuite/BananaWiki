"""Register the focused administration route groups."""

from routes.users import build_user_export_zip as build_user_export_zip
from .admin_accounts import register_admin_accounts_routes
from .admin_roles import register_admin_roles_routes
from .admin_sessions import register_admin_sessions_routes
from .admin_settings import register_admin_settings_routes
from .admin_appearance import register_admin_appearance_routes
from .admin_localization import register_admin_localization_routes
from .admin_migration import register_admin_migration_routes
from .admin_announcements import register_admin_announcements_routes
from .admin_badges import register_admin_badges_routes
from .admin_checkouts import register_admin_checkouts_routes
from .admin_contributions import register_admin_contributions_routes


def register_admin_routes(app):
    """Register administration endpoints and their shared logout scheduler."""
    schedule_auto_logout = register_admin_sessions_routes(app)
    register_admin_accounts_routes(app)
    register_admin_roles_routes(app)
    register_admin_settings_routes(app, schedule_auto_logout)
    register_admin_appearance_routes(app)
    register_admin_localization_routes(app)
    register_admin_migration_routes(app)
    register_admin_announcements_routes(app)
    register_admin_badges_routes(app)
    register_admin_checkouts_routes(app)
    register_admin_contributions_routes(app)
