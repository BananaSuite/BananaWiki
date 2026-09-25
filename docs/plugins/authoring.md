# Plugin Authoring Guide

> SDK version `1.0.0` · API version `1.0`

Build a BananaWiki plugin, from project layout through packaging a
`.bwplugin` file for distribution.

A plugin runs inside the wiki process with the wiki's own privileges, so
the admin who installs it is trusting you with the whole wiki.  The upload
form and the enable page tell them so, and ask for their password.  Keep the
code small enough to read: an admin who cannot review it should not install
it.  See [overview.md](overview.md#what-an-external-plugin-can-do).

## 1. Project structure

A plugin is a directory containing at minimum a `plugin.json` manifest and an
`__init__.py` entry point.  Everything else is optional.

```
my_plugin/
├── plugin.json          # required: manifest
├── __init__.py          # required: entry point (must define `plugin = Plugin("my_plugin")`)
├── routes.py            # recommended: route handlers (imported from __init__.py)
├── db.py                # optional: DB helper functions
├── templates/
│   └── my_plugin/       # namespace your templates under the plugin id
│       └── index.html
├── static/
│   └── my_plugin/       # namespace static files likewise
│       └── style.css
└── README.md
```

Namespace your `templates/` and `static/` sub-directories under the plugin ID
(`my_plugin/`) to avoid collisions with other plugins or core templates.

## 2. The plugin manifest (`plugin.json`)

Every plugin must include a `plugin.json` file in its root directory.  This
manifest describes the plugin to the loader and to the admin UI.

```json
{
    "id": "my_plugin",
    "name": "My Plugin",
    "version": "1.0.0",
    "author": "Your Name",
    "description": "A short description of what this plugin does.",
    "bananawiki_api_version": "1.0"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier: starts with a letter, then letters, digits, `_` or `-`, at most 64 characters, and never `__`. Lowercase with underscores is the convention. The folder the plugin is installed in is named after it; a folder whose name differs is ignored. |
| `name` | Yes | Human-readable display name, at most 100 characters. |
| `version` | Yes | Version string (e.g. `"1.2.0"`), at most 50 characters. |
| `author` | No | Author name or organisation, at most 200 characters. |
| `description` | No | One-sentence description shown in the admin plugin list, at most 2000 characters. |
| `bananawiki_api_version` | No | Minimum SDK API version required (default `"1.0"`). The major version must match the wiki's SDK. It is checked at upload and again whenever the plugin is loaded. |
| `builtin` | No | Must be `false` or absent. An upload that sets it to anything else is refused, and whether a plugin is built-in is decided by the folder it lives in, never by this field. |

`id`, `name`, `version`, `author` and `description` must be strings.  An
upload with any other type, or with text over the limit, is refused before
anything is written to disk.

> **Tip:** Choose a plugin ID that is unique to your project.  Using a
> reversed domain namespace (`com_example_my_plugin`) is a good practice for
> plugins intended for public distribution.  An upload whose id matches a
> built-in plugin, a name a built-in uses for its tables
> (`user_profile_fields`), or a plugin already installed, is refused.  Ids
> are compared without regard to case, so `Kanban` counts as `kanban`.

## 3. The entry point (`__init__.py`)

Every plugin must expose a `Plugin` instance named `plugin` at module
level.  The loader looks for this attribute to discover lifecycle callbacks.

```python
# my_plugin/__init__.py
from bananawiki_sdk import Plugin

plugin = Plugin("my_plugin")
```

The `Plugin` constructor takes the plugin ID string, which must match the
`id` field in `plugin.json`.  If it does not, the loader logs a warning and
uses the id from `plugin.json`.

## 4. Plugin lifecycle hooks

Three decorators control when your code runs:

```python
@plugin.on_load
def setup(app):
    """Called once when the Flask application loads this plugin.

    ``app`` is the live Flask application object.  Register routes, template
    folders, static folders, hooks, and template slots here.
    """
    pass


@plugin.on_enable
def enable():
    """Called when an admin enables the plugin via Admin → Plugins.

    Use this for one-time setup that should run on each enable: for example
    sending an internal notification or logging an audit event.  This is
    called *after* on_load.
    """
    pass


@plugin.on_disable
def disable():
    """Called when an admin disables the plugin via Admin → Plugins.

    Use this for teardown: for example stopping a background thread or
    clearing a cache.  Data should never be deleted here.
    """
    pass
```

> **Important:** `on_load` receives the Flask `app` and is the only lifecycle
> hook that does.  Use it for any Flask-level registration (routes, blueprints,
> template folders, static folders).  Do not perform heavy I/O or blocking
> operations inside `on_load` as it runs during server startup.

The wiki usually runs several Gunicorn workers.  `on_load` runs once in every
process that loads the plugin: once before the workers start for a plugin
enabled at startup, and once in each worker for a plugin enabled while the
wiki runs.  `on_enable` and `on_disable` run only in the worker that handled
the admin's click; the other workers pick up the change from the plugin
registry at their next request.  Something you start in every worker, such
as a background thread, should check `is_plugin_enabled()` itself and stop
when the plugin is off.

## 5. Registering Flask routes

Register routes inside `on_load` by decorating functions on the `app` object.

```python
from bananawiki_sdk import Plugin, login_required, admin_required, rate_limit

plugin = Plugin("my_plugin")


@plugin.on_load
def setup(app):

    @app.route("/my-plugin")
    @login_required
    def my_plugin_index():
        from bananawiki_sdk import render_template, get_current_user
        user = get_current_user()
        return render_template("my_plugin/index.html", user=user)

    @app.route("/my-plugin/action", methods=["POST"])
    @login_required
    @rate_limit(max_requests=10, window=60)
    def my_plugin_action():
        from bananawiki_sdk import redirect, url_for, flash
        # ... do something ...
        flash("Action completed!", "success")
        return redirect(url_for("my_plugin_index"))
```

Apply one of these auth decorators to every route:

| Decorator | Who can access |
|-----------|---------------|
| `login_required` | Any logged-in user |
| `editor_required` | Editors and admins |
| `admin_required` | Admins only |

Always apply `@rate_limit` to mutation routes (POST, PUT, DELETE). See
[Section 16](#16-rate-limiting) for details.

> **Note on route naming:** Flask requires all route function names to be
> globally unique. Prefix your function names with the plugin ID
> (e.g. `my_plugin_index`) to avoid collisions with core routes or other
> plugins.

The loader records every route that `on_load` adds.  While the plugin is
disabled, and after it is deleted, those routes answer 404 in every worker,
and `before_request`, `after_request`, teardown handlers, context
processors and error handlers added in `on_load` do nothing.  Register
them in `on_load`, not later from a request or a thread, or they are not
tied to your plugin.

## 6. Using Jinja2 templates

The simplest approach is to use a Flask `Blueprint` with a template folder:

```python
import os
from flask import Blueprint

@plugin.on_load
def setup(app):
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    bp = Blueprint(
        "my_plugin",
        __name__,
        template_folder=os.path.join(plugin_dir, "templates"),
        static_folder=os.path.join(plugin_dir, "static"),
        static_url_path="/static/my_plugin",
    )

    @bp.route("/my-plugin")
    def index():
        from bananawiki_sdk import render_template
        return render_template("my_plugin/index.html")

    app.register_blueprint(bp)
```

In your template (`my_plugin/templates/my_plugin/index.html`) you can extend
the core base layout:

```html
{% extends "base.html" %}

{% block title %}My Plugin{% endblock %}

{% block content %}
<h1>My Plugin</h1>
<p>Hello, {{ current_user.username }}!</p>
{% endblock %}
```

The `current_user`, `settings`, `enabled_plugins`, `time_ago`, and other
context variables injected by the core context processor are automatically
available in all templates, so you do not need to pass them from your route
handler.

## 7. Serving static files

If you registered a `static_folder` on your Blueprint (as shown above), your
static files are served at `/static/my_plugin/<filename>` and reachable in
templates via:

```html
<link rel="stylesheet" href="{{ url_for('my_plugin.static', filename='style.css') }}">
```

## 8. Subscribing to core hooks

Hooks let your plugin react to events in the core without modifying core code.
Use `@hook(name)` at module level (outside `on_load`) or `@plugin.hook(name)`
inside `on_load`.

```python
from bananawiki_sdk import hook

@hook("after_page_update")
def on_page_update(page, user, **kwargs):
    """Called after any wiki page is saved."""
    print(f"Page '{page['title']}' was updated by {user['username']}")
```

Register hooks at module level or inside `on_load`.  Everything registered
while the plugin loads is tagged with the id from `plugin.json`, whatever
decorator you used, and only runs while the plugin is enabled in the plugin
registry.  A hook registered later with the bare `@hook`, from a request or
a thread, carries no tag and keeps running after the plugin is disabled.
`@plugin.hook(name)` on the `Plugin` instance always tags the handler and is
the clearest spelling inside `on_load`:

```python
@plugin.on_load
def setup(app):

    @plugin.hook("after_page_create")
    def on_page_create(page, user, **kwargs):
        # page is a sqlite3.Row: access fields by name
        title = page["title"]
        ...
```

### Core hooks reference

| Hook name | Keyword arguments | When emitted |
|-----------|-------------------|--------------|
| `after_page_create` | `page`, `user` | After a new wiki page is successfully created |
| `after_page_update` | `page`, `user` | After a wiki page is saved (edit) |
| `after_page_delete` | `page`, `user` | After a wiki page is deleted |
| `after_login` | `user` | After a user logs in successfully |
| `after_user_create` | `user` | After a new user account is created |

All payloads are `sqlite3.Row` objects; access fields with `row["field_name"]`.
Always include `**kwargs` in your handler signature to remain forward-compatible
with new payload fields added in future versions.

### Emitting custom hooks

Your plugin can emit its own hooks so that *other* plugins can integrate with
yours:

```python
from bananawiki_sdk import emit_hook

emit_hook("my_plugin.after_action", data=my_data, user=current_user)
```

## 9. Injecting HTML with template slots

Template slots let your plugin insert HTML into predefined locations in core
templates without forking those templates.

```python
from bananawiki_sdk import template_slot

@template_slot("page.below_content")
def render_word_count(context):
    """Render HTML to show below every wiki page."""
    page = context.get("page")
    if not page:
        return ""
    return f'<p class="plugin-note">📝 Rendered by my_plugin for page {page["id"]}</p>'
```

The function receives a `context` dict and must return an HTML string (or an
empty string to render nothing).  Multiple plugins may register renderers for
the same slot; their outputs are concatenated in registration order.

### Available template slots

| Slot name | Context keys | Where it renders |
|-----------|-------------|-----------------|
| `page.below_content` | `page`, `user` | Below the content of every wiki page (`wiki/page.html`) |
| `sidebar.bottom` | `user` | Bottom of the navigation sidebar on every page (`base.html`) |

> **Adding new slots:** If you need a slot location that doesn't exist yet,
> add a `{{ render_slot("my_slot", {...}) | safe }}` call to the appropriate
> core template and document it for other plugin authors.

## 10. Database access

Use the SDK helpers instead of importing `db`.  They are guard rails for
honest plugins, not a security boundary: plugin code runs inside the wiki
process and nothing stops it importing `db` or `sqlite3`.  What they
guarantee is that a mistake made through them cannot change core data.

```python
from bananawiki_sdk import db_query, db_execute
```

### Reading data: `db_query`

```python
# Returns a list of sqlite3.Row objects
pages = db_query(
    "SELECT id, title, slug FROM pages WHERE category_id = ?",
    [category_id],
)
for page in pages:
    print(page["title"])
```

`db_query` is read-only.  A statement that would change anything, even one
hidden in a common table expression, raises `PluginError`.

### Writing to plugin-owned tables: `db_execute`

```python
# Returns the lastrowid for INSERT statements
row_id = db_execute(
    "INSERT INTO my_plugin__events (page_id, event, created_at) VALUES (?, ?, ?)",
    [page_id, "view", "2025-01-01T00:00:00"],
)
```

`db_execute` raises `PluginError` if the statement would write to a core
table, however it is spelled (`main.users`, `UPDATE OR REPLACE`, comments
inside the statement).  Every table the BananaWiki schema creates is a core
table; see the [API reference](api-reference.md#core-tables-read-only).

### Creating your plugin's tables

Create your tables inside `on_load` so they exist from the first request.
Always use `CREATE TABLE IF NOT EXISTS` so the statement is safe to re-run on
server restart:

```python
@plugin.on_load
def setup(app):
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS my_plugin__events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id   INTEGER NOT NULL,
            event     TEXT    NOT NULL,
            created_at TEXT   NOT NULL
        )
        """
    )
```

**Naming convention:** prefix all your tables with `<plugin_id>__` (double
underscore) to avoid collisions with core tables and other plugins.  When an
admin deletes the plugin, its data is kept unless they tick **Also delete the
plugin's data**.  The confirmation page then lists the tables that will go:
those whose name starts with exactly `<plugin_id>__`, case included, that
are not core tables and do not belong to another plugin with a longer id
(`foo___data` belongs to `foo_`, not to `foo`).  A table without the prefix
is never dropped.

## 11. Reading site settings and user context

```python
from bananawiki_sdk import get_current_user, get_setting

def some_route_handler():
    user = get_current_user()     # sqlite3.Row or None
    if user:
        print(user["username"], user["role"])

    site_name = get_setting("site_name")   # reads site_settings table
    print(site_name)
```

## 12. Custom permissions

Declare custom permissions in your plugin's module-level code.  They will
appear in **Admin → User Permissions** so admins can grant or revoke them
per-user.

```python
plugin.register_permission(
    key="my_plugin.view_stats",
    label="View Plugin Stats",
    description="Allow the user to view word-count statistics.",
    default_editor=True,   # editors have this permission by default
    default_user=False,    # regular users do not
)
```

Then check the permission inside your route handler:

```python
from bananawiki_sdk import has_permission, get_current_user
from flask import abort

def my_stats_route():
    user = get_current_user()
    if not user or not has_permission(user, "my_plugin.view_stats"):
        abort(403)
    # ... render stats ...
```

**Permission key convention:** use the format `<plugin_id>.<permission_name>`
(e.g. `my_plugin.view_stats`) to avoid collisions with core permission keys.

## 13. Admin menu items

Add a link to the admin sidebar so admins can navigate directly to your
plugin's admin page:

```python
plugin.add_admin_menu_item(
    label="Word Count Stats",
    endpoint="my_plugin_stats",   # Flask endpoint name
    icon="📊",
)
```

The `endpoint` must match the Flask route function name you registered.

## 14. Optional integration with other plugins

Check whether a sibling plugin is active before using it, so your plugin
degrades gracefully on instances where that plugin isn't installed:

```python
from bananawiki_sdk import is_plugin_enabled

@hook("after_page_update")
def on_page_update(page, user, **kwargs):
    if is_plugin_enabled("badges"):
        # Optional: award a badge via a hook the badges plugin emits
        emit_hook("badges.check_triggers", user=user)
```

## 15. Flash messages and redirects

```python
from bananawiki_sdk import flash, redirect, url_for

def my_action():
    # ... perform action ...
    flash("Your action was successful!", "success")
    return redirect(url_for("my_plugin_index"))
```

The standard BananaWiki flash categories are `"success"`, `"error"`, and
`"warning"`.  Use consistent phrasing to match the core UX:

- Success: `"X has been successfully …"`
- Permission error: `"You do not have the required permissions to …"`
- Validation: `"X cannot exceed …"` / `"X is required to continue"`

## 16. Rate limiting

Apply `@rate_limit` to every mutation route (POST, PUT, DELETE):

```python
from bananawiki_sdk import rate_limit

@app.route("/my-plugin/action", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def my_plugin_action():
    ...
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_requests` | `60` | Maximum number of requests allowed in the window |
| `window` | `60` | Time window in seconds |

When the limit is exceeded, BananaWiki automatically returns a `429 Too Many
Requests` response.  The rate limit is enforced per IP address and is
backed by the database (`rate_limit_hits` table) so it works correctly across
multiple Gunicorn workers.

## 17. Logging actions

```python
from bananawiki_sdk import log_action
from flask import request

def my_route():
    user = get_current_user()
    # ... perform action ...
    log_action("my_plugin.action_done", request, user=user, page_id=42)
```

`log_action(action, request, user=None, **details)` includes extra keyword
arguments as structured detail fields in the log entry.  The `user`
parameter accepts either a user row or a plain username string; only the
username is written to the log.

## 18. Raising plugin errors

```python
from bananawiki_sdk import PluginError, PluginConfigError

# General failure inside plugin code
raise PluginError("Something went wrong in my_plugin.")

# Invalid configuration (e.g. missing required setting)
raise PluginConfigError("my_plugin requires a valid API key in site settings.")
```

Do not let exceptions propagate out of hook handlers. The core catches
them and logs a traceback, but it is cleaner to handle them explicitly.

## 19. Packaging a `.bwplugin` file

A `.bwplugin` file is a standard ZIP archive with a `.bwplugin` extension.

```bash
# From inside the plugin directory
cd path/to/my_plugin/
zip -r ../my_plugin.bwplugin plugin.json __init__.py routes.py db.py templates/ static/
```

Or using Python:

```python
import zipfile, pathlib

root = pathlib.Path("my_plugin")
with zipfile.ZipFile("my_plugin.bwplugin", "w", zipfile.ZIP_DEFLATED) as zf:
    for path in root.rglob("*"):
        zf.write(path, path.relative_to(root.parent))
```

The archive may place files either at the root level or inside a single
subdirectory; the loader handles both layouts.

**Before packaging, verify:**

- `plugin.json` is present and contains `id`, `name`, and `version`.
- `"builtin"` is absent or `false`.
- `"bananawiki_api_version"` is set to `"1.0"` (or omitted for the default).
- Your plugin ID does not match a built-in plugin, a name a built-in uses
  for its tables, or an already-installed plugin, compared without regard
  to case.

Import via **Admin → Plugins → Import External Plugin**; the admin enters
their password with the file.  The archive and manifest are validated first.
The files are then unpacked into a hidden staging folder, the plugin is
registered as disabled, and only then is the folder renamed to
`plugins/external/<plugin_id>/`.  No plugin code runs until an admin enables
it.

## 20. Example plugins

The [`examples/`](examples/) directory contains two ready-to-run plugins:

| Example | What it demonstrates |
|---------|---------------------|
| [`hello_world/`](examples/hello_world/) | Minimal plugin: one route, one `@hook`, `@login_required` |
| [`page_word_count/`](examples/page_word_count/) | `@hook`, `db_execute` / `db_query`, `@template_slot`, plugin-owned table |

Below is an annotated version of `page_word_count` showing everything in one
place:

```python
# page_word_count/__init__.py
from bananawiki_sdk import (
    Plugin, hook, template_slot,
    db_query, db_execute,
)

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
```

## Further reading

- [`overview.md`](overview.md): plugin system architecture and built-in plugin catalogue
- [`api-reference.md`](api-reference.md): `bananawiki_sdk` symbol reference
- [`examples/hello_world/`](examples/hello_world/): minimal working example
- [`examples/page_word_count/`](examples/page_word_count/): realistic example with hooks, DB, and template slots
