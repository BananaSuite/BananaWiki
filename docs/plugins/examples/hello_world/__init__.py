"""Hello World: minimal example BananaWiki plugin."""

from bananawiki_sdk import Plugin, hook, login_required

plugin = Plugin("hello_world")


@plugin.on_load
def setup(app):
    """Register a single route that greets the user."""

    @app.route("/hello")
    @login_required
    def hello_world():
        from bananawiki_sdk import get_current_user, render_template
        from markupsafe import Markup, escape
        user = get_current_user()
        name = escape(user["username"]) if user else "stranger"
        return Markup(f"<h1>Hello, {name}!</h1><p>This page is provided by the Hello World plugin.</p>")


@hook("after_login")
def greet_on_login(user, **kwargs):
    """Print a greeting when a user logs in."""
    print(f"[hello_world] {user['username']} just logged in!")
