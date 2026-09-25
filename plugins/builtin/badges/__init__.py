"""Badges: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("badges")


@plugin.on_load
def setup(app):
    """Badge definitions and auto-trigger checks run in core code; loading the plugin adds no routes."""
    pass
