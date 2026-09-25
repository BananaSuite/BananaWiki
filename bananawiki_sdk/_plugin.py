"""Plugin registration class.

Each plugin creates a :class:`Plugin` instance in its ``__init__.py``::

    from bananawiki_sdk import Plugin

    plugin = Plugin("my_plugin")

    @plugin.on_load
    def setup(app):
        ...
"""

from bananawiki_sdk._hooks import hook as _hook_decorator


class Plugin:
    """Central registration object for a BananaWiki plugin."""

    def __init__(self, plugin_id, extends=None):
        self.plugin_id = plugin_id
        self.extends = extends or []
        self._on_enable_fn = None
        self._on_disable_fn = None
        self._on_load_fn = None
        self._permissions = []
        self._admin_menu_items = []

    # -- Lifecycle decorators

    def on_enable(self, fn):
        """Decorator: called when the admin enables this plugin."""
        self._on_enable_fn = fn
        return fn

    def on_disable(self, fn):
        """Decorator: called when the admin disables this plugin."""
        self._on_disable_fn = fn
        return fn

    def on_load(self, fn):
        """Decorator: called when the Flask app loads this plugin.

        The decorated function receives the Flask ``app`` instance.
        """
        self._on_load_fn = fn
        return fn

    # -- Registration helpers

    def register_permission(self, key, label, description,
                            default_editor=False, default_user=False):
        """Declare a custom permission that admins can assign to users."""
        self._permissions.append({
            "key": key,
            "label": label,
            "description": description,
            "default_editor": default_editor,
            "default_user": default_user,
        })

    def add_admin_menu_item(self, label, endpoint, icon=""):
        """Add an entry to the admin sidebar menu."""
        self._admin_menu_items.append({
            "label": label,
            "endpoint": endpoint,
            "icon": icon,
        })

    def hook(self, hook_name):
        """Convenience wrapper around the module-level ``@hook`` decorator
        that also tags the function with this plugin's ID.

        The tag is bookkeeping for the loader, not a security label: when
        the plugin is loaded, the loader re-tags everything it registered
        with the id from its ``plugin.json``.
        """
        def decorator(fn):
            fn._bw_plugin_id = self.plugin_id
            return _hook_decorator(hook_name)(fn)
        return decorator
