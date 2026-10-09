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

The latest build upgrades databases at schema 3 to 6 to schema 7, with an
automatic database backup first. Schema 5 adds a chat upload ledger that keeps
the 24-hour allowance consumed when a message or conversation is deleted.
Retained uploads from the preceding 24 hours are counted during the upgrade;
uploads deleted before the upgrade cannot be reconstructed. Schema 6 records
whether an owner or superuser imposed a suspension, indexes former user names,
and grants `page.view_all` and `category.view_all` to every saved permission
set, because both are now enforced (see [Permissions](#permissions)). Schema 7
counts read-aloud claims lost with their worker: upgrade the web application
and the read-aloud worker together, because a worker of an earlier release
cannot open a schema 7 database. Hosting databases move to version 5, which
keeps collaborators and merge records when the account that created them is
deleted.

Already on 1.6.0? These changes of the latest build need attention (details in
[Behaviour changes](#behaviour-changes)):

* Over HTTPS (or with `BW_SECURE_COOKIES=1`) the wiki session cookie is named
  `__Host-<name>` (for example `__Host-bw_session`). Users stay signed in: a
  session moves to the new name on its first HTTPS request. Going back to an
  earlier release signs HTTPS users out once, and tools that read the cookie
  by name over HTTPS must use the new name.
* Deleting an account no longer deletes its kanban tickets and comments on
  other people's boards: they go to the board owner (comments start with a
  note naming the former author), its own boards go to an administrator, and
  it is removed from assignees and shares.
* Read aloud: schema 7 (upgrade the web application and the worker together).
  A job whose worker dies three times is marked failed; `BW_TTS_MAX_JOB_SECONDS`
  (default 3600) bounds a job. On Linux, Piper runs in a child process per job
  (about 1-2 s more per job) whose memory can be limited with
  `BW_TTS_PIPER_MEMORY_MB`.
* Managed servers with more than about 76,000 files in `data/`: earlier 1.6
  controllers fail every backup and update of such an installation once the
  services have stopped (they start again), so the installed controller cannot
  update to this release. Run that one update with this release's controller
  from a checkout of the update source:
  `git clone --branch BRANCH SOURCE_URL /root/bananawiki-update`, then
  `sudo /root/bananawiki-update/banana --root /opt/bananawiki update`. Later
  updates use the installed controller as usual. Packages can now hold up to
  `BANANA_PACKAGE_MAX_FILES` files (default 1,000,000).
* Hosting dates (wiki expiry, suspensions, invites, banners) accept the years
  1900-9998; expiries already stored after 9998 are kept and can be shortened.
* Hosting platform backups no longer stop at the first wiki that fails: such a
  wiki is listed as incomplete on the settings card (and in
  `backup_manifest.json`) while the others are saved, and a backup is
  "complete" only when every wiki was saved in full. Each wiki may add at most
  its storage limit plus a tenth and 64 MiB. Tenant archives with bzip2 or LZMA
  members are refused.
* A response is abandoned once a write to the client has waited
  `BW_WRITE_TIMEOUT` seconds (`HOSTING_WRITE_TIMEOUT` for the portal; default
  300, `0` waits forever). Readers that kept taking in 256 KiB to 4 MiB per
  period were not cut off in tests; raise the value to serve slower links (see
  [deployment](docs/deployment.md#reverse-proxy)). Gunicorn logs each such
  disconnect as `Socket error processing request` with `TimeoutError: timed
  out`.
* Hosting with `HOSTING_ALLOW_TENANT_PLUGINS=1`: when an update fails only
  because wikis with their own plugins do not come back, their plugins are
  quarantined and the update is kept. The update result and `status` list them
  as `quarantined_tenants`; a portal administrator lifts the quarantine on the
  wiki's page, and the plugins it switched off stay off until the wiki's
  administrators switch them on. Updating to a release without the
  `hosting-admin instance quarantine-plugins` command rolls back as before.
* A hosted wiki's export (grace-period and administrator downloads) fails with
  a message when its files or its database copy exceed the wiki's storage limit
  plus a tenth and 64 MiB; further hard links to a file are listed in the
  export's `manifest.json` instead of being stored again.
* Whole-site imports refuse archives with encrypted members or members
  compressed other than stored or deflated (BananaWiki exports never contain
  them).
* Hosting: account merges done before this release may have left the merged
  account as a collaborator of its own wikis, which would keep it access after
  it transfers one. To remove such rows, run once against the portal database
  (`HOSTING_DATABASE_PATH`):
  `sqlite3 hosting.db "DELETE FROM instance_collaborators WHERE (instance_id, account_id) IN (SELECT id, account_id FROM instances);"`

* `page.view_all` and `category.view_all` are enforced. The upgrade grants
  them to every saved permission set, so nothing changes until an
  administrator saves permissions without them.
* `DELETE /api/v1/categories/<id>?page_action=delete` also needs the `pages`
  write scope and refuses hidden, forbidden or protected pages; API tokens
  expire at most 10 years ahead.
* Renaming or deleting an account no longer edits pages; members rename
  themselves at most 3 times a day and former names stay reserved.
* Suspensions recorded before the upgrade are lifted by an owner or
  superuser, not by the suspended administrator.
* On hosts that forbid public wikis, custom pages are for signed-in members.
* Hosting: "Pause deletion countdown" really pauses the deletion; tenant
  images are rebuilt on a fresh base at every update, so the Docker registry,
  the Debian mirror and PyPI must be reachable during updates (a mirror or
  PyPI outage records the commit as failed until `update --retry-failed`).
* Hosting needs hard XFS project quotas: the runtime agent assigns byte and
  inode limits before it seeds, imports, copies, restores or starts a wiki,
  and refuses on any other storage. Put the installation's `data/instances/`
  on a dedicated XFS filesystem with project quotas enforced (`prjquota`, see
  [docs/hosting.md](docs/hosting.md)) **before updating**. Otherwise an
  update of a server with running wikis keeps them down for the whole
  readiness timeout (up to 2 hours with many wikis), is then rolled back and
  records the commit as failed until `update --retry-failed`; on a server
  without running wikis it completes, but no wiki can be created, started or
  restored until the storage enforces project quotas.
* Hosting now runs only on Linux x86_64: on any other architecture the
  portal and the maintenance service refuse to start and the runtime agent
  refuses to launch any wiki, so an update is rolled back. Do not update an
  arm64 hosting server.
* Hosting in port or onion mode (managed servers without a domain, or
  `BASE_DOMAIN` empty, an IP address or `localhost`) refuses
  `HOSTING_ALLOW_TENANT_PLUGINS=1`, because the portal and the wikis share
  cookies there: the portal, the maintenance service and `hosting-admin`
  refuse to start, so an update is rolled back and records the commit as
  failed. Remove the setting from `config/app.env` (or set it to `0`, or
  configure a domain for subdomain mode) **before updating**; after a
  rolled-back update, do the same and run `update --retry-failed`.
* Hosting: a new wiki stays reserved as "Creating" until its provisioning
  finishes (hosting database version 4; existing wikis are marked ready). An
  interrupted or failed creation is never started or recovered: terminate it
  and create it again.
* REST API and JSON uploads: JSON with non-finite numbers, unpaired
  surrogates or more than 64 nesting levels is refused (400 on the API).
  `Idempotency-Key` replays re-check current access and may answer 409
  `idempotency_replay_unavailable` without running again; server, storage
  and routing errors under `/api/v1` carry `code` and `request_id`.
* The default source link (`BW_SOURCE_URL`) and the default repository of
  new managed installations are now `https://github.com/BananaSuite/BananaWiki`.

Hosted third-party Python plugins now require `HOSTING_ALLOW_TENANT_PLUGINS=1`
in the operator's hosting environment. The default is disabled; existing
plugin files and settings are kept, and built-in features are unaffected.
Operators who trust their tenants' custom plugins must explicitly enable
this setting and restart the portal and tenant containers. The opt-in
applies only in subdomain mode: port and onion mode refuse it (see
[addresses](docs/hosting.md#addresses)). Quarantined wikis always keep
external plugins disabled. Application storage limits still
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
  `https://github.com/BananaSuite/BananaWiki`; if your server follows
  another URL that no longer receives releases, change it with
  `sudo bananawiki source set --repo https://github.com/BananaSuite/BananaWiki.git --branch main`.
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
  step is finished only when every wiki that was serving is healthy and
  routed again. If the readiness checks fail, the old units, `app.env` and
  Caddyfile are put back.
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
* Members can rename themselves at most 3 times a day. A former name stays
  reserved for its account until that account is deleted, and
  `/users/<former name>` leads to it.
* Account merges in "lock" mode demote the source account to a plain user,
  clear its permission overrides, replace its password and suspend it. A merge
  can no longer remove the last active administrator.
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
* **`page.view_all` and `category.view_all` are enforced.** Without
  `page.view_all` an account reads no page, including federated copies;
  without `category.view_all` it sees no category in the navigation, category
  lists and search (category pickers in forms are unchanged). Both were shown
  but never checked before, so schema 6 grants them once to every saved
  individual permission set and custom role: nothing changes on upgrade.
  Untick them afterwards only for accounts that must not read. To list the
  accounts that would read nothing:
  `SELECT username FROM users u WHERE role IN ('user', 'editor') AND
  custom_role_id IS NULL AND EXISTS (SELECT 1 FROM user_category_access a
  WHERE a.user_id = u.id) AND NOT EXISTS (SELECT 1 FROM user_permissions p
  WHERE p.user_id = u.id AND p.permission_key = 'page.view_all');`
* **Deleting a category with its pages** (web, API, bulk delete) is all or
  nothing: hidden pages, pages the account may not delete and protected,
  checked-out or already scheduled pages refuse the whole deletion before
  anything changes; deletion slowdown still applies.

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
* `DELETE /api/v1/categories/<id>?page_action=delete` also needs the token's
  `pages` scope with write access. It answers 403 (`category_pages_hidden`,
  `category_pages_forbidden`) or 409 (`category_pages_blocked`) with the
  affected slugs, and 202 when pages were only scheduled for deletion.
* Token expiry dates are at most 10 years ahead (`expiry_too_far`); existing
  tokens keep their expiry and can still be revoked.
* Canvas: a locked wiki-page node keeps its page link. An operation or a
  document that changes only the page of a locked node is now applied without
  that change instead of answering 400 `locked`, and a wiki-page node sent
  without `page_id` or `page_slug` keeps its stored link.
* JSON request bodies with non-finite numbers (`NaN`, `Infinity`), unpaired
  Unicode surrogates or more than 64 nested objects/arrays are refused with
  400. The same check applies to uploaded JSON files: Kanban and canvas
  imports, themes, language packs, plugin manifests, 1.4 `site_export.json`
  and federation snapshots.
* `Idempotency-Key`: a keyed `POST` commits its changes together with its
  replay record. A replay re-checks the token owner's current role and access
  to the pages, categories, canvases and boards it returns; when one became
  unreadable, or the first answer was larger than 2 MiB, it answers 409
  `idempotency_replay_unavailable` with the first answer's `status` instead
  of replaying or running again.
* Unexpected server errors (500 `internal_error`), temporary storage
  failures (503 `storage_unavailable`) and routing errors under `/api/v1` use
  the error envelope with a generic message and a `request_id` (also sent as
  `X-Request-ID`).

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
  it on. An approval applies the version the reviewer saw: if the proposal or
  the page changed meanwhile, it is refused and the review is shown again.
* **Very large changes** in page history, edit conflicts, contribution reviews
  and kanban history show a coarser comparison or a "too large to display"
  note instead of an exact diff.
* **Custom pages** on hosts that forbid public wikis
  (`BW_FORBID_PUBLIC_MODE=1`, or `BW_FORBID_PUBLIC_BUILDER_PAGES=1` for
  builder pages, the default under managed hosting) are for signed-in members:
  anonymous visitors are sent to sign in.
* **Markdown limits:** only the first `[TOC]` expands, a page embeds at most
  200 videos, boards and canvases, and the page builder's page lists show at
  most 48 pages per document. Content that would cost too much to render is
  shown as escaped source.
* **Mentions:** renaming or deleting an account no longer edits pages. A
  mention of a former name leads to the renamed account, and the former name
  stays reserved for it. Account merges still rewrite the mentions, with a
  history entry on every changed page.
* **Uploads:** still images are re-encoded on upload, which removes EXIF/GPS
  metadata. Profile pictures are limited to 4 megapixels, the background
  image limit is checked before processing, and each account may upload 10
  profile or background images per 10 minutes.
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
* Hosting: pausing the deletion countdown of a terminated wiki now really
  pauses it (1.4 only blocked the owner's download). A terminated wiki whose
  download was blocked in 1.4 is therefore kept until an administrator
  resumes its countdown on its admin page; the paused time is then added to
  its grace period.
* Hosting: every update rebuilds the tenant image on a freshly pulled base
  image with current Debian and Python packages, so the Docker registry, the
  Debian mirror and PyPI must be reachable. If only the base image pull
  fails, the cached base is used and `update` reports `image_warnings`; if
  the Debian mirror or PyPI fails, the commit is recorded as failed until
  `update --retry-failed`. A wiki that does not
  come back is reported in `unready_tenants` instead of keeping the platform
  in maintenance; `recover --abandon` drops an operation that cannot finish.
  If Docker does not answer when an operation begins, nothing is changed.
  See [docs/operations.md](docs/operations.md). The tenant image builds FFmpeg
  and the ACL packages in earlier stages that a refresh alone does not
  rebuild; when the pulled base image changed they are rebuilt too, which also
  needs `ffmpeg.org` and the Debian source archive. The ACL stage installs two
  exact Debian unstable versions (`libacl1` 2.4.0-1, `tar` 1.35+dfsg-6), so
  that rebuild fails once unstable replaces them: the build then falls back
  to the previous base image, still with fresh Debian and Python packages,
  and `update` reports `image_warnings`. Only a build that fails on the
  previous base image too records the commit as failed.
* Hosting: the runtime agent assigns and verifies hard XFS project byte and
  inode quotas before every seed, import, copy, restore and launch. On storage
  without enforced project quotas, changed project identities or too little
  capacity it refuses, and the wiki stays stopped. Existing wikis are adopted
  when they next start (the recovery after the update restarts every
  running wiki that lacks verified quotas). Ceilings default to 10 GiB and
  100,000 inodes per wiki (`HOSTING_AGENT_MAX_STORAGE_BYTES`,
  `HOSTING_AGENT_MAX_INODES`); see [docs/hosting.md](docs/hosting.md).
* Hosting: a new wiki is inserted as a pending reservation and becomes
  running only when its seed, copy or import and its first start have
  finished. Until then, and while a failed creation still awaits its cleanup,
  it counts against the limits, cannot be started, changed or recovered, and
  is cancelled by terminating it. Neither a failure nor a cancellation deletes
  a data folder the creation did not make: when the folder already existed,
  or the portal never handed the creation to the runtime agent, the folder is
  left untouched for an administrator (a creation refused because its folder
  existed is terminated at once and does not count against the limits; the
  refusal is also kept under `HOSTING_PLATFORM_STATE_DIR` in
  `.provisioning-refused/`). The exception is a portal worker killed outright
  while the agent works on a creation: cancelling that creation, or its
  expiry, deletes the folder under its name. See
  [docs/hosting.md](docs/hosting.md).
* Hosting runs only on Linux x86_64. On any other architecture the portal
  and maintenance units, which now start through
  `bananawiki.ops.hosting_entrypoint`, exit with status 78, and the runtime
  agent refuses to launch any wiki (`sandbox_unavailable`).
* The default source link (`BW_SOURCE_URL`) and the default repository of
  new managed installations are now `https://github.com/BananaSuite/BananaWiki`;
  existing installations keep their configured repository.
* Hosting: backups and recoveries no longer start the stopped wiki
  containers again with `docker start`; they remove them and the portal
  starts every wiki marked running again in a new container, as after an
  update.

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
