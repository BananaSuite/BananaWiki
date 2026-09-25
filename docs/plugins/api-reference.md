# Plugin SDK API Reference

> SDK version `1.0.0` · API version `1.0`

All public symbols are importable directly from `bananawiki_sdk`:

```python
from bananawiki_sdk import (
    # Plugin class
    Plugin,
    # Hooks
    hook, emit_hook,
    # Template slots
    template_slot, render_slot,
    # Database helpers
    db_query, db_execute,
    # Context helpers
    get_current_user, has_permission, is_plugin_enabled, get_setting,
    # Flask re-exports
    flash, redirect, url_for, render_template,
    # Decorators
    rate_limit, login_required, admin_required, editor_required,
    # Logging
    log_action,
    # Encryption
    encrypt_value, decrypt_value,
    # Exceptions
    PluginError, PluginConfigError, PluginAPIVersionError,
)
```

## `Plugin`

```python
class Plugin(plugin_id: str)
```

Central registration object.  Create exactly one `Plugin` instance per
plugin, assign it to a module-level variable named `plugin`, and use its
decorators to register lifecycle callbacks, hooks, and permissions.

Pass the same id as in `plugin.json`.  If the two differ, the loader logs a
warning and uses the id from `plugin.json`.

```python
from bananawiki_sdk import Plugin

plugin = Plugin("my_plugin")
```

### Lifecycle decorators

#### `@plugin.on_load`

```python
@plugin.on_load
def setup(app: Flask) -> None: ...
```

Called once when the Flask application loads this plugin.  The decorated
function receives the live `Flask` application object.  Register all routes,
blueprints, hooks, template slots, and database tables here.

#### `@plugin.on_enable`

```python
@plugin.on_enable
def enable() -> None: ...
```

Called each time an admin enables the plugin via **Admin → Plugins**.  Runs
after `on_load`.  Use for one-time setup that should repeat on each enable.

It runs once, in the Gunicorn worker that handled the admin's request.  The
other workers load the plugin at their next request (its routes, hooks and
slots start working there) without calling `on_enable` again.

#### `@plugin.on_disable`

```python
@plugin.on_disable
def disable() -> None: ...
```

Called when an admin disables the plugin.  Use for teardown (e.g. stopping
a background thread).  **Never delete data here.**

Like `on_enable`, it runs only in the worker that handled the admin's
request.  Something your plugin started in every worker, such as a thread,
should check `is_plugin_enabled()` itself and stop when the plugin is off.
The plugin's routes, hooks, template slots and request handlers stop in
every worker without any help from the plugin.

### Registration methods

#### `plugin.hook(hook_name)`

```python
@plugin.hook("after_page_update")
def handler(page, user, **kwargs): ...
```

Convenience wrapper around the module-level `@hook` decorator.  Tags the
handler with the plugin ID so the loader can deregister it cleanly on
disable.  Prefer it over the bare `@hook` decorator when writing handlers
inside a plugin.

The tag is bookkeeping, not a security label.  Everything a plugin registers
while it loads, in its module code or in `on_load`, is tagged with the id
from its `plugin.json`, replacing whatever id the code set.  A handler
registered later, from a request or a thread, is not tagged by the loader
and keeps running after the plugin is disabled, unless it went through
`plugin.hook`.

#### `plugin.register_permission(key, label, description, default_editor, default_user)`

```python
plugin.register_permission(
    key="my_plugin.view_stats",
    label="View Plugin Stats",
    description="Allow viewing plugin statistics.",
    default_editor=True,
    default_user=False,
)
```

Declares a custom permission that admins can grant per-user in
**Admin → User Permissions**.

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str` | Unique permission key. Convention: `<plugin_id>.<name>`. |
| `label` | `str` | Human-readable name shown in the admin UI. |
| `description` | `str` | One-sentence description. |
| `default_editor` | `bool` | Whether editors have this permission by default. |
| `default_user` | `bool` | Whether regular users have this permission by default. |

#### `plugin.add_admin_menu_item(label, endpoint, icon="")`

```python
plugin.add_admin_menu_item(
    label="My Stats",
    endpoint="my_plugin_stats",
    icon="📊",
)
```

Adds a link to the admin sidebar.  `endpoint` is the Flask endpoint name
(the route function name).

## Hooks

### `@hook(hook_name: str)`

```python
from bananawiki_sdk import hook

@hook("after_page_update")
def on_update(page, user, **kwargs): ...
```

Decorator that subscribes a function to a named hook.  Multiple handlers can
subscribe to the same hook name; they are called in registration order.
Exceptions inside handlers are caught and logged, so a broken handler does not
stop other handlers from running.

Always include `**kwargs` in your handler signature to stay forward-compatible
when new payload fields are added.

### `emit_hook(hook_name: str, **kwargs) → None`

```python
from bananawiki_sdk import emit_hook

emit_hook("my_plugin.after_action", data=result, user=current_user)
```

Calls all subscribers registered for `hook_name`.  Exceptions in individual
subscribers are caught and printed; execution continues with the next
subscriber.

A subscriber that belongs to a plugin only runs while that plugin is enabled
in the plugin registry (the `plugins` table), which every worker shares.
Subscribers without an owner, such as core code, always run.

### Core hooks

The BananaWiki core emits these hooks.  All payload values are
`sqlite3.Row` objects (access fields by name: `row["field_name"]`).

| Hook name | Keyword arguments | Emitted in |
|-----------|-------------------|-----------|
| `after_page_create` | `page`, `user` | `routes/wiki.py`: after a new page is saved |
| `after_page_update` | `page`, `user` | `routes/wiki.py`: after an existing page is saved |
| `after_page_delete` | `page`, `user` | `routes/wiki.py`: after a page is deleted (`page` is the row that was deleted) |
| `after_login` | `user` | `routes/auth.py`: after a successful login |
| `after_user_create` | `user` | `routes/admin.py`: after a new user account is created |

## Template Slots

### `@template_slot(slot_name: str)`

```python
from bananawiki_sdk import template_slot

@template_slot("page.below_content")
def render_extra(context: dict) -> str:
    page = context.get("page")
    if not page:
        return ""
    return f"<p>Extra content for page {page['id']}</p>"
```

Decorator that registers a renderer for a named template location.  The
decorated function receives a `context` dict and must return an HTML string
(or an empty string to render nothing).  Multiple renderers may be registered
for the same slot; their outputs are concatenated.

### `render_slot(slot_name: str, context: dict = None) → str`

```python
from bananawiki_sdk import render_slot

html = render_slot("page.below_content", {"page": page_row, "user": user_row})
```

Renders all functions registered for `slot_name` and returns their
concatenated HTML.  Exceptions in individual renderers are caught and logged.
A renderer that belongs to a plugin is skipped while that plugin is disabled,
or hidden by EasyWiki mode; outside a request the plugin registry decides.

In Jinja2 templates, slots are rendered with:

```html
{{ render_slot("page.below_content", {"page": page, "user": current_user}) | safe }}
```

### Core template slots

| Slot name | Context keys | Template file | Location |
|-----------|-------------|---------------|----------|
| `page.below_content` | `page`, `user` | `wiki/page.html` | Below the body of every wiki page |
| `sidebar.bottom` | `user` | `base.html` | Bottom of the navigation sidebar, visible on every page |

## Database helpers

Use these two helpers rather than importing `db`.  They are guard rails for
honest plugins, not a security boundary: a plugin runs inside the wiki
process and could import `db` or `sqlite3` and change anything.  What the
helpers guarantee is that a mistake in a well-meant plugin cannot write to
core data through them.

Both rules are enforced by SQLite's authorizer, so they see a statement the
way SQLite parses it.  `main.users`, comments inside the statement,
`UPDATE OR REPLACE` and common table expressions get the same answer as the
plain spelling.  Each call runs exactly one statement.

### `db_query(sql: str, params: list = None) → list[sqlite3.Row]`

```python
from bananawiki_sdk import db_query

rows = db_query(
    "SELECT id, title, slug FROM pages WHERE category_id = ?",
    [category_id],
)
for row in rows:
    print(row["title"])
```

Executes a read-only SQL query against the BananaWiki database and
returns a list of `sqlite3.Row` objects.

Raises `PluginError` if the statement would change the database in any way,
including a write hidden in a common table expression.  `ATTACH`, `DETACH`
and PRAGMAs other than schema introspection (`table_info`, `table_xinfo`,
`table_list`, `index_list`, `index_info`, `index_xinfo`,
`foreign_key_list`) are refused too.

### `db_execute(sql: str, params: list = None) → int`

```python
from bananawiki_sdk import db_execute

last_id = db_execute(
    "INSERT INTO my_plugin__logs (user_id, action) VALUES (?, ?)",
    [user_id, "view"],
)
```

Executes a write SQL statement (INSERT, UPDATE, DELETE, CREATE TABLE,
etc.) against plugin-owned tables only.  Returns the `cursor.lastrowid`
(useful for INSERT statements).

Raises `PluginError` if the statement would create, change or drop a core
table, or an index or trigger on one; if it attaches another database; or
if it runs a PRAGMA other than the introspection ones listed above.

### Core tables (read-only)

Every table the BananaWiki schema creates is a core table.  Plugins may read
them with `db_query` but cannot write to them with `db_execute`.  The list is
`_CORE_TABLES` in `bananawiki_sdk/_database.py`, extended at run time with
every table the site export knows about (`db/_migration.py`), and a test
checks that a freshly created database has no table missing from it.  That
includes the namespaced tables the core keeps for built-in plugins, such as
`canvas__layouts` and `api_service__tokens`.

Give your own tables the `<plugin_id>__` prefix.  It keeps them apart from
core and from other plugins, and only tables with that exact prefix can be
dropped when the plugin is deleted (see [overview.md](overview.md#deleting--uninstalling-a-plugin)).

> **Note:** Use the `db.*` functions of the core for writes to core data from
> built-in code.  For an external plugin, a write to core data is outside
> what the SDK supports.

## Re-exported helpers

These symbols are re-exported from Flask and from BananaWiki core modules so
plugin authors never need to import from internal packages directly.

### Context helpers

#### `get_current_user() → sqlite3.Row | None`

```python
from bananawiki_sdk import get_current_user

user = get_current_user()
if user:
    print(user["username"], user["role"])
```

Returns the logged-in user's database row, or `None` if no user is logged in.
Reads `session["user_id"]` from the Flask session.

#### `has_permission(user: sqlite3.Row, key: str) → bool`

```python
from bananawiki_sdk import has_permission

if has_permission(user, "my_plugin.view_stats"):
    # grant access
    ...
```

Returns `True` if the user has been granted the given permission key.
Works for both built-in permission keys (e.g. `"page.create"`) and custom
permission keys registered by plugins.

#### `is_plugin_enabled(plugin_id: str) → bool`

```python
from bananawiki_sdk import is_plugin_enabled

if is_plugin_enabled("badges"):
    # integrate with the badges plugin
    ...
```

Returns `True` if the plugin with the given ID is currently enabled.

#### `get_setting(key: str) → Any`

```python
from bananawiki_sdk import get_setting

site_name = get_setting("site_name")
```

Returns the value of a column from the `site_settings` table (single-row
table, always `id = 1`).  Returns `None` if the key does not exist.

### Flask helpers

| Symbol | Signature | Description |
|--------|-----------|-------------|
| `flash` | `flash(message, category="message")` | Add a flash message to the next response |
| `redirect` | `redirect(location, code=302)` | HTTP redirect response |
| `url_for` | `url_for(endpoint, **values)` | Build a URL for a named Flask endpoint |
| `render_template` | `render_template(template_name, **context)` | Render a Jinja2 template |

### Auth decorators

| Decorator | Description |
|-----------|-------------|
| `login_required` | Redirect to `/login` if no user is logged in |
| `editor_required` | Return 403 if the user is not an editor or admin |
| `admin_required` | Return 403 if the user is not an admin |

Apply these as route decorators after `@app.route(...)`:

```python
@app.route("/my-plugin")
@login_required
def my_plugin_index(): ...
```

### `rate_limit(max_requests: int = 60, window: int = 60)`

```python
from bananawiki_sdk import rate_limit

@app.route("/my-plugin/action", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window=60)
def my_action(): ...
```

Route decorator that enforces a per-IP rate limit.  When exceeded, returns a
`429 Too Many Requests` response.  The limit is backed by the database
(`rate_limit_hits` table), so it works correctly across multiple Gunicorn workers.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_requests` | `60` | Max requests allowed per IP in the time window |
| `window` | `60` | Time window in seconds |

### `log_action(action: str, request, user=None, **details) → None`

```python
from bananawiki_sdk import log_action
from flask import request

log_action("my_plugin.action", request, user=current_user, page_id=42)
```

Writes a structured log entry.  The `user` parameter accepts a user row or a
plain username string; only the username is written to the log.  Extra keyword
arguments are included as structured detail fields.  Whether a given action is
logged depends on the configured logging level (`off`, `minimal`, `medium`,
`verbose`, `debug`).

### `encrypt_value(value: str) → str`

```python
from bananawiki_sdk import encrypt_value

encrypted = encrypt_value("my_secret_data")
```

Encrypts a plaintext string using the instance's secret key.  Use this when
your plugin stores sensitive data in its own tables.

### `decrypt_value(value: str) → str`

```python
from bananawiki_sdk import decrypt_value

plaintext = decrypt_value(encrypted)
```

Decrypts a string that was previously encrypted with `encrypt_value()`.

## Exceptions

```python
from bananawiki_sdk import PluginError, PluginConfigError, PluginAPIVersionError
```

| Exception | Inherits from | When to use |
|-----------|---------------|-------------|
| `PluginError` | `Exception` | Base exception: general plugin failure |
| `PluginConfigError` | `PluginError` | Invalid or missing `plugin.json` manifest, or bad configuration |
| `PluginAPIVersionError` | `PluginError` | Plugin requires an SDK API version incompatible with the running instance |

The loader catches all exceptions during plugin loading and logs them without
crashing the application.

## `plugin.json` manifest reference

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

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Lowercase letters, digits, underscores. Must match the directory name. |
| `name` | Yes | Human-readable display name. |
| `version` | Yes | Semantic version string. |
| `author` | No | Author name. |
| `description` | No | One-sentence description. |
| `bananawiki_api_version` | No | Default: `"1.0"`. Major-version compatibility is checked at load time. |
| `builtin` | No | Must be `false` or absent for external plugins. |

## Further reading

- [`overview.md`](overview.md): plugin system architecture and built-in plugin catalogue
- [`authoring.md`](authoring.md): step-by-step guide to building your own plugin
- [`examples/hello_world/`](examples/hello_world/): minimal working example
- [`examples/page_word_count/`](examples/page_word_count/): realistic example with hooks, DB, and template slots
