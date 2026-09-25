"""
BananaWiki Plugin SDK
=====================

Public API for first-party and external plugin authors.

Usage::

    from bananawiki_sdk import Plugin, hook, template_slot, db_query, db_execute

See ``docs/plugins/api-reference.md`` for the full reference.
"""

# Version ---
__version__ = "1.0.0"
API_VERSION = "1.0"

# Exceptions ---
from bananawiki_sdk._exceptions import (       # noqa: F401,E402
    PluginError,
    PluginConfigError,
    PluginAPIVersionError,
)

# Plugin class ---
from bananawiki_sdk._plugin import Plugin      # noqa: F401,E402

# Hooks ---
from bananawiki_sdk._hooks import (            # noqa: F401,E402
    hook,
    emit_hook,
)

# Template slots ---
from bananawiki_sdk._slots import (            # noqa: F401,E402
    template_slot,
    render_slot,
)

# Database helpers ---
from bananawiki_sdk._database import (         # noqa: F401,E402
    db_query,
    db_execute,
)

# Re-exports from BananaWiki core ---
from bananawiki_sdk._re_exports import (       # noqa: F401,E402
    get_current_user,
    has_permission,
    is_plugin_enabled,
    get_setting,
    flash,
    redirect,
    url_for,
    render_template,
    rate_limit,
    login_required,
    admin_required,
    editor_required,
    log_action,
    encrypt_value,
    decrypt_value,
)
