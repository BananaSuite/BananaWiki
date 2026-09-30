# Plugins

BananaWiki is built from *features*: one package per feature under
`bananawiki/wiki/features/`, each declaring a `Feature` (see
[ARCHITECTURE.md](../../ARCHITECTURE.md)). A third-party plugin is exactly that, installed outside
the source tree. It uses the same API as the built-in features: blueprints,
navigation entries, background jobs, events, template slots, interceptors,
translations and templates.

## What a plugin can do

A plugin runs inside the wiki process with the wiki's own privileges. Once
enabled it can read and change every table (accounts, password hashes,
private messages), read the secret key and therefore sign in as anyone, read
environment variables and make network requests. The checks described here
keep honest plugins from making mistakes; none of them contains a plugin
that means harm. Install only code you have read and trust, and give the
administrator role only to people you would trust with everything: any
administrator can install plugins.

For that reason the admin pages

* say so on the upload form and again before a plugin is enabled;
* ask for the administrator's own password before installing, enabling or
  deleting a plugin (while impersonating, the impersonator's password);
* copy the database to `<instance>/plugin_safety_snapshots/` before a plugin
  is enabled and before its tables are dropped (the five newest copies are
  kept) and refuse to go on when the copy fails;
* log every step (`plugin_imported`, `plugin_code_trusted`,
  `plugin_enabled`, `plugin_disabled`, `plugin_deleted`,
  `plugin_password_rejected`) under the `bananawiki.plugins` logger.

Operators decide whether third-party code may run at all:

| Variable | Effect |
|---|---|
| `BW_ALLOW_EXTERNAL_PLUGINS` | `0` turns uploads off and loads no plugin. Default on for self-hosting. |
| `BW_MANAGED_HOSTING` | Under managed hosting plugins are off unless `BW_PLUGIN_ISOLATION=container`. |
| `BW_MANAGED_PLUGIN_DENYLIST` | Comma-separated ids that never load (features and plugins alike). |
| `BW_EXTERNAL_PLUGINS_DIR` | Where plugins live; default `<instance>/plugins`. |
| `BW_EASY_WIKI` | Features and plugins with `easy_wiki=False` are hidden and stay off. |

## Quick start

1. Copy `bananawiki/wiki/features/plugin_manager/examples/hello_plugin/` to a
   new folder named after your plugin id and rename the id in `plugin.json`,
   `__init__.py`, the blueprint, the translation keys and the table names.
2. Develop against a local wiki (`bananawiki serve`): put the folder into
   `<instance>/plugins/`, restart, enable it under **Admin → Plugins**, and
   restart again (plugins load at start-up).
3. Use the extension points of [ARCHITECTURE.md](../../ARCHITECTURE.md):
   events (`page.created`, `user.login` …), template slots
   (`page.below_content`, `sidebar.bottom` …), interceptors, background jobs
   and navigation entries.
4. Write tests with the fixtures in `tests/conftest.py` if you keep the plugin
   in a fork, or with Flask's test client against `create_app()`.
5. Package it as a `.bwplugin` (see [packaging](#packaging)) and install it on
   other wikis through the upload form.

## Layout

```
<plugins folder>/hello_plugin/
├── plugin.json              manifest (required)
├── __init__.py              defines FEATURE (required)
├── routes.py                blueprints, views, event handlers
├── schema.py                optional: upgrade(conn) creates the plugin's tables
├── templates/hello_plugin/  Jinja templates (extend "base.html")
├── static/                  CSS/JS served at /static/<blueprint>/…
└── translations/en.json, it.json
```

A complete example is in
`bananawiki/wiki/features/plugin_manager/examples/hello_plugin/`; administrators can also
download it with this guide from **Admin → Plugins → Download the plugin kit**.

### plugin.json

```json
{
  "id": "hello_plugin",
  "name": "Hello Plugin",
  "version": "1.0.0",
  "author": "Your name",
  "description": "One sentence shown in the plugin list.",
  "min_bananawiki": "1.6.0",
  "requires": []
}
```

| Field | Rules |
|---|---|
| `id` | Required. Starts with a letter; letters, digits, `_`, `-`; at most 64 characters; never `__`. The folder must have this name. It may not be the id of a built-in feature, a 1.4 built-in or retired plugin, or a reserved name, compared without regard to case. |
| `name`, `version` | Required text, at most 100 and 50 characters. |
| `author`, `description` | Optional text, at most 200 and 2000 characters. |
| `min_bananawiki` | Optional. The plugin is not loaded on older releases. |
| `requires` | Optional list of feature or plugin ids. Enabling offers to enable them too; disabling one of them offers to disable its dependents. (`extends` from 1.4 means the same.) |
| `builtin` | Must be absent or `false`. |

### `__init__.py`

```python
from bananawiki.wiki.registry import Feature, NavItem
from .routes import bp, count_page

FEATURE = Feature(
    id="hello_plugin",                 # must equal plugin.json's id
    name="hello_plugin.name",          # translation keys
    description="hello_plugin.description",
    blueprints=[bp],
    nav=[NavItem("hello_plugin.nav", "hello_plugin.index", icon="smile", area="apps")],
    events={"page.created": [count_page]},
)
```

The loader always treats a plugin as `toggle="plugin"`: its row in the
`plugins` table (`builtin = 0`) is its switch, and without a row it is off.
Every blueprint of a plugin answers 404 while the plugin is off, whether or
not it was built with `registry.feature_blueprint`. Views are private by
default, like every view of the wiki; check roles and permissions yourself
(`auth.admin_required`, `auth.has_permission`, object-level checks). Views
must follow the rules in [ARCHITECTURE.md](../../ARCHITECTURE.md): POST for changes (CSRF is
checked for you), no inline scripts, translations in `en` and `it`.

`init_app(app)`, when given, runs once at start-up **before** the plugin's
blueprints are registered.

### Tables

Every table, index, trigger and view a plugin creates must be named
`<plugin_id>__…`. Create them in `schema.py`:

```python
def upgrade(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS hello_plugin__counts (name TEXT PRIMARY KEY, value INTEGER)")
```

`upgrade` runs at every start in one transaction, so it must be idempotent.
SQLite's authorizer refuses anything else it tries to create or change
(core tables, other names, `ATTACH`, pragmas other than schema
introspection); the whole upgrade is then rolled back and the plugin is not
loaded. At run time use `bananawiki.wiki.db.db` like the built-in features.
When an administrator deletes a plugin they may also drop its tables: those
named exactly `<plugin_id>__…` (case included) that no plugin with a longer
id owns.

## Lifecycle

Plugins are loaded once, when the wiki starts. There is no hot loading or
unloading: Python cannot really unload code, and 1.4's attempt to fake it
was over a thousand lines that still left threads and patches behind.

| Action | Effect |
|---|---|
| Install (upload a `.bwplugin`) | Files unpacked, row added **disabled**. No code runs. |
| Copy a folder into the plugins folder | Registered disabled at the next start. |
| Enable | Password, database copy, row enabled. The code loads at the **next restart**. |
| Disable | Row disabled. Its pages, navigation, slots, events, interceptors and jobs stop **at once** in every worker; the code leaves memory at the next restart. |
| Delete | Password; folder and row removed; tables dropped only if ticked. |

The plugin page shows what waits for a restart. Under Gunicorn without
`preload_app` it offers **Restart the wiki now**, which sends `SIGHUP` to the
Gunicorn master so it replaces its workers, and the new workers load the
plugins. When the application is preloaded in the master, or the wiki does
not run under Gunicorn, the page explains that the service has to be
restarted (`banana restart`, `systemctl restart bananawiki`, or the hosting
portal).

A plugin that fails to import or to set up is skipped with its error shown
on the plugin page; the wiki starts anyway.

## Packaging

A `.bwplugin` file is a ZIP archive with `plugin.json` and `__init__.py`
either at its root or inside one top-level folder:

```bash
cd hello_plugin && zip -r ../hello_plugin.bwplugin . -x '__pycache__/*'
```

Uploads are refused when the archive

* is not a ZIP or is larger than 50 MB;
* has absolute paths, `..`, links, device files, encrypted or duplicate members;
* holds compiled code (`.so`, `.pyd`, `.dll`, `.dylib`, `.exe`, `.pyc`,
  `.pyo`), which administrators cannot review;
* has more than 5,000 entries, a file over 10 MB, more than 50 MB unpacked
  or a member compressed more than 200 times;
* has no or several `plugin.json`, files outside the plugin's folder, no
  `__init__.py`, or an invalid manifest;
* uses an id that is reserved, denied or already installed.

The files are unpacked into a hidden `.staging-*` folder, the row is
registered, and only then does the folder get its real name. Staging
folders a crash left behind are removed at the next start.

## Plugins written for BananaWiki 1.4

1.4 plugins (`from bananawiki_sdk import Plugin, hook, template_slot,
db_query, db_execute`) still load. The loader makes `bananawiki_sdk` resolve
to `bananawiki.sdk.compat` (a `bananawiki_sdk` package at the top of the
source tree does the same for plugin authors' own tests), imports the plugin,
runs `on_load(app)` once at start-up and turns the result into a `Feature`:

| 1.4 | 1.6 |
|---|---|
| `plugin.json` with `bananawiki_api_version` 1.4 | accepted; `extends` means `requires` |
| `@plugin.on_load` | runs once at start-up; routes added with `@app.route` or `app.register_blueprint` answer 404 while the plugin is off |
| `@hook("after_page_create")` / `after_page_update` / `after_page_delete` | `page.created` / `page.updated` / `page.deleted`, called with `page=` and `user=` (the author or actor) |
| `@hook("after_login")`, `@hook("after_user_create")` | `user.login`, `user.created` |
| `@hook("<anything else>")`, `emit_hook(...)` | an event of the same name |
| `@template_slot(name)` renderers `fn(context)` | slots of the same name; the context has `user`, `csp_nonce` and the slot's own values (`page` …). Several renderers are concatenated. |
| `db_query`, `db_execute` | one statement each; `db_query` is read-only, `db_execute` may only write tables named `<plugin_id>__…` of an installed plugin (1.4 allowed any non-core table). Rows are dictionaries that also accept integer indexes. |
| `add_admin_menu_item(label, endpoint)` | an admin menu entry |
| `register_permission(key, …)` | `has_permission(user, key)` answers from the declared role defaults (administrators always) |
| `get_current_user`, `has_permission`, `is_plugin_enabled`, `get_setting`, `flash`, `redirect`, `url_for`, `render_template`, `rate_limit`, `login_required`, `admin_required`, `editor_required`, `log_action`, `encrypt_value`, `decrypt_value` | the 1.6 equivalents |
| `@plugin.on_disable` | runs in the worker that handles the administrator's click |

Not supported:

* `on_enable` when the plugin's code is not loaded yet: enabling takes
  effect at the next restart, so do set-up in `on_load`.
* Granting a plugin permission to individual users (only role defaults).
* The `after_impersonate_*` hooks.
* Switching off request handlers (`before_request`, error handlers,
  context processors) that `on_load` added to the application: they stay
  active until the restart after the plugin is disabled.
* Templates that rely on 1.4 context variables (`settings`,
  `enabled_plugins`) or on `render_slot(...) | safe` in core templates.
* Hooks and slots registered after loading (from a request or a thread)
  are ignored with a warning.

The 1.4 examples in [examples/](examples/) (`hello_world`,
`page_word_count`) load through the adapter unchanged.

## Retired 1.4 plugins

Rows of plugins that no longer exist (`banana_ai`, `feedback`, `meetings`,
`beta_testers`, `git_override`, `oauth_login`, `bw_oauth_provider`,
`file_manager`, `banana_cad`, `ea_mode`, `lab_camera`, `easter_egg`, and any
1.4 built-in without a 1.6 feature) are listed as *retired (inert)*. Nothing
under those ids is ever loaded, their data stays in the database, and
**Remove** deletes the row.

## Sidebar order

The plugin page also orders the sidebar apps (`site_settings.sidebar_apps_order`,
a comma-separated list of feature ids). Drag the entries or use the arrow
buttons, which also work without JavaScript.
