"""Difficulty Tags: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("difficulty_tags")


@plugin.on_load
def setup(app):
    """Difficulty levels and custom tags are edited inside the core page editor, leaving nothing to register."""
    pass
