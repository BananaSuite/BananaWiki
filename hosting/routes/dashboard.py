"""Register the focused hosting dashboard route groups."""

from .dashboard_instances import register_hosting_instances_routes
from .dashboard_features import register_hosting_features_routes
from .dashboard_instance_admin import register_hosting_instance_admin_routes
from .dashboard_accounts import register_hosting_accounts_routes
from .dashboard_archives import register_hosting_archives_routes
from .dashboard_collaboration import register_hosting_collaboration_routes
from .dashboard_settings import register_hosting_settings_routes


def register_hosting_dashboard_routes(app):
    """Register owner, administrator, collaboration, and recovery endpoints."""
    register_hosting_instances_routes(app)
    register_hosting_features_routes(app)
    register_hosting_instance_admin_routes(app)
    register_hosting_accounts_routes(app)
    register_hosting_archives_routes(app)
    register_hosting_collaboration_routes(app)
    register_hosting_settings_routes(app)
