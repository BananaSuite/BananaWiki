"""The plugin's page and event handler."""

from flask import render_template

from bananawiki.wiki import auth
from bananawiki.wiki.db import db
from bananawiki.wiki.registry import feature_blueprint

bp = feature_blueprint("hello_plugin", "hello_plugin", __name__, template_folder="templates")


@bp.get("/hello")
def index():
    """Every signed-in user may see the page (views are private by default)."""
    created = db.scalar("SELECT value FROM hello_plugin__counts WHERE name = 'pages_created'", default=0)
    return render_template("hello_plugin/index.html", user=auth.current_user(), created=created)


def count_page(page, **_):
    db.execute(
        "INSERT INTO hello_plugin__counts (name, value) VALUES ('pages_created', 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1"
    )
