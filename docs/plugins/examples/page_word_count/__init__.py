"""Page Word Count: realistic example BananaWiki plugin.

Hooks into ``after_page_update``, stores a word count per page in a
plugin-owned table, and renders a count badge via the
``page.below_content`` template slot.
"""

from bananawiki_sdk import Plugin, hook, template_slot, db_query, db_execute

plugin = Plugin("page_word_count")


@plugin.on_load
def setup(app):
    """Create the plugin table on first load."""
    db_execute(
        "CREATE TABLE IF NOT EXISTS page_word_count__counts "
        "(page_id INTEGER PRIMARY KEY, word_count INTEGER NOT NULL DEFAULT 0)"
    )


@hook("after_page_update")
def update_word_count(page, user, **kwargs):
    """Recount words whenever a page is saved."""
    content = page["content"] if hasattr(page, "__getitem__") else ""
    count = len(content.split())
    db_execute(
        "INSERT OR REPLACE INTO page_word_count__counts (page_id, word_count) "
        "VALUES (?, ?)",
        [page["id"], count],
    )


@template_slot("page.below_content")
def render_word_count(context):
    """Show word count below the page content."""
    page = context.get("page")
    if not page:
        return ""
    rows = db_query(
        "SELECT word_count FROM page_word_count__counts WHERE page_id = ?",
        [page["id"]],
    )
    if rows:
        count = rows[0]["word_count"]
        return (
            f'<p style="opacity:.6;font-size:.82rem;margin-top:1rem">'
            f'📝 {count} words</p>'
        )
    return ""
