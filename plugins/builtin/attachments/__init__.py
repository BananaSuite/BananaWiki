"""Attachments: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("attachments")


@plugin.on_load
def setup(app):
    """Upload and download of page attachments is handled by the core upload routes, so nothing is registered."""
    pass
