"""API Service: built-in BananaWiki plugin.

Provides a unified REST API with token authentication, audit logging,
bulk operations, and programmable admin controls.  Routes are always
registered and gated via ``_BUILTIN_PLUGIN_PATH_MATCHERS`` in ``app.py``.
"""

from bananawiki_sdk import Plugin

plugin = Plugin("api_service")


@plugin.on_enable
def enable():
    """Set reasonable defaults when the plugin is enabled."""
    import db
    settings = db.get_site_settings() or {}
    if not settings.get("api_service_enabled"):
        db.update_site_settings(api_service_enabled=1)


@plugin.on_disable
def teardown(app=None):
    """Disable API-owned features when the plugin is disabled."""
    import db
    db.update_site_settings(api_service_enabled=0, banana_mode=0)
