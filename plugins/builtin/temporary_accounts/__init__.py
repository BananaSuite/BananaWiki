"""Temporary Accounts & Pages: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin

plugin = Plugin("temporary_accounts")


@plugin.on_load
def setup(app):
    """Nothing to wire at load time: page and account expiry is served by the core temporary routes."""
    pass
