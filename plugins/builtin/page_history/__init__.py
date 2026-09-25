"""Page History: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("page_history")


@plugin.on_load
def setup(app):
    """Revision browsing, diffs and reverts come from the core wiki history routes."""
    pass
