"""User Data Export: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("user_data_export")


@plugin.on_load
def setup(app):
    """A no-op hook, since the ZIP export endpoint belongs to the core account routes."""
    pass
