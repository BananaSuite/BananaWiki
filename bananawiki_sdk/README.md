# BananaWiki Plugin SDK

The `bananawiki_sdk` package is the public API for BananaWiki plugin authors.
It exposes a stable, versioned interface so that plugins can interact with
core functionality without depending on internal implementation details.

**SDK version:** `1.0.0` &nbsp;·&nbsp; **API version:** `1.0`

## Installation

The SDK is bundled with BananaWiki and is available on the Python path whenever
the server is running.  No separate installation step is needed.

```python
from bananawiki_sdk import Plugin, hook, template_slot, db_query, db_execute
```

## Quick start

```python
# my_plugin/__init__.py
from bananawiki_sdk import Plugin

plugin = Plugin("my_plugin")

@plugin.on_load
def setup(app):
    """Register routes, hooks, and template slots here."""
    @app.route("/my-plugin")
    def my_page():
        return "<h1>Hello from My Plugin!</h1>"

@plugin.on_enable
def enabled():
    print("my_plugin enabled")

@plugin.on_disable
def disabled():
    print("my_plugin disabled")
```

See `docs/plugins/authoring.md` for a complete walkthrough, and
`docs/plugins/api-reference.md` for the full symbol reference.

## API surface

### `Plugin(plugin_id: str)`

Central registration object.  Create exactly one per plugin in `__init__.py`.

| Method | Description |
|--------|-------------|
| `on_load(fn)` | Decorator: `fn(app)` is called when the Flask app loads this plugin |
| `on_enable(fn)` | Decorator: `fn()` is called when an admin enables the plugin |
| `on_disable(fn)` | Decorator: `fn()` is called when an admin disables the plugin |
| `hook(hook_name)(fn)` | Decorator: subscribes `fn` to a named hook (tagged with plugin ID) |
| `register_permission(key, label, description, default_editor, default_user)` | Declare a custom permission |
| `add_admin_menu_item(label, endpoint, icon)` | Add an entry to the admin sidebar menu |

### Hooks

```python
from bananawiki_sdk import hook, emit_hook

# Subscribe to a core hook
@hook("after_page_update")
def on_page_update(page, user, **kwargs):
    ...

# Emit a hook (core code or plugins can emit custom hooks)
emit_hook("my_plugin.after_action", data=result, user=current_user)
```

Available core hooks: `after_page_create`, `after_page_update`,
`after_page_delete`, `after_login`, `after_user_create`.

All payloads are `sqlite3.Row` objects.  For `after_page_delete` the `page`
keyword holds the full row of the deleted page (not just the ID).

### Template slots

```python
from bananawiki_sdk import template_slot, render_slot

# Register HTML to inject into a named slot
@template_slot("page.below_content")
def my_widget(context):
    page = context.get("page")
    return f"<p>My plugin: page {page['id']}</p>" if page else ""
```

In Jinja2 templates, slots are rendered with:
`{{ render_slot("page.below_content", {"page": page, "user": current_user}) | safe }}`

Available core slots: `page.below_content` (context: `page`, `user`),
`sidebar.bottom` (context: `user`).

### Database helpers

```python
from bananawiki_sdk import db_query, db_execute

# Read rows (returns list of sqlite3.Row objects)
rows = db_query("SELECT * FROM pages WHERE category_id = ?", [cat_id])

# Write to plugin-owned tables (INSERT / UPDATE / DELETE)
db_execute("INSERT INTO my_plugin__table (key, value) VALUES (?, ?)", [k, v])
```

Plugins should create their own tables in `on_load` via `db_execute`.
Prefix table names with `<plugin_id>__` to avoid collisions; only tables with
that exact prefix can be dropped when an admin deletes the plugin.

`db_query` refuses anything that would change the database, and
`db_execute` refuses writes to core tables.  SQLite's authorizer enforces
both, so the check sees each statement as SQLite parses it.  These are guard
rails for honest plugins, not a security boundary: plugin code runs inside
the wiki process with the wiki's own privileges.

### Core re-exports

The following symbols from BananaWiki core are re-exported for convenience:

| Symbol | Description |
|--------|-------------|
| `get_current_user()` | Returns the logged-in user row or `None` |
| `has_permission(user, key)` | Check a custom or built-in permission |
| `is_plugin_enabled(plugin_id)` | Check whether another plugin is active |
| `get_setting(key)` | Read a value from `site_settings` |
| `flash(message, category)` | Flask flash message |
| `redirect(location)` | Flask redirect response |
| `url_for(endpoint, **values)` | Flask URL builder |
| `render_template(template, **ctx)` | Jinja2 template renderer |
| `rate_limit(max_requests, window)` | Rate-limiting decorator (defaults: 60 req / 60 s) |
| `login_required` | Redirect to login if not authenticated |
| `admin_required` | Return 403 if not admin |
| `editor_required` | Return 403 if not editor or admin |
| `log_action(action, request, user, **details)` | Structured action logger |

### Exceptions

```python
from bananawiki_sdk import PluginError, PluginConfigError, PluginAPIVersionError
```

| Exception | When to raise |
|-----------|---------------|
| `PluginError` | General plugin failure |
| `PluginConfigError` | Invalid or missing plugin configuration |
| `PluginAPIVersionError` | Plugin requires a newer SDK API version |

## Plugin manifest (`plugin.json`)

```json
{
    "id": "my_plugin",
    "name": "My Plugin",
    "version": "1.0.0",
    "author": "Your Name",
    "description": "A short description.",
    "bananawiki_api_version": "1.0"
}
```

## Further reading

- `docs/plugins/overview.md`: architecture and first-party plugin list
- `docs/plugins/authoring.md`: step-by-step authoring guide
- `docs/plugins/api-reference.md`: complete API reference
- `docs/plugins/examples/`: example plugins
