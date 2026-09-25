"""Deletion Slowdown: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("deletion_slowdown")


@plugin.on_load
def setup(app):
    """The 48 hour grace period and the pending deletions screen are core routes, so this hook just marks the plugin loaded."""
    pass
