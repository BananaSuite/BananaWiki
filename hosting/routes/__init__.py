"""Route registration for the hosting portal."""

from .auth import register_hosting_auth_routes
from .dashboard import register_hosting_dashboard_routes
from .oauth import register_hosting_oauth_routes
from .banners import register_hosting_banner_routes
from .domains import register_domain_routes
from .mfa import register_mfa_routes
from .api import register_hosting_api_routes


def register_hosting_routes(app):
    """Register all hosting portal routes."""
    register_hosting_auth_routes(app)
    register_hosting_dashboard_routes(app)
    register_hosting_oauth_routes(app)
    register_hosting_banner_routes(app)
    register_domain_routes(app)
    register_mfa_routes(app)
    register_hosting_api_routes(app)
