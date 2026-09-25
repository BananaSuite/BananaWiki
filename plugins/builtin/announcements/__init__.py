"""Announcements: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("announcements")


@plugin.on_load
def setup(app):
    """Banner storage and the admin editor are built into the core app, so this hook registers nothing."""
    pass
