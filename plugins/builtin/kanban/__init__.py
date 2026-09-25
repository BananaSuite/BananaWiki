"""Kanban Board: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("kanban")


@plugin.on_load
def setup(app):
    """Board, column and card handling ships with the core app, so no blueprint is attached."""
    pass
