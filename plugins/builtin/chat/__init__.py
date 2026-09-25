"""Chats and Groups: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("chat")


@plugin.on_load
def setup(app):
    """Hook into the application when the plugin is loaded."""
    pass
