# Architecture & Security

## Part 1: Architecture

### 1.1 Entry points

| File | Purpose |
|---|---|
| `app.py` | Flask application factory: creates the `app` object, registers middleware, hooks, security headers, template globals, and all route blueprints. |
| `wsgi.py` | Gunicorn WSGI entry point: imports the `app` object from `app.py`. |
| `gunicorn.conf.py` | Gunicorn worker and bind settings (workers, bind address/port). |

### 1.2 Request lifecycle

Every HTTP request passes through the following stages in order:

```
Client request
  │
  ├─ 1. ProxyFix          (if PROXY_MODE is enabled: trusts X-Forwarded-* headers)
  ├─ 2. CSP nonce          (_get_csp_nonce() generates a per-request random token,
  │                         stored on flask.g)
  ├─ 3. CSRF check         (Flask-WTF protects cookie-authenticated forms;
  │                         token-authenticated API views use explicit exemptions)
  ├─ 4. before_request_hook()
  │     ├─ Clear per-request caches
  │     ├─ Load site_settings from the database
  │     ├─ Redirect to /setup if setup_done is false
  │     ├─ Plugin path gating (disabled plugin → 404)
  │     ├─ Periodic mini-cleanup (every 5 minutes):
  │     │     • Cleanup expired suspensions
  │     │     • Cleanup all expired temporary items
  │     │     • Prune stale rate limit hits
  │     │     • Cleanup expired pending deletions
  │     │     • Revert expired public mode and open signup
  │     ├─ Maintenance mode enforcement (non-admins → /maintenance; admin login at /admin)
  │     ├─ Session limit enforcement (stale sessions → /session-conflict)
  │     ├─ Global rate limiting check (exceeded → 429)
  │     └─ Log request with current user
  │
  ├─ 5. Route handler      (matched route in routes/)
  │
  └─ 6. after_request: set_security_headers()
        (CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
         HSTS on HTTPS, Server header removed)
```

### 1.3 Route organization

Route handlers live in `routes/`, one file per feature area:

| File | Feature |
|---|---|
| `auth.py` | `/login`, `/logout`, `/signup`, `/setup`, `/maintenance` (`/lockdown` legacy redirect), `/session-conflict` |
| `wiki.py` | Wiki pages, categories, history, attachments, reservations, PDF export |
| `users.py` | User accounts, profiles, badge notifications, user data export |
| `admin.py`, `admin_*.py` | Administration registration and focused handlers for accounts, roles, sessions, settings, appearance, localization, migration, announcements, badges, checkouts, and contributions |
| `chat.py` | Direct messaging routes + chat cleanup scheduler |
| `groups.py` | Group chat routes (create, join, manage, moderate) |
| `kanban.py` | Kanban boards, columns, tickets, sharing, attachments, comments |
| `canvas.py` | Canvas layouts: visual knowledge boards with nodes and connections |
| `api.py` | JSON API (search, Markdown preview, drafts, reorder, accessibility) |
| `uploads.py` | Image/attachment upload and download, `cleanup_unused_uploads()` |
| `plugins.py` | Admin plugin management (enable, disable, import, delete) |
| `temporary.py` | Temporary pages, user accounts, and role grants |
| `custom_pages.py` | Custom page admin management + `try_serve_custom_page()` |
| `deletion_slowdown.py` | Deletion slowdown queue management |
| `api_service.py` | Scoped bearer-token REST API, userbot automation, and CSRF-protected token-management forms |
| `errors.py` | 400, 403, 404, 405, 413, 429, 500 error handlers |

New routes are registered inside a `register_*_routes(app)` function in
the relevant file, then called from `routes/__init__.py` →
`register_core_routes(app)`.

The [development guide](development.md) describes the administration and hosting
module boundaries and the shared lifecycle maintenance workflow.

### 1.4 Database layer

Application-table operations belong in the `db/` package. Migration and archive
code also opens separate SQLite snapshots to validate and transfer portable
data; these operations must preserve archive and transaction boundaries.

| File | Responsibility |
|---|---|
| `_connection.py` | `get_db()`: shared SQLite connection factory (WAL mode, foreign keys on, `Row` row factory) |
| `_schema.py` | `init_db()`: `CREATE TABLE` statements plus all `ALTER TABLE` migrations |
| `_users.py` | User CRUD, accessibility prefs, suspensions, login attempts, custom roles |
| `_invites.py` | Invite code generation and consumption |
| `_categories.py` | Category CRUD, tree building, drag-to-reorder, sequential nav |
| `_pages.py` | Page CRUD, history, search, attachments, difficulty tags, deindex, PDF export |
| `_drafts.py` | Draft autosave |
| `_settings.py` | Site-wide settings (single row, `id=1`); encrypts sensitive columns |
| `_announcements.py` | Announcement banners and contribution tracking |
| `_migration.py` | Full-site ZIP export/import (three conflict modes) |
| `_profiles.py` | User profiles and contribution heatmap |
| `_chats.py` | Direct messaging: messages, soft-delete, attachments, unread counts |
| `_groups.py` | Group chats: members, roles, timeouts, unread counts, cleanup |
| `_badges.py` | Badge types, user awards, auto-trigger logic |
| `_reservations.py` | Page checkout/reservation system with cooldowns |
| `_permissions.py` | Custom per-user permission set and category access rules |
| `_audit.py` | Role history, suspension history, custom user tags |
| `_kanban.py` | Kanban boards, columns, tickets, shares, attachments, history, comments |
| `_canvas.py` | Canvas layouts, nodes, connections, sharing |
| `_temporary.py` | Temporary pages, users, and role grants with expiry |
| `_custom_pages.py` | Custom pages with 19 content types |
| `_api_service.py` | API Service tokens, Userbot tokens, audit logging, and Banana Mode API |
| `_plugins.py` | Plugin registry (first-party + external) |
| `_deletion_slowdown.py` | Pending-deletion queue for the deletion slowdown plugin |
| `_wiki_docs.py` | `spawn_wiki_docs()`: seeds in-app user/admin documentation |
| `_cleanup.py` | Shared cleanup helpers (expired items, stale uploads, suspensions) |

`db/__init__.py` re-exports every public function so callers can do
`import db; db.get_page(slug)`.

Public signup commits account creation, invite redemption, role assignment, and
required approval state in one SQLite write transaction. Invite validity is
rechecked inside that transaction. Hosting follows the same rule and also
rechecks its current signup policy before committing an account.

**Schema migrations** use `PRAGMA table_info` + `ALTER TABLE … ADD COLUMN`
inside `db/_schema.py`. Migrations are idempotent and safe to re-run on
existing databases.

**Rows** are returned as `sqlite3.Row` objects; access columns by name:
`row["column_name"]`.

**Dynamic SQL columns** (in `_settings.py`, `_users.py`, `_kanban.py`) are
validated against explicit allowlists before interpolation to prevent SQL
injection.

### 1.5 Helpers

The `helpers/` package contains pure utility functions that do not depend on
Flask request context:

| File | Contents |
|---|---|
| `_constants.py` | `ALLOWED_TAGS`, `ALLOWED_ATTRS`, `ROLE_LABELS`, `_USERNAME_RE`, `_DUMMY_HASH` |
| `_rate_limiting.py` | Login and per-route rate limiting (DB-backed) |
| `_markdown.py` | `render_markdown(text, embed_videos=False)`, video embedding, `[[video]]` shortcodes |
| `_diff.py` | Diff computation for page history |
| `_text.py` | `slugify()` |
| `_validation.py` | `allowed_file()`, `allowed_attachment()`, `_is_valid_hex_color()`, `_is_valid_username()`, `_safe_referrer()` |
| `_auth.py` | `login_required`, `editor_required`, `admin_required`, `get_current_user()`, permission/access checks |
| `_permissions.py` | `PERMISSIONS` dict, `get_default_permissions()`, `get_all_permission_keys()` |
| `_time.py` | `get_site_timezone()`, `time_ago()`, `format_datetime()`, chat cleanup countdown helpers |
| `_favicon.py` | Favicon helpers and preset management |
| `_crypto.py` | `encrypt_value()`/`decrypt_value()`, `ENCRYPTED_SETTINGS_COLUMNS` |
| `_obsidian_sync.py` | Obsidian vault pull/push helpers (CLI-only, experimental) |
| `_request_cache.py` | Per-request memoization (`get_request_*`) |

### 1.6 Frontend

BananaWiki uses vanilla HTML + CSS + JavaScript, with no build step and no
framework.

| Path | Contents |
|---|---|
| `app/templates/` | Jinja2 templates organized by feature (`auth/`, `wiki/`, `admin/`, `account/`, `users/`, `chats/`, `groups/`, `kanban/`, `canvas/`) |
| `app/static/css/style.css` | All styles: "Industrial Theme" (steel/grey palette, dark and light modes) |
| `app/static/js/main.js` | All client-side JS: editor, sidebar, drafts, accessibility, canvas |
| `app/static/favicons/` | Eight preset banana-colour favicon variants |
| `app/static/uploads/` | Runtime user uploads (gitignored) |

All inline `<script>` and `<style>` tags in templates **must** carry
`nonce="{{ csp_nonce }}"` to comply with the Content Security Policy.

### 1.7 Plugin system

BananaWiki has a three-layer plugin architecture:

1. **Core**: built into the application; always active.
2. **First-party built-in plugins** (`plugins/builtin/`): ship with the
   codebase, tracked in the `plugins` DB table, and can be enabled/disabled
   from Admin → Plugins without a server restart.
3. **External plugins**: `.bwplugin` ZIP files imported via the admin UI.
   `plugin_loader.py` manages dynamic loading/unloading.

The folder decides which is which: code under `plugins/builtin/` is
built-in, code in the external plugins folder is external, whatever the
`builtin` column of the `plugins` table says.  An upload can never take the
id of a built-in or of an installed plugin.

An external plugin runs inside the wiki process with the wiki's privileges.
Once enabled it can read and change every table, read the secret key and act
as any user, the owner included.  Any admin can install one, so the admin
role is complete control of the wiki (see [User roles](#user-roles)).  The
admin pages say so on the upload form and before external code is enabled,
ask for the admin's password again to import, enable or delete an external
plugin, log each step, and copy the database before external code is
enabled.  `BW_ALLOW_EXTERNAL_PLUGINS=0` removes external plugins from a
self-hosted wiki.  [Plugins](plugins/overview.md#what-an-external-plugin-can-do)
has the details and the recovery steps.

The `bananawiki_sdk` package is the public API for plugin authors. It
exports:

- `Plugin`: base class with `on_load`, `on_enable`, `on_disable`, and
  `register_permission` lifecycle hooks.
- `hook()` / `emit_hook()`: decorator and dispatcher for event hooks.
- `template_slot()` / `render_slot()`: decorator and renderer for
  injecting content into template slots.
- `db_query()` / `db_execute()`: database helpers with guard rails
  (read-only, and no writes to core tables) enforced by SQLite's authorizer.
  They keep honest plugins from damaging core data by mistake; they are not
  a security boundary, since plugin code can import `db` itself.
- Re-exports of core helpers for plugin convenience.

Core hooks are wired in: `emit_hook("after_page_create/update/delete")` in
`routes/wiki.py`, `emit_hook("after_login")` in `routes/auth.py`,
`emit_hook("after_user_create")` in `routes/admin_accounts.py`. `render_slot()` is
registered as a Jinja2 global in `app.py`.

### 1.8 Plugin path gating

`_BUILTIN_PLUGIN_PATH_MATCHERS` in `app.py` maps each built-in plugin to
its URL patterns. When a plugin is disabled, any request matching its
patterns returns 404 immediately in `before_request_hook()`, and the route
handler is never called.  Routes nested under a page or a user are matched
on the segment after the slug, so a page named `history` or `tag` is never
caught by a disabled plugin's pattern.

Routes a plugin adds in its `on_load` (external plugins add theirs there)
are recorded by the loader, and `before_request_hook()` answers 404 for them
while the plugin is disabled or after it was deleted.  Request handlers the
plugin added in `on_load`, its hooks and its template slots also check the
plugin registry before they run.  Enabling or disabling a plugin runs its
callbacks in one Gunicorn worker only, but because every check reads the
shared registry, the other workers follow at their next request.

This lets admins toggle plugins on and off without restarting the server
and without leaving dead routes accessible.  Code a plugin has already run,
such as a thread it started, stops only when the wiki restarts.

---

## Part 2: Security

### 2.1 CSRF protection

All forms must include `{{ csrf_token() }}` or use Flask-WTF's automatic
injection. AJAX calls must send the token in the `X-CSRFToken` request
header.

The API Service exempts only views wrapped by its bearer-token authenticator.
Those views require token validity, scope and write permission, and exact
browser-origin matching when an Origin header is present. Their administrative
and token-management forms retain CSRF protection. The read-only health and
robots endpoints and the bounded, rate-limited CSP report receiver have explicit
exemptions in `app.py`.

### 2.2 HTML sanitisation

All user-generated Markdown is rendered through `render_markdown()` from
`helpers/_markdown.py`, which:

1. Converts Markdown to HTML using Python-Markdown with `tables`,
   `fenced_code`, `toc`, and `nl2br` extensions.
2. Passes the output through Bleach with `ALLOWED_TAGS` and
   `ALLOWED_ATTRS` (defined in `helpers/_constants.py`).
3. Applies `CSSSanitizer` for any permitted `style` attributes on elements
   like `img`, `figure`, and `div`.

When `embed_videos=True` (used on wiki page views), bare YouTube and Vimeo
URLs are auto-embedded as responsive iframes. The `[[video]]` shortcode
syntax provides additional control:

```
[[video url="https://www.youtube.com/watch?v=..." width="800" align="center" ratio="16:9"]]
```

Supported shortcode attributes: `url`, `width`, `align` (none/left/right/center),
`ratio` (16:9/4:3/1:1), `margin`, `autoplay`, `loop`, `controls`, `preload`.

**Never** mark raw user content as `Markup` or bypass sanitisation.

### 2.3 Upload security

SVG uploads are blocked because inline scripts in them are an XSS vector.
Image uploads are validated with Pillow to confirm they are genuine image
files rather than renamed executables, and `ALLOWED_EXTENSIONS` controls which
image types are accepted (default `png`, `jpg`, `jpeg`, `gif`, `webp`). Page
attachments, chat attachments, Kanban attachments, and custom page files are
stored outside `app/static/` and served through guarded routes, so they are
never directly accessible. Upload mode can be configured as `whitelist` or
`blacklist` in site settings, with configurable extension lists and per-file
size caps.

### 2.4 Rate limiting

Rate limiting uses the `rate_limit_hits` database table (columns: `ip`,
`bucket`, `hit_at`). The check-and-record operation uses
`BEGIN IMMEDIATE` for atomicity across Gunicorn workers.

Apply `@rate_limit(max_requests, window_seconds)` from
`helpers/_rate_limiting.py` to every mutation endpoint. Login brute-force
protection uses the separate `login_attempts` table, with automatic lockout
after too many failed attempts. Stale rate limit records are pruned in the
mini-cleanup cycle.

### 2.5 SQL injection prevention

All queries use parameterised `?` placeholders. Never write SQL strings using
f-strings or `%` formatting. Three modules build dynamic `UPDATE` statements
(`db/_settings.py`, `db/_users.py`, `db/_kanban.py`); all three validate column
names against explicit allowlists before interpolation.

### 2.6 Response headers

`app.py → set_security_headers()` sets the following headers on every
response:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'; connect-src 'self' stun: turn: turns:; media-src 'self' blob: mediastream:; frame-src https://www.youtube.com https://player.vimeo.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'; report-uri /api/csp-report; report-to csp-endpoint` |
| `Reporting-Endpoints` | `csp-endpoint="/api/csp-report"` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `SAMEORIGIN` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (HTTPS only) |

Custom-page routes can widen `frame-src` for a single response through the
internal `X-Frame-Src-Override` header, whose values are validated as bare
`https://host[:port]` origins, and can prepend `sandbox allow-scripts` through
`X-Custom-Page-Sandbox`.

The `Server` header is removed from all responses.

### 2.7 Authentication

- Passwords are hashed through `helpers/_passwords.py`, which wraps
  Werkzeug hashing and falls back to `pbkdf2:sha256` on Python/OpenSSL builds
  without `hashlib.scrypt`. Plain-text passwords are **never** stored.
- Against timing attacks, login checks always run `check_password_hash`
  against `_DUMMY_HASH` (defined in `helpers/_constants.py`) even when the
  username does not exist, which keeps the comparison constant-time and
  prevents username enumeration.
- `session["user_id"]` holds the logged-in user's ID.
- `get_current_user()` from `helpers/_auth.py` returns the full user row or
  `None`.

### 2.8 Session security

BananaWiki defines `_AutoSecureSessionInterface` (a
`SecureCookieSessionInterface` subclass) that sets the `Secure` cookie flag
dynamically based on `request.is_secure`. This lets the same app work
over HTTP (dev/LAN) and HTTPS (production) without reconfiguration.

Session cookie attributes:

| Attribute | Value |
|---|---|
| `HttpOnly` | `True` |
| `SameSite` | `Lax` |
| `Secure` | Dynamic (auto-detects HTTPS) |
| `Name` | Configurable (`bw_session` by default) |
| Lifetime | 7 days (`permanent_session_lifetime`) |

### 2.9 Session limit

The session limit is disabled by default. When admins enable
`session_limit_enabled` from Site Settings, each login stores a
`session_token` in the `users` table. Any existing session whose token does
not match is invalidated and redirected to `/session-conflict`. This
enforces one active session per user.

### 2.10 Maintenance mode

When `maintenance_mode` is enabled in site settings, all non-admin users
are immediately kicked out and redirected to `/maintenance`.  The
configurable maintenance message is displayed on the maintenance page.
Admins can continue to use the site normally and sign in from the
dedicated `/admin` page; the legacy `/lockdown` URL permanently
redirects to `/maintenance` for backwards compatibility.

### 2.11 Safe redirects

When redirecting based on user-supplied URLs (e.g. the `Referer` header),
always use `_safe_referrer()` from `helpers/_validation.py`. It validates
that the target URL is same-origin to prevent open redirect attacks.

### 2.12 Storage layout

User-uploaded files are stored in separate, purpose-specific directories:

| Directory | Contents | Access |
|---|---|---|
| `app/static/uploads/` | Image uploads (wiki pages) | Public (served by Flask/nginx) |
| `instance/attachments/` | Page file attachments | Authenticated route |
| `instance/chat_attachments/` | Chat file attachments | Authenticated route |
| `instance/kanban_attachments/` | Kanban ticket attachments | Authenticated route |
| `instance/custom_page_files/` | Custom page binary files | Guarded route (`/_cpf/`) |

### 2.13 Settings encryption

Sensitive `site_settings` columns are transparently encrypted at rest using
Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from the Flask
secret key via PBKDF2-SHA256 (100,000 iterations).

`ENCRYPTED_SETTINGS_COLUMNS` in `helpers/_crypto.py` lists the encrypted
columns.

`db/_settings.py` calls `encrypt_value()` / `decrypt_value()` automatically.
Callers always work with plaintext. **Never** store secrets in
`site_settings` columns that are not in `ENCRYPTED_SETTINGS_COLUMNS`.

### 2.14 ZIP bomb protection

ZIP imports (migration and plugin) are protected against ZIP-bomb attacks
with configurable size limits:

| Config | Default | Purpose |
|---|---|---|
| `MAX_IMPORT_UNCOMPRESSED_SIZE` | 500 MB | Max total uncompressed size for migration ZIP |
| `MAX_IMPORT_MEMBER_SIZE` | 200 MB | Max uncompressed size for any single file in the ZIP |
| `MAX_PLUGIN_UNCOMPRESSED_SIZE` | 50 MB | Max total uncompressed size for `.bwplugin` ZIP |

These limits are enforced before extraction begins.

### 2.15 Path traversal defence

File operations that handle user-supplied filenames use
`os.path.commonpath()` validation to ensure the resolved path stays within
the expected directory. This prevents `../` traversal attacks on upload,
download, and attachment routes.

## User roles

BananaWiki has four built-in tiers plus support for custom roles:

| Role | Constant | Description |
|---|---|---|
| Member | `"user"` | Read-only access (default for new users) |
| Editor | `"editor"` | Read + create/edit pages, manage categories (deleting pages needs `page.delete`) |
| Administrator | `"admin"` | Full access + user management, settings, announcements |
| Protected Admin | `"owner"` | Same as admin; other admins cannot edit, demote, suspend or delete it through the admin interface |

Admins are fully trusted.  Any admin can install a plugin or import a
full-site backup, and either gives complete control of the wiki: an admin
who does so can make themselves owner or superuser and change any account.
Owner status protects an account from being demoted or deleted through the
normal interface, not from an admin who installs code or imports a backup.
Give the admin role only to people you would trust with everything.  The
pages that hand over that control ask for the admin's password again and
log the action: full-site export (it contains every password hash),
full-site import, plugin import, and enabling or deleting an external
plugin.

**Custom roles** can be created by admins at Admin → Custom Roles. Each
custom role defines its own set of permissions and category access rules.
Users assigned a custom role inherit its permissions in addition to their
base role permissions.

## User suspension

Admins can suspend non-protected-admin accounts in two ways. A permanent
suspension sets `suspended = 1` and `suspended_until = NULL`. A timed
suspension sets `suspended = 1` and `suspended_until` to a future UTC ISO
datetime.

`db.is_suspension_active(user)` checks both cases.
`db.check_suspension_expired(user_id)` auto-unsuspends when a timed
suspension has elapsed. Suspension history is recorded in the `audit` table.

## Logging

User actions are logged with `log_action(action, request, user=user, **kwargs)`
from `wiki_logger`. Five log levels control verbosity:

| Level | What is logged |
|---|---|
| `off` | Nothing |
| `minimal` | Critical events only |
| `medium` | Critical + important auth/admin actions |
| `verbose` | All user actions (default) |
| `debug` | All above + HTTP request details |

Logs are written to `logs/bananawiki.log` (configurable via `BW_LOG_FILE`).

## Further reading

- [Getting Started](getting-started.md): installation and first steps.
- [Configuration](configuration.md): config reference.
- [Permissions](permissions.md): roles, permissions, and category access.
- [Plugins](plugins/overview.md): managing and authoring plugins.
