"""Assessments: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("assessments")


@plugin.on_load
def setup(app):
    """No blueprint is attached here; poll and test handling lives in the core assessment routes."""
    pass
