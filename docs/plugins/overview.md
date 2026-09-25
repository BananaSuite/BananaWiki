# Plugin System Overview

> SDK version `1.0.0` · API version `1.0`

BananaWiki has a plugin architecture that lets administrators enable only the
features they need and lets third parties add functionality without touching
core code.

## Architecture

The plugin system has three layers:

| Layer | Location | Description |
|-------|----------|-------------|
| **Core** | `routes/`, `db/`, `helpers/` | Auth, wiki pages, categories, admin settings, security. Always active. |
| **First-party plugins** | `plugins/builtin/<plugin_id>/` | Optional features that ship with BananaWiki (19 plugins). Pre-installed; disabled by default on fresh installs. |
| **External plugins** | `plugins/external/<plugin_id>/` (or `BW_EXTERNAL_PLUGINS_DIR`) | `.bwplugin` ZIP files authored by third parties. Imported and managed through the admin UI. |

Where the code lives decides what a plugin is.  Anything under
`plugins/builtin/` is built-in; anything found in the external plugins
folder is external, whatever the `builtin` column of the `plugins` table
says.  That column only mirrors the folder for the admin pages and is
corrected at every start.

## What an external plugin can do

An external plugin is Python code that runs inside the wiki process, with
the wiki's own privileges.  Once enabled it can read and change every table
(accounts, roles, password hashes, private messages), read the secret key
and therefore sign sessions as any user, read environment variables, make
network requests, and change how any page behaves.  The SDK's helpers and
the checks described below are there to keep honest plugins from making
mistakes; none of them contains a plugin that means harm.

Any admin can import and enable plugins, and importing a full-site backup
(Admin → Migration) gives the same complete control.  Give the admin role
only to people you would trust with everything.  The owner role protects an
account from being demoted or deleted through the normal interface; it does
not protect it from an admin who installs code or imports a backup.

Because of that, the admin pages:

- say so on the import form and again before external code is enabled;
- ask for the admin's password again before a plugin is imported, before
  external code is enabled, and before an external plugin is deleted;
- record each of these steps in the application log (`plugin_imported`,
  `plugin_code_trusted`, `plugin_enabled`, `plugin_deleted`, and
  `plugin_password_rejected` for a wrong password);
- take a copy of the database before external code is enabled and before a
  plugin's data is dropped (see
  [Recovering after a bad plugin](#recovering-after-a-bad-plugin)).

A self-hosted wiki that should never run third-party code can set
`BW_ALLOW_EXTERNAL_PLUGINS=0`: the upload form disappears and external
folders are ignored.  On managed hosting external plugins are only allowed
inside the per-tenant container (`BW_PLUGIN_ISOLATION=container`).

The `bananawiki_sdk` package is the stable, versioned public API that all
plugins (both first-party and external) must use to interact with the core.
See [`authoring.md`](authoring.md) for a complete walkthrough and
[`api-reference.md`](api-reference.md) for the symbol reference.

## Plugin lifecycle

```
Discover → Validate manifest → Check API version → Load module → on_load(app)
```

On server startup, `plugin_loader.py` scans `plugins/builtin/` and
`plugins/external/` in alphabetical order, seeds new built-in plugins into the
database, and calls `plugin.on_load(app)` for each **enabled** plugin.

When an admin toggles a plugin via **Admin → Plugins**, the change takes effect
immediately and needs no server restart.

On enable, the module is imported on demand (if not already loaded),
`on_load(app)` runs so its routes become active, then `on_enable()` fires.
That happens in the Gunicorn worker that handled the admin's request.  Every
other worker compares its loaded plugins with the registry at the start of
each request and loads the plugin there too, without calling `on_enable()`
again.

On disable, `on_disable()` fires in that one worker and the plugin's hooks
and template slots are set aside.  In every worker, the plugin then stops:

- its hooks and template slots are skipped, because both check the registry
  before they run;
- its routes answer 404: built-in routes through the path matchers in
  `app.py` (see [Plugin path gating](#plugin-path-gating)), and any route a
  plugin added in `on_load` through the list of endpoints recorded while
  `on_load` ran;
- request handlers it added in `on_load` (`before_request`,
  `after_request`, teardown handlers, context processors) do nothing, and
  error handlers it added there step aside for the ones they replaced.

Flask cannot remove a route from a running application, and nothing can
unload code that has already run.  A thread the plugin started, or a core
function it replaced, keeps going until the wiki restarts.  Restart the wiki
after disabling or deleting a plugin you no longer trust.

Disabling a plugin never deletes data. Re-enabling restores full
functionality.

## First-party plugins

BananaWiki ships with 19 built-in plugins, one directory each under
`plugins/builtin/`. The Path gating column says whether `app.py` blocks the
plugin's URL paths while the plugin is disabled (see
[Plugin path gating](#plugin-path-gating)).

A manifest may set `"experimental": true`, in which case the plugin is not
seeded, so it does not appear in Admin → Plugins and never loads. None of the
shipped plugins uses it, so a new installation lists all 19. Its id stays
reserved like every built-in id: an uploaded plugin cannot use it. What was
taken out before release, and why, is in [MIGRATION.md](../../MIGRATION.md).

| Plugin ID | Name | Description | Path gating |
|-----------|------|-------------|-------------|
| `announcements` | Announcements | Site-wide notification banners | Yes |
| `api_service` | API Service | REST API service with token authentication, audit logging, bulk operations, and admin controls | Yes |
| `assessments` | Assessments | Page-level polls and tests with points, attempt control, and admin results | Yes |
| `attachments` | Attachments | File attachments on wiki pages | Yes |
| `audit` | Audit | Role change history, custom user tags, and contribution tracking | Yes |
| `badges` | Badges | Achievement badges with auto-trigger support | Yes |
| `canvas` | Canvas | Visual canvas layouts with text, images, videos, and wiki page links | Yes |
| `chat` | Chats and Groups | Direct messages, group chat spaces, attachments, moderation, and scheduled cleanup | Yes |
| `custom_pages` | Custom Pages | Admin-created pages at arbitrary URL paths with 16 content types | Yes |
| `deletion_slowdown` | Deletion Slowdown | 48-hour grace period before permanent page deletion, with restore from Admin → Pending Deletions | Yes |
| `difficulty_tags` | Difficulty Tags | Label wiki pages with difficulty levels and custom tags | Yes |
| `drafts` | Drafts | Draft autosave while editing | Yes |
| `kanban` | Kanban Board | Kanban boards, columns, tickets, and comments with drag-and-drop | Yes |
| `page_governance` | Page Governance | Page reservations, page protection, and contribution approval | Yes |
| `page_history` | Page History | Revision history with diff viewing and page revert | Yes |
| `temporary_accounts` | Temporary Accounts & Pages | Time-limited pages, user accounts, and role grants on a configurable schedule | Yes |
| `tts` | Text-to-Speech | On-demand local audio narrations of wiki pages using Piper neural voices, deduplicated in flight and invalidated when the page changes | Yes |
| `user_data_export` | User Data Export | Download personal data as a ZIP archive | Yes |
| `user_profiles` | User Profiles | Public profile pages with contribution heatmap | Yes |

## Plugin path gating

When a built-in plugin with routes is disabled, its URL paths are blocked at
the `before_request` level in `app.py` by `_BUILTIN_PLUGIN_PATH_MATCHERS`. Any
request to a disabled plugin's route returns 404 immediately, before the route
handler is invoked.

Routes nested under a page or a user, such as `/page/<slug>/history` or
`/admin/users/<id>/audit`, are matched on the segment after the slug, never
on the slug itself.  A page called `history-of-rome` or `tag` therefore stays
reachable while Page History or Difficulty Tags is disabled.

The guard stops users reaching disabled plugin features by direct URL without
removing route registrations from Flask. It runs on every request, so
disabling a plugin takes effect instantly.

Routes a plugin registers in `on_load`, which is how external plugins add
theirs, get a second guard: the loader records the endpoints `on_load`
added, and `app.py` answers 404 for them whenever the plugin is not enabled
in the registry or has been deleted.  The endpoints and request handlers of
a deleted plugin stay closed until the next restart, even if a new copy is
installed under the same id: a worker that still holds the old code,
loaded or set aside after a disable, drops it as soon as it sees the
plugin's registry row gone or replaced, and imports the new copy when it is
enabled.
The new copy cannot register the same routes again before a restart, so
restart the wiki before installing another version of a plugin.


## Checking whether a plugin is active

### From your own plugin

```python
from bananawiki_sdk import is_plugin_enabled

if is_plugin_enabled("badges"):
    # badges-specific logic
    ...
```

This pattern is useful when your plugin provides optional integration with
another plugin. Use it rather than importing that plugin's code directly; the
target plugin may not be installed on every instance.

### From core code or templates

```python
import db

if db.is_plugin_enabled("chat"):
    # chat-specific logic
    ...
```

In Jinja2 templates, the context processor injects the `enabled_plugins`
dict:

```html
{% if enabled_plugins.get("badges") %}
    <p>Badges are enabled!</p>
{% endif %}
```

## Managing plugins via the admin UI

### Enabling / disabling

1. Navigate to **Admin → Plugins**.
2. Click **Enable** or **Disable** next to the plugin name.

The change takes effect immediately and needs no server restart.

Enabling a built-in plugin needs nothing more.  Enabling an external plugin,
or enabling a plugin together with external dependencies, first shows a page
that explains what the code will be able to do and asks for your password.
This page is the only way to switch external code on: the feature choices in
onboarding only cover built-in plugins, and the loader runs a plugin's code
only while its row in the plugin registry is enabled.

### Importing an external plugin

1. Obtain a `.bwplugin` file from the plugin author, and read its code.
2. Go to **Admin → Plugins → Import External Plugin**.
3. Choose the file, enter your password and upload.  Before anything is
   written, the manifest is checked (text fields must be short strings),
   the SDK API version must match, and the id must be new: an upload cannot
   use the id of a built-in plugin, a name a built-in uses for its tables
   (`user_profile_fields`), or the id of anything already installed,
   compared without regard to case, and cannot contain `__`.  An uploaded
   manifest cannot declare itself built-in.
4. The files are unpacked into a hidden staging folder and the plugin is
   registered as disabled; only then does the folder get its real name.  No
   plugin code runs at this point.  Review its contents, then enable it when
   ready.

A crash in the middle of an upload can leave a hidden `.staging-*` folder in
the external plugins folder.  Discovery ignores every name that starts with
a dot, and the next start removes staging folders older than an hour.

### Deleting / uninstalling a plugin

| Type | What happens |
|------|-------------|
| **External plugin** | A confirmation page asks for your password. The plugin directory (`plugins/external/<id>/`) and its registry row are deleted. Its data is kept unless you tick **Also delete the plugin's data**, which lists the exact tables that will be dropped. When the box is ticked, a copy of the database is taken first, and nothing is deleted if the copy fails. |
| **Built-in plugin** | Only the database row is removed; plugin files (part of the BananaWiki distribution) are preserved on disk. The plugin reappears as *disabled* on the next server restart. Its data is never dropped. |

A table belongs to an external plugin when its name starts with exactly
`<plugin_id>__`, case included, it is not a core table, and no other plugin
with a longer id has its own `<id>__` prefix at the start of the name (so
`foo___data` belongs to `foo_`, not to `foo`).  Only tables that were listed
on the confirmation page and still match those rules are dropped.  A dropped
table can only be recovered from a backup.

### Recovering after a bad plugin

Each time an external plugin is enabled, before its code runs, BananaWiki
copies the database to `plugin_safety_snapshots/<UTC time>-<plugin id>.db`
inside the instance directory (`BW_INSTANCE_DIR`, `instance/` by default).
It does the same before it drops a plugin's tables.  The five newest copies
are kept.  If the copy cannot be made, the plugin is not enabled and no
table is dropped.

The copy covers the database only.  Uploaded files, attachments and the
secret key are not in it, and a plugin that ran could have changed or read
them.  To go back on a self-hosted wiki:

1. Stop the wiki (for example `sudo systemctl stop bananawiki`).
2. Delete the plugin's folder from the external plugins folder
   (`plugins/external/`, or `BW_EXTERNAL_PLUGINS_DIR` if set; `banana install`
   uses `data/plugins`), so it cannot load again.
3. Move the current database aside: `bananawiki.db` and, if present,
   `bananawiki.db-wal` and `bananawiki.db-shm`.
4. Copy the snapshot to the database path (`BW_DATABASE_PATH`, by default
   `bananawiki.db` in the instance directory) and make sure the service
   account owns it and only it can read it (`chmod 600`).
5. Start the wiki.

Everything written to the database after the snapshot is lost, so export
anything you want to keep first.  If you do not trust what the plugin did,
also restore uploaded files from a backup, and replace `.secret_key`: every
session is signed out, and encrypted settings (such as stored API keys) must
be entered again.

On managed hosting the wiki still makes this copy, but it lands in the
tenant's own data folder, where an enabled plugin can also write, so the
hosting portal never restores from it.  The operator restores, from the
instance's page in the hosting portal, a snapshot the platform took itself
and keeps outside the tenant's folder.

## Further reading

- [`authoring.md`](authoring.md): step-by-step guide to building your own plugin
- [`api-reference.md`](api-reference.md): `bananawiki_sdk` symbol reference
- [`examples/hello_world/`](examples/hello_world/): minimal working example
- [`examples/page_word_count/`](examples/page_word_count/): realistic example with hooks, DB, and template slots
