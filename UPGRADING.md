# Upgrading from BananaWiki 1.4 to 1.6

BananaWiki 1.6 is a rewrite that takes over a 1.4 installation (or any earlier 1.x release at schema 3) **in place**:
same database, same tables and columns, same files, same secret key, same
environment variables, same URLs. Accounts, passwords, pages, history,
sessions ("remember me" cookies included) and API tokens survive. This page
explains how to upgrade each kind of installation, what happens on the first
start, how to go back, and every behaviour change.

The upgrade is covered by an automated test (`tests/test_upgrade_from_1x.py`)
that boots 1.6 on a real 1.4 instance and checks that users, sessions,
tokens, content and files survive.

The interface refresh keeps existing URLs, themes and display preferences.
Hosting account settings have a section index, and administrators open the
account and invite creation forms from their dashboard section headings.
Secondary page actions are in **Page actions**, and new pages and categories
start from **New** in the sidebar. The editor groups less common commands under
**Formatting** and **Insert**; Tab moves to the next field. Administration links
are grouped by task, and the feature list has a search field.
These interface changes require no configuration or database changes.

Managed backups now include the allowed SSH commit signers for required signed
updates. Earlier packages may omit `config/repo.allowed_signers`; after restoring
one, recover the operator's original trust file and configure
`source set --require-signatures FILE` before updating. Failed restores now
restore repository credentials and signer trust along with the previous data.

The latest build upgrades databases at schema 3 or 4 to schema 5, with an
automatic database backup first. The new chat upload ledger keeps the
24-hour allowance consumed when a message or conversation is deleted.
Retained uploads from the preceding 24 hours are counted during the upgrade;
uploads deleted before the upgrade cannot be reconstructed.

Hosted third-party Python plugins now require `HOSTING_ALLOW_TENANT_PLUGINS=1`
in the operator's hosting environment. The default is disabled; existing
plugin files and settings are kept, and built-in features are unaffected.
Operators who trust their tenants' custom plugins must explicitly enable
this setting and restart the portal and tenant containers. Quarantined wikis
always keep external plugins disabled. Application storage limits still
require filesystem quotas for a hard limit against tenant code.

Container builds now use Debian 13 and apply available distribution updates.
Runtime images omit `pip`, `setuptools`, `wheel`, and `ensurepip`; add custom
dependencies during the image build. Rebuild your images to receive these
changes. Instance data and existing plugin files remain in `/data`.

## Before you start

1. **Your 1.4 installation must be at database schema 3**, which every 1.4 release
   since the public release (`2d9ad2f`) writes. Check it:

   ```sh
   sqlite3 /path/to/bananawiki.db 'PRAGMA user_version'     # must print 3
   ```

   Older, pre-ledger installations print `0`. Start the last 1.4 release once
   (it upgrades the database to schema 3), then upgrade to 1.6. 1.6 refuses
   older databases with the message *"this release upgrades version 3 or
   newer. Start the last 1.4 release once to finish its own upgrade"*.
2. **Back up.** Managed servers: `sudo bananawiki backup`. Others: stop the
   wiki and copy the instance directory, the upload folders and the secret key
   (`instance/.secret_key` or your `SECRET_KEY`).
3. **Keep the secret key.** 1.6 uses the same key file. A different key signs
   everyone out and invalidates API tokens and encrypted settings.
4. Read the [behaviour changes](#behaviour-changes) below, in particular if
   you use the REST API, custom pages with scripts, or third-party plugins.

## Managed servers (`banana`)

```sh
sudo bananawiki source show      # which repository and branch the server follows
sudo bananawiki update
sudo bananawiki update           # a second time: see below
sudo bananawiki status
```

* `source show` tells you where updates come from. BananaWiki lives at
  `https://github.com/OverloadedTech/BananaWiki`; if your server follows
  another URL that no longer receives releases, change it with
  `sudo bananawiki source set --repo https://github.com/OverloadedTech/BananaWiki.git --branch main`.
* The **first** `update` is carried out by the 1.4 updater that is installed
  on the server: it fetches 1.6 (a fast-forward of 1.4), builds the release,
  writes the `before-update-*.tar.gz` package, switches to 1.6 and waits for
  `/health`. If the new release does not become healthy it restores the 1.4
  release and data by itself.
* From then on the installed command runs the **1.6 controller**. The
  **second** `update` (or `sudo bananawiki restart`) finds no new commit and
  converges the systemd units to the hardened 1.6 units. On a hosting server
  this installs the runtime agent (`bananawiki-agent.service`), removes the
  portal from the `docker` group and adds `BW_RUNTIME_AGENT_SOCKET` to
  `config/app.env`. Wikis are then routed by Caddy straight to their
  containers instead of through the portal, so the same step creates the
  agent's routes directory (`/var/lib/bananawiki-routes`) and, if the
  Caddyfile was installed with `bananawiki proxy --install`, re-renders it to
  import that directory (Caddy is reloaded only when the file changes). The
  step is finished only when every running wiki is healthy and routed. If the
  readiness checks fail, the old units, `app.env` and Caddyfile are put back.
  **On a hosting server the wikis are not reachable between the two updates**,
  so run the second one right away. A Caddyfile you wrote yourself is left
  alone; `update` then warns until it contains
  `import /var/lib/bananawiki-routes/*.caddy` (see `bananawiki proxy`).
* `status` should then show `"units_current": true` (and on hosting servers
  `"runtime_agent": true`).

Your `config/app.env`, update policy, source credentials and backup settings
are kept; the configuration files keep their 1.4 format. Automatic updates
(if you enabled them) upgrade the server the same way on their next run.

## Installations from a Git checkout

```sh
cd /path/to/BananaWiki
git pull
python -m pip install -r requirements.txt       # inside the wiki's virtual environment
# restart the service, e.g.
sudo systemctl restart bananawiki
```

The start command does not change: `gunicorn -c gunicorn.conf.py wsgi:app`
(or `gunicorn … wsgi:app`) keeps working. The read-aloud worker is still
`python scripts/tts_worker.py`.

Commands that changed:

| 1.4 | 1.6 |
|---|---|
| `python -c 'import config; print(config.SETUP_TOKEN)'` | `bananawiki setup-token` |
| `python reset_password.py …` | `bananawiki reset-password NAME` |
| `./dev.sh` | `bananawiki serve` |
| `./start.sh`, `setup_wizard.py` | Gunicorn directly, or `banana install` (see [docs/deployment.md](docs/deployment.md)) |

`bananawiki` is available after `python -m pip install -e .`; without
installing the package use `python -m bananawiki.cli …` from the checkout.

**Data folders.** 1.4 kept some files inside the source tree by default. On
the first start, for every folder whose variable is **not** set, 1.6 moves
the files into the instance directory (it never overwrites a file; a file
that cannot be moved is logged, and setting the variable keeps the old
location):

| Files | From (1.4 default) | To | Variable |
|---|---|---|---|
| Page images, avatars | `app/static/uploads/` | `instance/uploads/` | `BW_UPLOAD_FOLDER` |
| Custom favicons (`custom_*`) | `app/static/favicons/` | `instance/favicons/` | `BW_FAVICON_UPLOAD_FOLDER` |
| Attachments | `instance/attachments/` (source tree) | `<instance>/attachments/` | `BW_ATTACHMENT_FOLDER` |
| Chat files | `instance/chat_attachments/` | `<instance>/chat_attachments/` | `BW_CHAT_ATTACHMENT_FOLDER` |
| Kanban files | `instance/kanban_attachments/` | `<instance>/kanban_attachments/` | `BW_KANBAN_ATTACHMENT_FOLDER` |
| Custom page files | `instance/custom_page_files/` | `<instance>/custom_page_files/` | `BW_CUSTOM_PAGE_FILES_FOLDER` |
| Uploaded interface languages | `translations/*.json` (other than `en`, `it`) | `<instance>/translations/` | — |

The log moves from `logs/bananawiki.log` to `<instance>/logs/bananawiki.log`
unless `BW_LOG_FILE` is set. (Managed servers already set every folder
variable, so nothing moves there.)

## Docker

1.4 had no official image for a single wiki. 1.6 ships a `Dockerfile` and a
`compose.yaml` (see [docs/deployment.md](docs/deployment.md#docker-and-docker-compose)).
The image uses `/data` as instance directory with the same folder names as
1.4 hosted wikis (`bananawiki.db`, `.secret_key`, `uploads/`, `attachments/`,
`chat_attachments/`, `kanban_attachments/`, `custom_page_files/`, `tts/`).
To move a 1.4 wiki into it, either copy its instance directory and folders
into the volume in that layout (owned by UID 10001), or export it from the
1.4 **Admin → Site migration** page and import the archive in the new wiki
(this replaces the new wiki's accounts with the old ones).

Wikis on a 1.4 hosting platform are upgraded by upgrading the platform: the
updater builds the 1.6 tenant image and every running wiki is restarted on it.

## Desktop app

Install BananaWiki Desktop and point it at the folder the 1.4 Easy
Deployment App used (the `bananawiki/` folder next to the old program): it is
opened in place. A ZIP made with **Export All** in the old app can be
restored with **Restore…**. See [docs/desktop.md](docs/desktop.md).

## What happens on the first start

1. **Backup.** Before the schema changes, an online copy of the database is
   written to `<instance>/backups/pre-upgrade-v3-<date>-<time>.db` (mode 0600).
2. **Data folders** are moved as described above (non-managed installations).
3. **Migration 3 → 4**, in one transaction:
   * references to non-existent users (1.4 wrote `-1` for "system") become
     empty, or rows whose required parent is gone are removed, as a foreign key
     cascade would have done;
   * every timestamp is rewritten to `YYYY-MM-DD HH:MM:SS` UTC;
   * the pre-custom-role editor category restrictions are folded into the
     current ones;
   * secrets of retired features are erased (the Telegram bot token,
     feedback bot settings);
   * `pages.revision` (edit-conflict detection) and `job_runs` (background job
     leases) are added, plus indexes and a full-text search index;
   * each feature adds what it needs (for example the leaderboard's statistics
     table and the platform sign-in links).
4. **Migration 4 → 5** adds the chat upload usage ledger and backfills the
   preceding 24 hours of retained direct-message and group attachments.
5. Every later start only reads the schema version.

Tables of plugins that 1.6 no longer ships are left untouched. When you no
longer need their data, `bananawiki db prune-retired` drops them after another
backup.

If the migration fails, nothing is changed and the wiki does not start; the
log names the problem. The `pre-upgrade` copy is there in any case.

## Going back to 1.4

1.4 refuses a database at schema 4 or 5, so going back always means restoring the
data from before the upgrade. **Changes made after the upgrade are lost.**

* **Managed:** `sudo bananawiki rollback`. It restores the package the 1.4
  updater wrote before the upgrade (source, data and configuration), with the
  1.4 units. `sudo bananawiki backup` first if you want to keep the 1.6 state.
* **Checkout:** stop the wiki; `git checkout` the 1.4 commit and reinstall its
  `requirements.txt`; replace `bananawiki.db` with
  `<instance>/backups/pre-upgrade-v3-*.db` and delete `bananawiki.db-wal` and
  `bananawiki.db-shm`; either move the relocated folders back or set the
  `BW_*` folder variables to their new locations; start 1.4.
* **Docker/desktop:** restore the backup of the data you took before.

## Behaviour changes

### Accounts, sessions and administration

* **Sessions:** 1.4 cookies that carry a server-side session keep working.
  Cookies without one (1.4 issued them to suspended users signing in and via
  `/admin` and `/session-conflict/force`) are signed out.
* Without **remember me** the cookie now ends when the browser closes (1.4
  kept it for 7 days).
* **Administrators can no longer demote, suspend, delete or reset the password
  of other administrators**: only owners and superusers can. Owners are
  changed only by themselves, superusers only by themselves. The account made
  at `/setup` on a new wiki is an owner and a superuser; on an upgraded wiki,
  make sure at least one trusted person is an owner or superuser.
* **A suspended administrator lifts their own suspension only** when an
  administrator who still has an account imposed it (1.4 allowed any).
  Suspensions imposed by an owner or superuser, and every suspension recorded
  before the upgrade, are lifted by an owner or superuser.
* **Sign-up approval is enforced**: pending and denied accounts can no longer
  use the wiki (1.4 let them in). Check **Admin → Users** for pending accounts
  after upgrading if you had approval turned on.
* Sign-in limits: 20 failures per address and 8 per account name in 15
  minutes, across workers; a successful sign-in no longer resets the
  address counter.
* The setup token is accepted only in the setup form, not in the URL.
* The guided tour no longer shows other roles' real pages: it illustrates
  them.
* Bot protection tokens are single-use and are also checked in test
  environments.

### Permissions

* **Every permission in the catalogue is enforced.** Editors whose individual
  permissions or custom role lack `page.create`, `page.edit_all`,
  `page.edit_metadata`, `history.revert`, `category.*` etc. lose those actions,
  which 1.4 allowed despite the setting. Review **Admin → Custom roles** and
  individual permissions after the upgrade.
* Category actions (create, rename, move, delete) need write access to the
  categories involved.
* Deleting uploaded images (`/api/upload/delete`) is for administrators only
  and refuses images still in use.
* The **page builder** needs the right to edit the page, whatever
  `page_builder_access` says (plain users can no longer edit pages through it).
* A permission of a switched-off feature is never granted.

### REST API

* Error bodies are `{"ok": false, "error": "…", "code": "…"}`; timestamps are
  ISO 8601 UTC (`2025-01-31T09:30:00Z`).
* Tokens are revoked when their account's password changes (anywhere), when
  an administrator resets it, and when the account is suspended.
* Deleting a category needs `category.delete`; changing a page's title or
  category needs `page.edit_metadata`; userbot profile updates need
  `profile.edit_own`.
* The rate limit counts all tokens of an account together; the userbot key no
  longer counts towards the token limit.
* Maintenance mode answers 503 and accounts with a pending password change or
  onboarding get 403 (1.4 let the API bypass both).
* Banana Mode and its endpoints (`/api/v1/banana-mode`, `/admin/banana…`)
  are gone; use maintenance mode.
* New: history, search, single category, `expected_revision`, paging,
  `/api/v1/openapi.json`. See [docs/api.md](docs/api.md).

### Features

* **Switching a feature off** stops its background jobs (temporary items and
  pending deletions pause; 1.4 kept deleting) and never resets its settings
  (1.4 reset the upload policy to "allow all" when attachments were switched
  off).
* **Events fire on every path**: `page.deleted` and friends now also fire for
  bulk deletions, deletion-slowdown purges, temporary items, federation forks
  and platform sign-up. Plugins that listen to them see more events.
* **Deleting a chat message erases its text and attachments** for everyone,
  including administrators (1.4 only hid it). A daily job also erases what
  1.4 kept of messages deleted before the upgrade.
* **Custom pages** with HTML, CSS or JavaScript run in a sandboxed document
  (opaque origin): their scripts no longer see the wiki's cookies or call its
  APIs as the visitor.
* **Whole-site import** never writes code, configuration or the secret key
  (1.4's "restore system files" option is gone), and it **replaces
  everything**, accounts included. It is refused under managed hosting unless
  `BW_ALLOW_SITE_IMPORT=1`.
* **Read aloud:** `tts_enabled_languages` is enforced (pages are read only in
  enabled languages); the GPU server needs a token of at least 16 characters;
  `langdetect` is no longer installed by default (a built-in English/Italian
  heuristic is used; install `langdetect==1.0.9` by hand for other
  languages); generating audio needs an account.
* **Page builder** documents are saved as version 2 (1.4 documents are read
  and upgraded, and saved as version 2 the next time the page is published).
  1.4 cannot open version 2 documents: this only matters if you carry pages
  back to 1.4 by hand instead of restoring the pre-upgrade backup (see
  [Going back to 1.4](#going-back-to-14)); their Markdown content stays
  readable there. Custom pages made with the builder are stored as Markdown
  wiki pages plus a new `custom_pages.builder_json` column, so releases that
  do not know the builder show their Markdown version.
* **Kanban** revert restores the board without deleting tickets, comments or
  attachments; each board keeps its newest 200 history entries.
* **Canvases:** a single share no longer opens every public canvas to that
  user; archived canvases are visible only to their creator and
  administrators.
* **Assessments** show the answer key only to managers and to takers who
  cannot try again.
* **Leaderboard** figures are computed differently (size change against the
  previous revision), so numbers differ from 1.4.
* **Contribution approval** is switched by its own setting
  (`contribution_approval_enabled`); the migration keeps it on where 1.4 had
  it on.
* **Mentions:** renaming or deleting an account no longer edits pages. A
  mention of a former name leads to the renamed account, and the former name
  stays reserved for it. Account merges still rewrite the mentions, with a
  history entry on every changed page.
* **Uploads:** still images are re-encoded on upload, which removes EXIF/GPS
  metadata.
* **Plugins:** third-party plugins load only at start-up (enabling one takes
  effect after a restart, which the plugin page can trigger); 1.4 plugins run
  through an adapter whose limits are listed in
  [docs/plugins/README.md](docs/plugins/README.md#plugins-written-for-bananawiki-1x).
* **Bulk delete** (`/admin/bulk`) runs every deletion through the owning
  feature, so page protection, reservations and deletion slowdown apply.
* **Notifications:** the per-feature counters of 1.4 are replaced by one
  "needs your attention" count (red dot on the account menu) and the
  `/attention` page. Accounts gain an optional email address (`users.email`,
  only used for notifications) and two opt-out flags; the site gains the
  notification and mail-server settings (`attention_email_*`,
  `decision_email_enabled`, `public_base_url`, `mail_*`). Email stays off
  until an administrator enables it in **Admin → Notifications**.

### Configuration and operations

* Default `BW_LOGGING_LEVEL` is `medium` (was `verbose`).
* Data folders default to the instance directory (see above).
* No longer read: `BW_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES`,
  `BW_SITE_EXPORT_COMPRESS_LEVEL`, `BW_SITE_EXPORT_STORE_FILE_BYTES`,
  `BW_SITE_EXPORT_STORE_EXTENSIONS`, `BW_DB_OBSERVABILITY`,
  `BANANAWIKI_SKIP_BACKGROUND_SERVICES` (use `BW_BACKGROUND_JOBS=0`),
  `HOSTING_DEBUG`.
* New: `BW_WORKERS`, `BW_THREADS`, `BW_WORKER_TIMEOUT`, `BW_ACCESS_LOG`,
  `BW_SECURE_COOKIES`, `BW_BACKGROUND_JOBS`, `BW_ALLOW_SITE_IMPORT`, and for
  notification emails `BW_MAIL_PROVIDER`, `BW_MAIL_FROM`, `BW_MAIL_REPLY_TO`,
  `BW_MAIL_API_KEY`, `BW_MAIL_TIMEOUT`, `BW_SMTP_HOST`, `BW_SMTP_PORT`,
  `BW_SMTP_SECURITY`, `BW_SMTP_USERNAME`, `BW_SMTP_PASSWORD`, `BW_BASE_URL`
  (they take precedence over the administrator's settings). See
  [docs/configuration.md](docs/configuration.md).
* Hosting: approval emails are sent by the maintenance service for every
  queue (accounts, feature requests, account merges), to every administrator
  with an address when *Email every administrator* is on and to the 1.4
  approval address; the "immediate" mode no longer sends from the sign-up
  request, so an email arrives within one maintenance interval. The 1.4 digest
  interval in hours becomes `approval_notify_interval_minutes`; a daily mode
  is new. Accounts gain `attention_emails` and `language`.
* Gunicorn no longer preloads the application; background jobs run in every
  worker with a lease, so each job still runs once.
* Updates take an online snapshot first, so downtime no longer grows with the
  data size; `before-update-*` packages, old releases and old tenant images
  are pruned (keep count: `updates enable --keep-backups`).
* Hosting: the portal loses Docker access (runtime agent);
  `HOSTING_CONTAINER_IMAGE` defaults to `bananawiki-tenant:latest`; the portal
  no longer asks public IP-echo services for its address; tenant logs go to
  Docker's rotated log driver; tenant databases are migrated only inside the
  tenant container.

### Removed

* Obsidian vault sync (`scripts/obsidian_sync.py`); use the REST API.
* Banana Mode.
* Joke audio conversion of chat and kanban uploads (`.mp5`/`.mp7`).
* The BananaChat installation modes of the `banana` controller (only `wiki`
  and `hosting` remain) and the shared-file manifests with BananaChat.
* `setup_wizard.py`, `dev.sh`, `start.sh`, `reset_password.py`.
* The `legacy_redirect/` daemon; use `deploy/Caddyfile.legacy-redirect`.
* The hosting portal's Arabic mirror (right-to-left) mode.
* Demo wiki spawning on the hosting portal (`/admin/spawn-demos` only shows a
  notice).
* The hosting admin CLI (`python -m hosting.admin_cli`); use the portal's
  administrator pages.
* The dead `api_tokens` and `userbot_api_tokens` tables are no longer used
  (drop them with `bananawiki db prune-retired`).

The eight plugins retired in late 1.4 (`banana_ai`, `bw_oauth_provider`,
`file_manager`, `git_override`, `oauth_login`, `feedback`, `meetings`,
`beta_testers`) and other 1.4-only plugins stay listed as *retired (inert)* on
the plugin page; nothing under those ids loads.
