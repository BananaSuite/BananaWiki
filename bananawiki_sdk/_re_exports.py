"""Re-exports from BananaWiki core.

These are thin wrappers so that plugin authors do not need to import from
Flask or BananaWiki internals directly.
"""

# Flask re-exports ---
from flask import flash, redirect, url_for, render_template  # noqa: F401

# BananaWiki helper re-exports ---
from helpers._auth import (                                   # noqa: F401
    get_current_user,
    login_required,
    editor_required,
    admin_required,
)
from helpers._rate_limiting import rate_limit                 # noqa: F401
from wiki_logger import log_action                            # noqa: F401
from helpers._crypto import encrypt_value, decrypt_value      # noqa: F401


def has_permission(user, key):
    """Check whether *user* has the given permission *key*."""
    import db
    return db.has_permission(user, key)


def is_plugin_enabled(plugin_id):
    """Return ``True`` if the plugin with *plugin_id* is enabled."""
    import db
    return db.is_plugin_enabled(plugin_id)


def get_setting(key):
    """Return a value from the ``site_settings`` table."""
    import db
    settings = db.get_site_settings()
    return settings[key] if key in settings.keys() else None
