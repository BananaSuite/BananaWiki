# Configuration


BananaWiki uses a two-layer configuration model: **static settings** in
`config.py` (read at server start) and **runtime settings** stored in the
SQLite database (editable from Admin → Site Settings, effective
immediately).

---

## Layer 1: Static configuration (`config.py`)

Most static settings support `BW_*` environment variable overrides. The
hosting platform uses these to isolate per-instance configuration without
modifying the file.

### Networking

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `PORT` | `BW_PORT` | `5001` | Gunicorn bind port |
| `HOST` | `BW_HOST` | `"127.0.0.1"` | Gunicorn bind address |
| `PROXY_MODE` | `BW_PROXY_MODE` | `False` | Enable `ProxyFix` middleware for nginx / reverse proxy.  **Default flipped to off after the security audit**: production deployments behind nginx must set `BW_PROXY_MODE=1` explicitly (`banana install --domain ...` configures this in the private environment file). |

### Security

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | `SECRET_KEY` | Auto-generated, persisted to `instance/.secret_key` | Flask secret key for session signing |
| `SESSION_COOKIE_NAME` | `BW_SESSION_COOKIE_NAME` | `"bw_session"` | Session cookie identifier |

BananaWiki generates the secret key on first run, saves it to
`instance/.secret_key` and reloads it on later starts. If the
`SECRET_KEY` environment variable is set, it takes precedence.

### Database

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `DATABASE_PATH` | `BW_DATABASE_PATH` | `bananawiki.db` under the instance directory | SQLite database file location |

The database operates in WAL (Write-Ahead Logging) mode for concurrent
read performance.

### Image uploads

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `UPLOAD_FOLDER` | `BW_UPLOAD_FOLDER` | `app/static/uploads` | Directory for user image uploads |
| `MAX_CONTENT_LENGTH` | `BW_MAX_CONTENT_LENGTH_BYTES` | `16 MB` (16,777,216 bytes) | Maximum upload size (Flask-enforced) |
| `ALLOWED_EXTENSIONS` | - | `png, jpg, jpeg, gif, webp` | Permitted image file types (SVG intentionally excluded) |

### Page attachments

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `ATTACHMENT_FOLDER` | `BW_ATTACHMENT_FOLDER` | `instance/attachments` | Page attachment storage directory |
| `MAX_ATTACHMENT_SIZE` | `BW_MAX_ATTACHMENT_SIZE_BYTES` | `5 MB` (5,242,880 bytes) | Per-attachment size cap |
| `ATTACHMENT_ALLOWED_EXTENSIONS` | - | `pdf, doc, docx, xls, xlsx, ppt, pptx, txt, md, csv, json, xml, zip, tar, gz, png, jpg, jpeg, gif, webp, mp4, webm, mp3, ogg, py, js, ts, html, css, sh` | Permitted attachment file types |

### Chat attachments

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `CHAT_ATTACHMENT_FOLDER` | `BW_CHAT_ATTACHMENT_FOLDER` | `instance/chat_attachments` | Chat file attachment storage directory |
| `CHAT_ALLOWED_EXTENSIONS` | - | `pdf, doc, docx, xls, xlsx, ppt, pptx, txt, md, csv, json, xml, zip, tar, gz, png, jpg, jpeg, gif, webp` | Permitted chat attachment file types |

### Kanban attachments

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `KANBAN_ATTACHMENT_FOLDER` | `BW_KANBAN_ATTACHMENT_FOLDER` | `instance/kanban_attachments` | Kanban ticket attachment storage directory |
| `KANBAN_MAX_ATTACHMENT_SIZE` | - | `5 MB` (5,242,880 bytes) | Per-attachment size cap |
| `KANBAN_ATTACHMENT_ALLOWED_EXTENSIONS` | - | `pdf, doc, docx, xls, xlsx, ppt, pptx, txt, md, csv, json, xml, zip, tar, gz, png, jpg, jpeg, gif, webp, mp4, webm, mp3, ogg, py, js, ts, html, css, sh` | Permitted Kanban attachment file types |

### Custom page files

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `CUSTOM_PAGE_FILES_FOLDER` | `BW_CUSTOM_PAGE_FILES_FOLDER` | `instance/custom_page_files` | Custom page binary file storage |
| `CUSTOM_PAGE_MAX_FILE_SIZE` | - | `16 MB` (16,777,216 bytes) | Per-file size cap (non-video) |
| `CUSTOM_PAGE_MAX_VIDEO_SIZE` | - | `100 MB` (104,857,600 bytes) | Per-video file size cap (overridable via site settings) |

### Logging

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `LOGGING_LEVEL` | - | `"verbose"` | Log verbosity level |
| `LOG_FILE` | `BW_LOG_FILE` | `logs/bananawiki.log` | Log file path |

Available log levels:

| Level | What is logged |
|---|---|
| `off` | Nothing |
| `minimal` | Critical events only |
| `medium` | Critical + important auth/admin actions |
| `verbose` | All user actions (default) |
| `debug` | All above + HTTP request details |

### Feature toggles

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `PAGE_HISTORY_ENABLED` | - | `True` | Enable page revision history feature |
| `EXPERIMENTAL_OBSIDIAN_SYNC` | - | `False` | Enable Obsidian vault pull/push CLI commands (no web routes) |

### Time-based settings

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `INVITE_CODE_EXPIRY_HOURS` | - | `48` | Hours until invite codes expire |
| `PAGE_RESERVATION_DURATION_HOURS` | - | `48` | Hours a page reservation lasts |
| `PAGE_RESERVATION_COOLDOWN_HOURS` | - | `24` | Cooldown hours after a reservation is released |
| `SERVER_RESTART_COOLDOWN_SECONDS` | - | `60` | Minimum seconds between admin-triggered server restarts (**code-only**, no UI / DB / `BW_*` override). See [Operations](operations.md). |

### Import / export limits

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `MAX_IMPORT_UNCOMPRESSED_SIZE` | - | `500 MB` (524,288,000 bytes) | Maximum total uncompressed size for migration ZIP imports |
| `MAX_IMPORT_MEMBER_SIZE` | - | `200 MB` (209,715,200 bytes) | Maximum uncompressed size for any single file in the import ZIP |
| `MAX_PLUGIN_UNCOMPRESSED_SIZE` | - | `50 MB` (52,428,800 bytes) | Maximum total uncompressed size for `.bwplugin` ZIP files |

### Google Drive backup fallbacks

These are not `config.py` constants. They are keys in the `hosting_settings`
table, managed through Admin → Site Settings. `_read_settings()` in
`hosting/gdrive_backup.py` supplies the defaults below when a column is missing
or `NULL`.

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `gdrive_backup_enabled` | - | `0` | Google Drive backup enable flag |
| `gdrive_folder_id` | - | `""` | Target Google Drive folder ID |
| `gdrive_credentials_path` | - | `CREDENTIALS_PATH_DEFAULT` | Path to Google service-account JSON key |
| `gdrive_retention_days` | - | `7` | Number of days to keep old backups |
| `gdrive_backup_time` | - | `"03:00"` | Time of day for the nightly backup (HH:MM) |

---

## Layer 2: Runtime settings (Admin → Site Settings)

Runtime settings are stored in the `site_settings` table (single row,
`id = 1`). Changes take effect immediately without a server restart.

Settings that hold sensitive values are transparently encrypted at rest. See
[Architecture & Security](architecture-and-security.md#213-settings-encryption).

### General

| Column | Type | Default | Description |
|---|---|---|---|
| `site_name` | TEXT | `"BananaWiki"` | Displayed in the sidebar, browser title, and header |
| `interface_language` | TEXT | `"en"` | Default interface language code when users do not set a personal override |
| `interface_language_fallback` | TEXT | `"en"` | Built-in fallback (`en` or `it`) used when custom language selections cannot render built-in content |
| `interface_languages_json` | TEXT | `"{}"` | Custom language packs metadata (code, display name, enabled state) managed from Admin → Site Settings |
| `timezone` | TEXT | `"UTC"` | Site-wide timezone for displayed dates and times |
| `setup_done` | INTEGER | `0` | Set to `1` after the first-run setup wizard completes |

### Appearance: Dark theme

| Column | Type | Default | Description |
|---|---|---|---|
| `primary_color` | TEXT | `"#8fa0d4"` | Primary UI colour (dark mode) |
| `secondary_color` | TEXT | `"#1e1e2c"` | Secondary colour (dark mode) |
| `accent_color` | TEXT | `"#7e9ada"` | Accent colour (dark mode) |
| `text_color` | TEXT | `"#c8ccd8"` | Text colour (dark mode) |
| `sidebar_color` | TEXT | `"#1a1a24"` | Sidebar background colour (dark mode) |
| `bg_color` | TEXT | `"#16161f"` | Page background colour (dark mode) |

### Appearance: Light theme

| Column | Type | Default | Description |
|---|---|---|---|
| `light_primary_color` | TEXT | `"#4b63b6"` | Primary UI colour (light mode) |
| `light_secondary_color` | TEXT | `"#ffffff"` | Secondary colour (light mode) |
| `light_accent_color` | TEXT | `"#3553c7"` | Accent colour (light mode) |
| `light_text_color` | TEXT | `"#202534"` | Text colour (light mode) |
| `light_sidebar_color` | TEXT | `"#e9edf5"` | Sidebar background colour (light mode) |
| `light_bg_color` | TEXT | `"#f6f7fb"` | Page background colour (light mode) |

### Appearance: General

| Column | Type | Default | Description |
|---|---|---|---|
| `default_theme_mode` | TEXT | `"dark"` | Default theme (`"dark"` or `"light"`) |
| `favicon_enabled` | INTEGER | `0` | Enable custom favicon |
| `favicon_type` | TEXT | `"yellow"` | Preset favicon colour (`yellow`, `blue`, `green`, etc.) |
| `favicon_custom` | TEXT | `""` | Base64-encoded custom favicon data |

### Access control

| Column | Type | Default | Description |
|---|---|---|---|
| `maintenance_mode` | INTEGER | `0` | When `1`, non-admins are redirected to `/maintenance`; admins sign in at `/admin` (legacy `/lockdown` redirects to `/maintenance`) |
| `maintenance_message` | TEXT | `""` | Custom message displayed on the maintenance page |
| `session_limit_enabled` | INTEGER | `0` | Enforce one active session per user (disabled by default; admins can enable via Site Settings) |
| `public_mode` | INTEGER | `0` | When `1`, unauthenticated visitors can read wiki pages |
| `public_mode_until` | TEXT | `NULL` | UTC ISO datetime to auto-disable public mode (optional) |
| `public_mode_message` | TEXT | `""` | Banner message shown in public mode |
| `public_mode_show_message` | INTEGER | `0` | Whether to show the public mode message banner |
| `open_signup` | INTEGER | `0` | When `1`, new users can register without an invite code |
| `open_signup_until` | TEXT | `NULL` | UTC ISO datetime to auto-disable open signup (optional) |
| `auto_logout_enabled` | INTEGER | `0` | Enable daily automatic mass logout |
| `auto_logout_hour` | INTEGER | `0` | Hour (0–23 UTC) for automatic mass logout |

### Chat: Direct messages

| Column | Type | Default | Description |
|---|---|---|---|
| `chat_dm_enabled` | INTEGER | `1` | Enable the direct messaging feature |
| `chat_allow_dm_creation` | INTEGER | `1` | Allow users to start new DM conversations |
| `chat_dm_auto_clear_messages` | INTEGER | `0` | Auto-clear DM messages during cleanup |
| `chat_dm_auto_clear_attachments` | INTEGER | `1` | Auto-clear DM attachments during cleanup |
| `chat_dm_message_retention_days` | INTEGER | `0` | DM message retention (0 = keep forever) |
| `chat_dm_attachment_retention_days` | INTEGER | `7` | DM attachment retention in days |

### Chat: Group messages

| Column | Type | Default | Description |
|---|---|---|---|
| `chat_group_enabled` | INTEGER | `1` | Enable the group chat feature |
| `chat_allow_group_creation` | INTEGER | `1` | Allow users to create new group chats |
| `chat_group_auto_clear_messages` | INTEGER | `0` | Auto-clear group messages during cleanup |
| `chat_group_auto_clear_attachments` | INTEGER | `1` | Auto-clear group attachments during cleanup |
| `chat_group_message_retention_days` | INTEGER | `0` | Group message retention (0 = keep forever) |
| `chat_group_attachment_retention_days` | INTEGER | `7` | Group attachment retention in days |

### Chat: Global settings

| Column | Type | Default | Description |
|---|---|---|---|
| `chat_max_message_length` | INTEGER | `5000` | Maximum characters per chat message |
| `chat_attachments_enabled` | INTEGER | `1` | Enable file attachments in chats |
| `chat_max_attachment_size_mb` | INTEGER | `5` | Maximum chat attachment size in MB |
| `chat_attachments_per_day_limit` | INTEGER | `10` | Maximum chat attachments per user per day |
| `chat_auto_clear_messages` | INTEGER | `0` | Legacy: auto-clear messages (superseded by per-type settings) |
| `chat_auto_clear_attachments` | INTEGER | `1` | Legacy: auto-clear attachments |
| `chat_message_retention_days` | INTEGER | `0` | Legacy: message retention days |
| `chat_attachment_retention_days` | INTEGER | `7` | Legacy: attachment retention days |

### Chat cleanup scheduling

| Column | Type | Default | Description |
|---|---|---|---|
| `chat_cleanup_enabled` | INTEGER | `1` | Enable the periodic chat cleanup job |
| `chat_cleanup_frequency_days` | INTEGER | `7` | Days between cleanup runs |
| `chat_cleanup_hour` | INTEGER | `3` | Hour (0–23 UTC) to run the cleanup |
| `chat_cleanup_split_configured` | INTEGER | `0` | Whether DM/group cleanup settings have been individually configured |
| `last_chat_cleanup_at` | TEXT | `NULL` | UTC timestamp of the last cleanup run |

### Page reservations

| Column | Type | Default | Description |
|---|---|---|---|
| `page_reservations_enabled` | INTEGER | `0` | Enable the page checkout / reservation system |
| `page_reservation_duration_hours` | INTEGER | `48` | Hours a reservation lasts (overrides `config.py`) |
| `page_reservation_cooldown_hours` | INTEGER | `24` | Cooldown hours after release (overrides `config.py`) |
| `default_reserved_pages_quota` | INTEGER | `5` | Default max concurrent reservations per user |

### Kanban

| Column | Type | Default | Description |
|---|---|---|---|
| `kanban_access` | TEXT | `"admin"` | Who can see Kanban boards: `"admin"`, `"editor"`, or `"all"` |
| `kanban_write_access` | TEXT | `"admin"` | Who can create/edit boards: `"admin"`, `"editor"`, or `"all"` |

Users individually shared on a board bypass the global access level.

### Canvas

| Column | Type | Default | Description |
|---|---|---|---|
| `canvas_access` | TEXT | `"admin"` | Who can see canvas layouts: `"admin"`, `"editor"`, or `"all"` |
| `canvas_write_access` | TEXT | `"admin"` | Who can create/edit canvas layouts: `"admin"`, `"editor"`, or `"all"` |

### Google Drive backup

| Column | Type | Default | Description |
|---|---|---|---|
| `gdrive_backup_enabled` | INTEGER | `0` | Enable nightly Google Drive backup |
| `gdrive_folder_id` | TEXT | `""` | Target Google Drive folder ID |
| `gdrive_credentials_path` | TEXT | `""` | Path to Google service-account JSON key |
| `gdrive_retention_days` | INTEGER | `30` | Number of days to keep old backups |
| `gdrive_backup_time` | TEXT | `"02:00"` | Time of day for the nightly backup (HH:MM) |
| `last_backup_sent_at` | REAL | `0` | Unix timestamp of the last backup sent |

### Banana mode

| Column | Type | Default | Description |
|---|---|---|---|
| `banana_mode` | INTEGER | `0` | When `1`, non-admin users see a full-screen 🍌 overlay |

Banana mode is controlled by the built-in API Service plugin from
`/admin/api-service#banana-mode` or via `GET`/`POST /api/v1/banana-mode`.

### PDF export

| Column | Type | Default | Description |
|---|---|---|---|
| `pdf_export_enabled` | INTEGER | `0` | Enable PDF export of wiki pages (requires the `page.export_pdf` permission) |

When enabled, users with the `page.export_pdf` permission can download any
accessible wiki page as a PDF document. The server generates the PDF with the
fpdf2 library.

### Upload controls

| Column | Type | Default | Description |
|---|---|---|---|
| `upload_mode` | TEXT | `"whitelist"` | Upload filter mode: `"whitelist"` or `"blacklist"` |
| `upload_whitelist` | TEXT | `""` | Comma-separated allowed extensions (whitelist mode) |
| `upload_blacklist` | TEXT | *(long default list of dangerous extensions)* | Comma-separated blocked extensions (blacklist mode) |
| `upload_max_size_mb` | INTEGER | `5` | Per-file upload size cap in MB |

### Custom pages

| Column | Type | Default | Description |
|---|---|---|---|
| `custom_pages_max_video_size_mb` | INTEGER | `100` | Override for maximum video file size on custom pages (MB) |

### Documentation

| Column | Type | Default | Description |
|---|---|---|---|
| `docs_category_id` | INTEGER | `NULL` | Category ID of the spawned wiki documentation |
| `docs_bypass_deletion_slowdown` | INTEGER | `1` | Whether spawned docs bypass the deletion slowdown queue |

### User profiles

| Column | Type | Default | Description |
|---|---|---|---|
| `profile_group_badges_enabled` | INTEGER | `0` | Show group membership badges on user profiles |

---

## Environment variable quick reference

`config.py` holds almost all of them, each next to its default, and is the
place to look when something here is not enough. `BW_EXTERNAL_PLUGINS_DIR` is
the exception: it is read in `plugin_loader.py` and points at
`plugins/external/`, which matters for a deployment whose install tree is read
only. The ones a single installation is most likely to set:

```bash
BW_INSTANCE_DIR=/var/lib/bananawiki      # data directory; see the note below
BW_ENV=production
BW_PORT=5001
BW_HOST=127.0.0.1
BW_PROXY_MODE=1                          # only behind a trusted reverse proxy
BW_PREFERRED_URL_SCHEME=https
BW_SESSION_COOKIE_NAME=bw_session
BW_PASSWORD_HASH_METHOD=auto
BW_LOG_FILE=logs/bananawiki.log
BW_LOGGING_LEVEL=INFO
BW_SOURCE_URL=https://github.com/you/your-fork   # AGPL source offer
BW_SETUP_TOKEN=...                       # generated if unset
```

The storage paths do not all follow the same base. `BW_DATABASE_PATH` and
`BW_TTS_FOLDER` default under `BW_INSTANCE_DIR`. `BW_ATTACHMENT_FOLDER`,
`BW_CHAT_ATTACHMENT_FOLDER`, `BW_KANBAN_ATTACHMENT_FOLDER` and
`BW_CUSTOM_PAGE_FILES_FOLDER` default to `instance/` beside the source, and
`BW_UPLOAD_FOLDER` and `BW_FAVICON_UPLOAD_FOLDER` default inside `app/static/`.
Setting `BW_INSTANCE_DIR` alone therefore moves the database but leaves attachments
and uploads in the checkout, where an update that replaces the source tree
would take them with it.

`banana install` and the hosting platform set all of them explicitly, so this
only affects an installation configured by hand. If that is yours, set each
one, or keep the whole checkout somewhere an update does not replace. See
[MIGRATION.md](../MIGRATION.md) for what to move and when.

Size and rate limits (`BW_MAX_CONTENT_LENGTH_BYTES`,
`BW_MAX_ATTACHMENT_SIZE_BYTES`, the `BW_BACKGROUND_IMAGE_*` and
`BW_SITE_EXPORT_*` groups, the `BW_TTS_*` group) and the feature switches
(`BW_ALLOW_EXTERNAL_PLUGINS`, `BW_PLUGIN_ISOLATION`, `BW_EASY_WIKI`,
`BW_FEDERATION_ENABLED`, the `BW_FORBID_*` group) are documented next to their
defaults in `config.py`. [Federation](federation.md) covers
`BW_FEDERATION_ENABLED` in full.

`BW_ALLOW_EXTERNAL_PLUGINS` is on unless set to `0` on a self-hosted wiki.
While it is on, any admin can upload a `.bwplugin` file, and a plugin that is
enabled runs inside the wiki with the wiki's own privileges, which gives
complete control of the wiki (see
[Plugins](plugins/overview.md#what-an-external-plugin-can-do)).  Set it to
`0` and the upload form disappears and external plugin folders are ignored.
Under `BW_MANAGED_HOSTING` it also needs `BW_PLUGIN_ISOLATION=container`.

`BW_MANAGED_HOSTING`, `BW_PLATFORM_INSTANCE_ID`, `BW_INSTANCE_EXPIRES_AT`,
`BW_STORAGE_LIMIT_BYTES`, `BW_MEMORY_LIMIT_MB`, `BW_NOFILE_LIMIT`, the
`BW_MANAGED_*` group and the `BW_PLATFORM_OAUTH_*` group are set by the hosting
platform for each tenant container. A standalone installation leaves them
unset. See [hosting](hosting.md) and
[platform OAuth SSO](platform-oauth-sso.md). Under `BW_MANAGED_HOSTING` the
wiki refuses, with status 411, any request whose body is sent without a
`Content-Length` header (for example with `Transfer-Encoding: chunked`),
because the storage quota check sizes the body from that header. Browsers
and the usual HTTP libraries send it for form posts, file uploads and JSON;
an API client that streams a request body has to send a length instead.

`SECRET_KEY` is read without the prefix. Leave it unset and the application
keeps a generated key in `.secret_key` inside the instance directory, which is
what you want unless several processes need to share a key you manage
yourself.

---

## Further reading

- **[Getting Started](getting-started.md)**: installation and first steps.
- **[Architecture & Security](architecture-and-security.md)**: request
  lifecycle and security measures.
- **[Permissions](permissions.md)**: the custom permission system.
- **[Hosting](hosting.md)**: multi-tenant hosting platform configuration.
