"""Page Governance: first-party BananaWiki plugin.

Bundles three editorial-governance features under a single toggle:

* Page reservations: check-out / cooldown / quota system.
* Page protection: editor-locked pages with a 72-hour admin-unlock waiting
  period.
* Contribution approval: proposed edits from read-only users that require an
  admin approval before being applied.

When the plugin is disabled all three feature settings are reset to safe
defaults via ``_PLUGIN_SETTINGS_TO_DISABLE`` in ``routes/plugins.py`` so no
governance toggle can stay silently active behind a disabled plugin.
"""

from bananawiki_sdk import Plugin

plugin = Plugin("page_governance")


@plugin.on_load
def setup(app):
    """Register routes and hooks when the plugin is loaded.

    The page reservation, protection, and contribution-approval routes are
    declared in :mod:`routes.wiki` and :mod:`routes.admin` and are gated at
    the path-matcher level (see ``_BUILTIN_PLUGIN_PATH_MATCHERS`` in
    ``app.py``).  No per-plugin route registration is required here.
    """
    pass
