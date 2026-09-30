"""Hello Plugin: a minimal BananaWiki 1.6 plugin.

It shows the pieces a plugin usually needs: a page (``routes.py``), a
sidebar entry, a table of its own (``schema.py``) and an event handler that
counts the pages created while it is enabled.
"""

from bananawiki.wiki.registry import Feature, NavItem

from .routes import bp, count_page

FEATURE = Feature(
    id="hello_plugin",
    name="hello_plugin.name",
    description="hello_plugin.description",
    blueprints=[bp],
    nav=[NavItem("hello_plugin.nav", "hello_plugin.index", icon="smile", area="apps")],
    events={"page.created": [count_page]},
)
