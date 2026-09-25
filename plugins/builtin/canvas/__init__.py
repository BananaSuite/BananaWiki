"""Canvas: first-party BananaWiki plugin."""

from bananawiki_sdk import Plugin, hook
import db


plugin = Plugin("canvas")


@plugin.on_load
def setup(app):
    """Register hooks when the plugin is loaded."""
    pass


@hook("after_page_update")
def _on_page_update(page, user, **kwargs):
    """Sync canvas wiki-page nodes when a page is updated."""
    if not db.is_plugin_enabled("canvas"):
        return
    db.canvas_update_wiki_nodes_for_page(
        page["id"],
        new_title=page["title"],
        new_slug=page["slug"],
    )


@hook("after_page_delete")
def _on_page_delete(page, user, **kwargs):
    """Mark canvas wiki-page nodes as deleted when a page is removed."""
    if not db.is_plugin_enabled("canvas"):
        return
    db.canvas_mark_deleted_wiki_nodes(page["id"])
