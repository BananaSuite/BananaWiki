"""Audit: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("audit")


@plugin.on_load
def setup(app):
    """Empty by design: role history, user tags and contribution tracking are core features, not plugin routes."""
    pass
