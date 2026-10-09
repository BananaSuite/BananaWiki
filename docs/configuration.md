# Configuration

BananaWiki has two kinds of settings:

* **Environment variables** (this page). They are read once when a process
  starts; change them and restart. They decide where data lives, how the
  server listens, and what a host allows.
* **Site settings**, stored in the `site_settings` row of the database and
  edited by administrators under **Admin** (`/admin/settings` and the feature
  pages). They apply at once. See [features](features.md) for the settings of
  each feature.

Every `BW_*` variable that BananaWiki 1.4 honoured is still honoured with the
same meaning, so an existing systemd unit, container environment or `.env`
file keeps working. Defaults that changed are marked below.

## How values are read

* **Booleans** accept `1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`
  (any case). Anything else stops the wiki at start-up with a message naming
  the variable. (The `HOSTING_PROXY_MODE`, `HOSTING_EMAIL_SMTP_TLS` and
  `HOSTING_AUTO_FLATTEN_DOMAIN_LAYOUT` flags of the portal keep the 1.4 rule:
  anything except a false word is on.)
* **Numbers** outside the documented range stop the wiki at start-up.
  Exception: the `BW_TTS_*` variables only log a warning and use their
  default, so a typo in an optional feature never keeps the wiki down.
* **Paths** may use `~` and are made absolute.
* **Lists** are comma-separated.

`bananawiki config check` validates the environment and prints warnings (for
example a production server without `BW_PROXY_MODE` listening on a public
address, or a data folder that is not writable).

## Where the wiki keeps its data

Everything the wiki writes goes into the **instance directory**. Every folder
can be moved individually; unset folders follow `BW_INSTANCE_DIR`.

| Variable | Default | What it is |
|---|---|---|
| `BW_INSTANCE_DIR` | `<source tree>/instance`, or `./instance` in the working directory for an installed package | Instance directory holding the database, secret key and every data folder. Set it explicitly in production. |
| `BW_DATABASE_PATH` | `<instance>/bananawiki.db` | The SQLite database (WAL mode; `-wal`, `-shm`, `.initialized` and `.schema.lock` files sit next to it). |
| `BW_UPLOAD_FOLDER` | `<instance>/uploads` | Images embedded in pages (served at `/static/uploads/<name>`), avatars and display backgrounds. **1.4 default:** `<source>/app/static/uploads`. |
| `BW_FAVICON_UPLOAD_FOLDER` | `<instance>/favicons` | Custom favicons. **1.4 default:** `<source>/app/static/favicons`. |
| `BW_ATTACHMENT_FOLDER` | `<instance>/attachments` | Page attachments. **1.4 default:** `<source>/instance/attachments`. |
| `BW_CHAT_ATTACHMENT_FOLDER` | `<instance>/chat_attachments` | Files sent in direct messages and groups. |
| `BW_KANBAN_ATTACHMENT_FOLDER` | `<instance>/kanban_attachments` | Kanban ticket files. |
| `BW_CUSTOM_PAGE_FILES_FOLDER` | `<instance>/custom_page_files` | Files of custom pages. |
| `BW_TTS_FOLDER` | `<instance>/tts` | Generated read-aloud audio. |
| `BW_TTS_PIPER_VOICE_DIR` | `<instance>/piper-voices` | Piper voice models. |
| `BW_SITE_EXPORT_TEMP_DIR` | `<instance>/tmp_exports` | Scratch space for whole-site export and import, attachment and bulk Markdown exports, and personal data exports. |
| `BW_EXTERNAL_PLUGINS_DIR` | `<instance>/plugins` | Third-party plugins. |
| `BW_LOG_FILE` | `<instance>/logs/bananawiki.log` | Application log, rotated at 10 MB with 5 old files. **1.4 default:** `<source>/logs/bananawiki.log`. |

The instance directory also holds `.secret_key`, uploaded interface languages
(`translations/`), automatic database copies (`backups/`) and plugin safety
snapshots (`plugin_safety_snapshots/`).

When a 1.4 installation starts 1.6 and one of the folder variables above is
**not** set, the files found at the 1.4 default location are moved into the
instance directory (never overwriting anything). See [UPGRADING](../UPGRADING.md).

## Core settings

| Variable | Default | Meaning |
|---|---|---|
| `BW_ENV` | `production` | `production`, `development` (`dev`) or `test` (`testing`). Production refuses a secret key shorter than 32 characters and a key file readable by other users, and sends HSTS on HTTPS requests. `bananawiki serve` defaults to `development`. |
| `SECRET_KEY` (alias `BW_SECRET_KEY`) | generated | Signs sessions and derives the keys for encrypted settings, API token digests and the setup token. When unset, 64 hex characters are generated once into `<instance>/.secret_key` (mode 0600). **Changing it signs everyone out, invalidates every API token and makes encrypted settings unreadable.** |
| `BW_SETUP_TOKEN` | derived | Token needed to create the first account at `/setup`. Default: HMAC-SHA256 of the secret key and `initial-admin-setup` (hex). Print it with `bananawiki setup-token`. |
| `BW_HOST` | `127.0.0.1` | Listen address (Gunicorn and `bananawiki serve`). |
| `BW_PORT` | `5001` | Listen port (1–65535). |
| `BW_PROXY_MODE` | `0` | Trust `X-Forwarded-For`, `-Proto` and `-Host` from **one** proxy in front. Turn it on only when the port is reachable through that proxy alone. `X-Forwarded-Prefix` is never trusted. |
| `BW_PREFERRED_URL_SCHEME` | `https` with proxy mode, else `http` | Scheme of absolute links the wiki generates. |
| `BW_SESSION_COOKIE_NAME` | `bw_session` | Name of the session cookie (hosted wikis use `bw_session_<slug>`). A `Secure` cookie gets the `__Host-` prefix: `__Host-bw_session`. |
| `BW_SECURE_COOKIES` | follows the request | `1` always marks cookies `Secure` (and `__Host-` named), `0` never; unset: `Secure` on HTTPS requests. |
| `BW_PASSWORD_HASH_METHOD` (alias `HASH_METHOD`) | `auto` | `auto` (scrypt when available, else PBKDF2-SHA256), `scrypt` or `pbkdf2`. Existing hashes of any Werkzeug format keep working. |
| `BW_SOURCE_URL` | `https://github.com/BananaSuite/BananaWiki` | Where `/source` redirects (AGPL section 13). Must be an http(s) URL without credentials. Point it at the source of the version you run. |
| `BW_LOGGING_LEVEL` | `medium` | `off`, `minimal` (warnings), `medium` (information), `verbose` (same as `medium`) or `debug`. **1.4 default:** `verbose`. |
| `BW_DEFAULT_INTERFACE_LANGUAGE` | `en` | Interface language used until an administrator chooses one. |
| `BW_BACKGROUND_JOBS` | `1` | `0` stops the scheduler thread in the web workers; run `bananawiki jobs run` from cron instead. Replaces 1.4's `BANANAWIKI_SKIP_BACKGROUND_SERVICES`. |
| `BW_MAINTENANCE_FILE` (alias `BANANA_MAINTENANCE_FILE`) | none | While this file exists every request except `/health` and `/healthz` gets a plain 503. The managed updater uses it during updates. (Not the same as the administrator's maintenance mode.) |

## Limits

| Variable | Default | Meaning |
|---|---|---|
| `BW_MAX_CONTENT_LENGTH_BYTES` | 16 MiB (at least 1 MiB) | Largest ordinary request and image upload. |
| `BW_MAX_ATTACHMENT_SIZE_BYTES` | 100 MiB (at least 1 KiB) | Largest page, canvas or chat attachment (chat also has its own admin limit). |
| `BW_BACKGROUND_IMAGE_MAX_UPLOAD_SIZE_BYTES` | 4 MiB | Largest display background picture a member can upload. |
| `BW_BACKGROUND_IMAGE_MAX_PIXELS` | 16000000 | Largest background picture in pixels. |
| `BW_BACKGROUND_IMAGE_MAX_DIMENSION` | 2560 (at least 16) | Background pictures are scaled down to this width/height. |
| `BW_MIN_FORM_SECONDS` | 0.4 | With bot protection on, sign-up forms sent faster than this are refused. |
| `BW_MEMORY_LIMIT_MB` | 0 (none) | Address-space limit (`RLIMIT_AS`) for each process. |
| `BW_NOFILE_LIMIT` | 0 (none) | Open-files limit (`RLIMIT_NOFILE`) for each process. |

Fixed limits (not configurable): whole-site import archives up to 500 MiB,
custom page files 16 MiB, hosted videos on custom pages 100 MiB (lowered by
the `custom_pages_max_video_size_mb` setting), JSON request bodies 2 MiB,
form fields 2 MiB in memory, sessions 7 days (30 days with "remember me";
without it the cookie ends with the browser session).

## Database tuning

| Variable | Default | Meaning |
|---|---|---|
| `BW_DB_BUSY_TIMEOUT_MS` | 5000 (100–30000) | How long a connection waits for SQLite's write lock. |
| `BW_DB_CACHE_KIB` | 4096 (256–65536) | Page cache per connection. |
| `BW_DB_SYNCHRONOUS` | `full` | `full` or `normal`. `normal` is faster; a power loss may lose the last transactions (never corrupt the file) in WAL mode. |
| `BW_DB_INTEGRITY_CHECK_INTERVAL_SECONDS` | 3600 | Accepted for compatibility; 1.6 does not run periodic integrity checks. Use `bananawiki db check`. |

`BW_DB_OBSERVABILITY` from 1.4 is no longer read.

## Features and plugins

| Variable | Default | Meaning |
|---|---|---|
| `BW_FEDERATION_ENABLED` | `0` | Turns on [federation](federation.md). There is no administrator switch. |
| `BW_ALLOW_SITE_IMPORT` | `1` (`0` under managed hosting) | Allows **Admin → Site migration → Import**, which replaces the whole database. |
| `BW_ALLOW_EXTERNAL_PLUGINS` | `1` | `0` refuses plugin uploads and loads no third-party plugin. Under managed hosting plugins stay off unless `BW_PLUGIN_ISOLATION=container`. |
| `BW_PLUGIN_ISOLATION` | none | `container` tells a managed wiki that it runs in its own container, which allows third-party plugins. |
| `BW_EXTERNAL_PLUGINS_DIR` | `<instance>/plugins` | See the data table above. |
| `BW_MANAGED_PLUGIN_DENYLIST` | none | Feature and plugin ids that never load. |
| `BW_EASY_WIKI` | `0` | Hides and keeps off the advanced features (chat, kanban, canvas, assessments, custom pages, page builder, page governance, contributions, federation, read aloud). |

## Email

The wiki sends email only for notifications (requests waiting for
administrators and reviewers, decisions on someone's own request), and only
after an administrator switches them on in **Admin → Notifications**. The mail
server can be configured there (the SMTP password and API key are stored
encrypted with the secret key) or with these variables. **When
`BW_MAIL_PROVIDER` or `BW_SMTP_HOST` is set, the environment wins** and the
administrator's server form is locked. Under managed hosting
(`BW_MANAGED_HOSTING=1`) only the environment counts: the host decides whether
tenant wikis can send email.

| Variable | Default | Meaning |
|---|---|---|
| `BW_MAIL_PROVIDER` | `smtp` when `BW_SMTP_HOST` is set, else none | `smtp`, `brevo` or `resend`. |
| `BW_MAIL_FROM` | none | Sender, `wiki@example.org` or `Wiki <wiki@example.org>`. Required. |
| `BW_MAIL_REPLY_TO` | none | Reply-to address. |
| `BW_MAIL_API_KEY` | none | API key for Brevo or Resend. |
| `BW_SMTP_HOST` | none | SMTP server. |
| `BW_SMTP_PORT` | 587 | SMTP port. |
| `BW_SMTP_SECURITY` | `ssl` on port 465, else `starttls` | `starttls`, `ssl` (implicit TLS) or `none`. A password is never sent unencrypted except to `localhost`. |
| `BW_SMTP_USERNAME`, `BW_SMTP_PASSWORD` | none | SMTP credentials. |
| `BW_MAIL_TIMEOUT` | 15 (1–120) | Seconds before a connection to the mail server gives up. |
| `BW_MAIL_DAILY_LIMIT` | 0 (none) | Not used by the wiki's notifications (they are throttled per recipient); accepted for symmetry with the portal. |
| `BW_BASE_URL` | the administrator's setting | Public address of the wiki (`https://wiki.example.org`) for links in emails. Without it, the address saved in **Admin → Notifications** is used; with neither, emails carry no links. |

## Hosting platform (set by the host)

These are set by the hosting portal or the desktop launcher. On a server you
run yourself, leave them unset.

| Variable | Default | Meaning |
|---|---|---|
| `BW_MANAGED_HOSTING` (alias `BW_HOSTED_MODE`) | `0` | The wiki is a tenant of a hosting platform: third-party plugins require the operator's `HOSTING_ALLOW_TENANT_PLUGINS` opt-in, site import off, upload size and remote GPU settings owned by the host, builder pages never public. |
| `BW_FORBID_PUBLIC_MODE` | `0` | The host forbids public mode; published custom pages are then shown to signed-in members only. |
| `BW_FORBID_PAGE_BUILDER` | `0` | The host forbids the page builder. |
| `BW_FORBID_PUBLIC_BUILDER_PAGES` | same as `BW_MANAGED_HOSTING` | Page-builder pages, custom pages included, are never shown to anonymous visitors. |
| `BW_MANAGED_TTS_DISABLED` | `0` | The host switched read-aloud generation off (existing audio stays playable). |
| `BW_STORAGE_LIMIT_BYTES` | 0 (none) | Uploads are refused once the stored files reach this size. |
| `BW_PLATFORM_UPLOAD_BLACKLIST` | none | File extensions the host refuses, on top of the administrator's rules. |
| `BW_PLATFORM_INSTANCE_ID` | none | This wiki's id on the portal (used by platform sign-in). |
| `BW_INSTANCE_EXPIRES_AT` | none | Accepted for compatibility; the portal enforces expiry. |
| `BW_EASY_DEPLOYMENT` | `0` | Set by the desktop launcher; accepted for compatibility. |
| `BW_PLATFORM_OAUTH_ENABLED` | `0` | Sign in with the hosting portal. When on, the portal also sets `BW_PLATFORM_OAUTH_CLIENT_ID`, `_CLIENT_SECRET`, `_AUTHORIZE_URL`, `_TOKEN_URL`, `_USERINFO_URL`, `_PORTAL_BASE`, `_LINK_URL`, `_LINK_STATUS_URL`, `_UNLINK_URL` and `_VERIFY_URL`. |

## Web server (Gunicorn)

Read by `gunicorn.conf.py` (`bananawiki/ops/gunicorn_conf.py`). Out-of-range
values are clamped.

| Variable | Default | Meaning |
|---|---|---|
| `BW_HOST`, `BW_PORT` | `127.0.0.1`, `5001` | Bind address. |
| `BW_WORKERS` | 2 (1–16) | Worker processes. SQLite serialises writes, so a few workers with threads work better than many processes. |
| `BW_THREADS` | 4 (1–32) | Threads per worker. |
| `BW_WORKER_TIMEOUT` | 120 (10–600) | Seconds before a stuck worker is replaced. |
| `BW_WRITE_TIMEOUT` | 300 (0–3600) | Whole seconds a response write may wait for the client before the connection is closed, so a client that stops reading a download holds a worker thread no longer than that. Measured, a slow reader was kept while it took in about 256 KiB per period, or up to 4 MiB once its receive buffer had grown after a fast start (about 1 and 14 KiB/s with 300; see [deployment](deployment.md#reverse-proxy)); raise the value to serve slower links. `0` waits forever. |
| `BW_ACCESS_LOG` | `-` | Access log: `-` for standard output, `off`, or a file path. |

The application is not preloaded in the Gunicorn master: every worker runs its
own scheduler thread (a lease in `job_runs` makes sure each job runs in one
worker at a time) and loads plugins itself.

## Read aloud (text-to-speech)

Invalid values log a warning and fall back to the default.

| Variable | Default | Meaning |
|---|---|---|
| `BW_TTS_BACKEND` | chosen from the admin page | `piper` (local; `local` and `offline` also mean this), `remote-gpu` or `stub` (silent clips, for development). |
| `BW_TTS_INLINE_WORKER` | `0` | `1` runs the synthesis worker as threads inside the web workers instead of a separate `scripts/tts_worker.py` process. |
| `BW_TTS_WORKER_COUNT` | 1 (1–8) | Worker threads. |
| `BW_TTS_MANUAL_MAX_ACTIVE_JOBS` | 1 | Generations that may be pending or running at once, site-wide, before readers are told the queue is full. |
| `BW_TTS_MANUAL_MAX_ACTIVE_PER_USER` | 1 | The same, per reader who asked. |
| `BW_TTS_MIN_START_INTERVAL_SECONDS` | 0.25 | Minimum pause between two jobs. |
| `BW_TTS_MAX_AUTO_RESUME_ATTEMPTS` | 3 (0–50) | Retries of a failed job. |
| `BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS` | 2.0 | First retry delay (doubles each time). |
| `BW_TTS_AUTO_RESUME_MAX_DELAY_SECONDS` | 30.0 | Longest retry delay. |
| `BW_TTS_RATE_LIMIT_COOLDOWN_SECONDS` | 900 | Pause after the GPU server answers `429`. |
| `BW_TTS_SHUTDOWN_GRACE_SECONDS` | 20 | Jobs still running this long after a stop request go back to the queue. |
| `BW_TTS_MAX_JOB_SECONDS` | 3600 (300–86400) | Longest a job may run; then Piper is stopped and the job fails. Keep it above `BW_TTS_REMOTE_GPU_TIMEOUT`. |
| `BW_TTS_PIPER_MEMORY_MB` | 0 (none) | Address-space limit (`RLIMIT_AS`) of the Piper process on Linux; a page that needs more fails. See [read aloud](tts.md#limits). |
| `BW_TTS_PERFORMANCE_MODE` | from the admin page | `auto`, `balanced` or `fast`. |
| `BW_TTS_PIPER_AUTO_DOWNLOAD` | `1` | Download missing Piper voices on first use. |
| `BW_TTS_PIPER_VOICE_MAP` | built-in voices | Extra or replacement voices: JSON (`{"de": "de_DE-thorsten-high"}`) or `de=de_DE-thorsten-high,pt=pt_BR-faber-medium`. |
| `BW_TTS_PIPER_OUTPUT_FORMAT` | `auto` | `auto`, `wav` or `mp3` (MP3 needs ffmpeg). |
| `BW_TTS_PIPER_LENGTH_SCALE`, `BW_TTS_PIPER_NOISE_SCALE`, `BW_TTS_PIPER_NOISE_W_SCALE` | voice defaults | Piper voice parameters. |
| `BW_TTS_FFMPEG` | `ffmpeg` on `PATH` | Path or name of ffmpeg. |
| `BW_TTS_REMOTE_GPU_URL` | none | GPU speech server address. Takes precedence over the admin page (and is the only source under managed hosting). |
| `BW_TTS_REMOTE_GPU_AUTH_TOKEN` | none | Its bearer token. |
| `BW_TTS_REMOTE_GPU_TIMEOUT` | 120 | Seconds per request (at most 3600). |
| `BW_TTS_WORKER_LOG_LEVEL` | `INFO` | Log level of the separate worker process. |

The GPU speech server itself is configured with `TTS_*` variables; see
[read aloud](tts.md#the-gpu-speech-server).

## Managed servers

`banana install` writes `config/app.env` and the systemd units load it. The
controller fills in defaults it owns and never overwrites a value you set,
except for these, which it manages: `BANANA_MAINTENANCE_FILE`,
`PYTHONDONTWRITEBYTECODE`, and on hosting servers `HOSTING_CONTAINER_IMAGE`
and `BW_RUNTIME_AGENT_SOCKET`. A wiki installation starts with:

`BW_HOST=127.0.0.1`, `BW_PORT=<port>`, `BW_PROXY_MODE` (1 with a domain),
`BW_PREFERRED_URL_SCHEME` (`https` with a domain), `BW_ENV=production`, a
random `BW_SETUP_TOKEN`, `BW_SOURCE_URL` (the web page of the update source),
`BW_SYSTEMD_SERVICE` (informational), and every folder variable pointing into
`<root>/data`. `BANANA_PACKAGE_MAX_FILES` (default 1000000, 1000–100000000),
which you add, sets how many files a backup or update package may hold (see
[operations](operations.md#managed-servers-portable-packages)).

## Hosting portal

The portal reads its own variables. Every variable of BananaWiki Hosting 1.4
is honoured with the same default. Paths default to `<source tree>/hosting/data`
like 1.4; managed installations set them to `<root>/data`.

### Server and storage

| Variable | Default | Meaning |
|---|---|---|
| `HOSTING_ENV` | value of `BW_ENV`, else `production` | Same values as `BW_ENV`. |
| `HOSTING_HOST`, `HOSTING_PORT` | `127.0.0.1`, `5099` | Listen address. |
| `HOSTING_PROXY_MODE` | off | Trust one proxy's `X-Forwarded-For`, `-Proto`, `-Host`. The managed Caddy configuration puts the visitor's address there, also behind Cloudflare. |
| `HOSTING_PREFERRED_URL_SCHEME` | none | Scheme of generated absolute links. |
| `HOSTING_SECRET_KEY` | from the key file | Portal signing key. Also derives the MFA and encrypted-settings keys: changing it disables every account's authenticator. |
| `HOSTING_SECRET_KEY_PATH` | `<source>/hosting/data/.secret_key` | Generated key file (0600). |
| `HOSTING_BOOTSTRAP_TOKEN` | none | Required to create the first (administrator) account at `/signup`. Without it the first sign-up is locked. |
| `HOSTING_DATABASE_PATH` | `<source>/hosting/data/hosting.db` | Portal database. |
| `HOSTING_BACKUP_KEY_PATH` | `<database dir>/.backup_encryption_key` | Key for platform backups: a 32-byte regular file owned by the portal service user with mode 0600. Public files, links and special files are refused. |
| `INSTANCES_DIR` | `<source>/hosting/data/instances` | Tenant data directories (`<slug>` or `<slug>__apex`). |
| `HOSTING_PLATFORM_STATE_DIR` | `<database dir>/platform_state` | Portal state; must not be inside `INSTANCES_DIR`. |
| `HOSTING_DB_BUSY_TIMEOUT_MS` | 5000 (100–30000) | SQLite write-lock wait. |
| `HOSTING_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. |
| `BANANA_MAINTENANCE_FILE` (alias `BW_MAINTENANCE_FILE`) | none | 503 for everything but `/health` while it exists. |
| `BW_SOURCE_URL` | `https://github.com/BananaSuite/BananaWiki` | Source link shown by the portal (managed installs set it to the update source). |
| `HOSTING_WORKERS`, `HOSTING_THREADS`, `HOSTING_WORKER_TIMEOUT`, `HOSTING_WRITE_TIMEOUT`, `HOSTING_ACCESS_LOG` | 2, 4, 120, 300, `-` | Read by `hosting/gunicorn.conf.py`; the same meaning as the `BW_` variables of the [web server](#web-server-gunicorn). |

### Addresses

| Variable | Default | Meaning |
|---|---|---|
| `BASE_DOMAIN` | none | The platform's domain. |
| `PORTAL_DOMAIN` | `BASE_DOMAIN` | Where the portal answers. |
| `INSTANCE_URL_SUFFIX` | `hosting` | Wikis live at `<slug>-<suffix>.<BASE_DOMAIN>`. Set it to an empty value for `<slug>.<BASE_DOMAIN>`. |
| `HOSTING_AUTO_FLATTEN_DOMAIN_LAYOUT` | on | With only `BASE_DOMAIN=hosting.example.com` set, read it as base `example.com`, portal `hosting.example.com`, suffix `hosting` (single-level wiki hosts, as in 1.4). |
| `HOSTING_MODE` | detected | `subdomain`, `port` or `onion`. Detected as `subdomain` when `BASE_DOMAIN` is a domain name, otherwise `port`. |
| `HOSTING_PUBLIC_HOST` | base domain, or the outbound interface address | Host name used in port mode links. 1.4 asked public IP-echo services; 1.6 never does. |
| `HOSTING_PUBLIC_SCHEME` | `https` | Scheme of wiki links. |
| `INSTANCE_PORT_START`, `INSTANCE_PORT_END` | 6001, 7000 | Port range in port mode. |
| `SUBDOMAIN_MIN_LENGTH`, `SUBDOMAIN_MAX_LENGTH` | 3, 40 | Wiki name length. |
| `HOSTING_CUSTOM_DOMAIN_TARGET` | none | Host name customers point their CNAME at. |
| `HOSTING_CUSTOM_DOMAIN_IPS` | none | Addresses accepted for A/AAAA records of custom domains. |
| `HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED` | on | Accept a custom domain whose A/AAAA records are all Cloudflare edge addresses (a record proxied by the customer's Cloudflare account); the TXT proof still proves ownership. `0` requires the CNAME or the addresses above. |
| `HOSTING_CONTACT_EMAIL` | reply-to address, else `contact@<BASE_DOMAIN>` | Shown in the help centre and emails. |
| `HOSTING_STATUS_URL` | none | Link to a status page. |

### Tenants

| Variable | Default | Meaning |
|---|---|---|
| `HOSTING_RUNTIME_BACKEND` | `agent` | Module under `bananawiki/hosting/runtime/` that runs tenants. |
| `BW_RUNTIME_AGENT_SOCKET` | set by the controller | Socket of the runtime agent. |
| `HOSTING_INSTANCE_RUNTIME` | `docker` | `docker` or `process` (validated; tenants run in Docker). |
| `HOSTING_CONTAINER_IMAGE` | `bananawiki-tenant:latest` | Tenant image; the managed updater sets `bananawiki-tenant:<revision>`. |
| `HOSTING_CONTAINER_INTERNAL_PORT` | 5001 | Port inside tenant containers. |
| `HOSTING_TENANT_NETWORK` | `isolated` in subdomain mode, else `outbound` | `isolated`: a Docker internal bridge for each tenant, blocking traffic beyond its subnet. Host gateway listeners still require host firewall INPUT rules; bind host-only services to loopback. Port and onion mode need `outbound`. |
| `HOSTING_ALLOW_TENANT_PLUGINS` | `0` | Operator opt-in for third-party Python plugins in hosted wikis. Built-in features remain available. Existing plugin files and settings are retained while disabled; set `1` only for tenants whose code you trust, with host filesystem quotas and tested container isolation. Quarantine still overrides this setting. Subdomain mode only: refused in port and onion mode, where the portal and the wikis share cookies. |
| `HOSTING_TENANT_PLUGIN_DENYLIST` | none | Plugin ids tenants may never load. |
| `HOSTING_TTS_GPU_TENANT_TOKENS` | `1` | Give each wiki its own GPU speech token derived from the master token instead of the master token itself. Needs the GPU server from this release; set `0` only while an older GPU server is still running. |
| `HOSTING_FEDERATION_INSTANCES` | none | Wiki ids (from the portal) whose tenants get `BW_FEDERATION_ENABLED=1`; `*` for every wiki. |
| `MAX_INSTANCES_PER_ACCOUNT` | 5 | Wikis per customer. |
| `INSTANCE_DURATION_DAYS` | 14 | Lifetime of a new wiki. |
| `INSTANCE_STORAGE_LIMIT_MB` | 500 | Storage per wiki. |
| `INSTANCE_MAX_UPLOAD_MB`, `INSTANCE_MAX_ATTACHMENT_MB` | 16, 100 | Upload limits passed to tenants. |
| `INSTANCE_MEMORY_LIMIT_MB` | 768 (at least 128) | Container memory. |
| `INSTANCE_CPU_LIMIT` | `1.0` | Container CPUs. |
| `INSTANCE_PIDS_LIMIT` | 256 (at least 32) | Container processes. |
| `INSTANCE_NOFILE_LIMIT` | 1024 (at least 128) | Container open files. |
| `INSTANCE_STARTUP_TIMEOUT_SECONDS` | 120 (30–600) | How long a starting wiki may take. |
| `HOSTING_AGENT_MAX_MEMORY_MB`, `HOSTING_AGENT_MAX_CPUS`, `HOSTING_AGENT_MAX_PIDS`, `HOSTING_AGENT_MAX_NOFILE` | 4096, 4, 4096, 65536 | Ceilings the runtime agent enforces whatever the portal asks. |
| `HOSTING_AGENT_MAX_STORAGE_BYTES` | 10737418240 | Finite per-tenant hard byte ceiling on dedicated enforced XFS; positive and divisible by 512. A zero portal plan uses this ceiling. |
| `HOSTING_AGENT_MAX_INODES` | 100000 | Finite hard inode limit per tenant (1–10000000). |
| `HOSTING_AGENT_PROJECT_ID_START` | 1000000 | First private project ID (1–2147483647); dedicate the range and filesystem to this installation. IDs are not reused. |
| `HOSTING_AGENT_STORAGE_RESERVE_BYTES` | 268435456 | Positive reserve in both total-budget and free-space quota admission, plus conservative inode headroom; does not budget independent operator writes or every metadata allocation. |

`HOSTING_BACKUP_ENCRYPTION_KEY` (URL-safe base64 of 32 bytes) may replace the
key file at `HOSTING_BACKUP_KEY_PATH`. `BW_HOSTING_RECOVERY_MAX_WORKERS`
(default 2, 1–16) in `config/app.env` tells the updater how many wikis are
restarted in parallel, which sets how long it waits for all of them to become
healthy. `HOSTING_ROUTES_DIR` is only a placeholder in the example
`deploy/Caddyfile.hosting` (default `/var/lib/bananawiki-routes`): the
directory where the agent writes the per-wiki Caddy routes.

Inside a tenant container: `BW_INSTANCE_GUNICORN_WORKERS` (1),
`BW_INSTANCE_GUNICORN_THREADS` (4), `BW_INSTANCE_STARTUP_TIMEOUT_SECONDS`
(120) and `BW_HOSTING_TTS_WORKER_POLL_INTERVAL_SECONDS` (30) tune the tenant
entry point; the runtime also passes `BW_MANAGED_PLUGIN_QUARANTINE` when an
administrator quarantined a wiki's plugins.

### Sign-up and email

| Variable | Default | Meaning |
|---|---|---|
| `HOSTING_DEFAULT_SIGNUP_MODE` | `open` | Sign-up mode of a new platform: `open`, `invite`, `approval` or `closed`. |
| `HOSTING_EMAIL_PROVIDER` | none | `brevo`, `resend` or `smtp`. Without it no email is sent. |
| `HOSTING_EMAIL_API_KEY` | none | API key for Brevo or Resend. |
| `HOSTING_EMAIL_FROM`, `HOSTING_EMAIL_REPLY_TO` | none | Sender and reply-to. |
| `HOSTING_EMAIL_SMTP_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_TLS` | —, 587, —, —, on | SMTP server. With TLS on, port 465 uses implicit TLS and every other port STARTTLS. |
| `HOSTING_EMAIL_TOKEN_TTL_SECONDS` | 86400 (at least 300) | Lifetime of verification and reset links. |
| `HOSTING_EMAIL_DAILY_LIMIT` | 300 (0 = none) | Messages per day in total (plus 15 per recipient). |

### Archives

| Variable | Default | Meaning |
|---|---|---|
| `HOSTING_IMPORT_CHUNK_BYTES` | 8 MiB | Upload chunk size for imports. |
| `HOSTING_IMPORT_MAX_BYTES` | 10 GiB | Largest archive. |
| `HOSTING_IMPORT_MAX_EXTRACTED_BYTES` | 3 × the above | Largest unpacked size. |
| `HOSTING_IMPORT_MAX_MEMBERS` | 250000 | Most files in an archive. |
| `HOSTING_IMPORT_MIN_FREE_BYTES` | 512 MiB | Free disk space kept during imports. |
| `HOSTING_IMPORT_TEMP_DIR`, `HOSTING_EXPORT_TEMP_DIR` | `<source>/hosting/data/tmp_imports`, `tmp_exports` | Scratch folders. |
| `HOSTING_EXPORT_COMPRESS_LEVEL` | 1 (0–9) | ZIP compression level. |
| `HOSTING_EXPORT_STORE_FILE_BYTES` | 64 MiB | Files larger than this are stored uncompressed. |
| `HOSTING_EXPORT_STORE_EXTENSIONS` | media and archive types | Extensions that are always stored uncompressed. |

`STATIC_SITE_DIR` is only used by the Caddy configuration (the apex website).

## Removed variables

These 1.4 variables are no longer read: `BW_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES`,
`BW_SITE_EXPORT_COMPRESS_LEVEL`, `BW_SITE_EXPORT_STORE_FILE_BYTES`,
`BW_SITE_EXPORT_STORE_EXTENSIONS`, `BW_DB_OBSERVABILITY`,
`BANANAWIKI_SKIP_BACKGROUND_SERVICES` (use `BW_BACKGROUND_JOBS=0`) and
`HOSTING_DEBUG`. Leaving them set does no harm.
