"""Plugin manager: switch built-in features, install third-party plugins, order the sidebar apps.

See ``PLUGINS.md`` in this folder for how third-party plugins are written
and loaded (:mod:`bananawiki.wiki.plugins_external`).
"""

from ... import attention
from ...registry import AttentionSource, Feature, NavItem
from .routes import _is_admin, bp, pending_restarts

FEATURE = Feature(
    id="plugin_manager",
    name="feature.plugin_manager.name",
    description="feature.plugin_manager.description",
    toggle="always",
    blueprints=[bp],
    nav=[NavItem("plugin_manager.nav", "plugin_manager.index", icon="puzzle", area="admin", order=70,
                 visible=_is_admin, badge=attention.badge("plugin_manager.restarts"))],
    attention=[AttentionSource("plugin_manager.restarts", "attention.source.plugin_manager.restarts",
                               "plugin_manager.index", pending_restarts, order=90)],
    order=6,
)
