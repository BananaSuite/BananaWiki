"""Custom Pages: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("custom_pages")


@plugin.on_load
def setup(app):
    """Admin-defined paths are resolved by the core custom page routes, so this hook adds none of its own."""
    pass
