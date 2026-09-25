"""Drafts: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("drafts")


@plugin.on_load
def setup(app):
    """Autosave endpoints ship with the core editor, so this load hook is only a placeholder."""
    pass
