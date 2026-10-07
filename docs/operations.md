# Operations

Running a wiki day to day: updates, backups, restores, background jobs, logs,
health checks, and the two command-line tools.

* **`bananawiki`** (the Python package's command, also `python -m bananawiki.cli`)
  administers one wiki: database, accounts, jobs, export. It reads the same
  `BW_*` variables as the server, so run it with the server's environment and
  user.
* **`banana`** (the `./banana` script in the source tree, installed on a
  managed server as `/usr/local/bin/bananawiki`, or under the name given to
  `install --name`) manages a server installation: install, update, backup,
  restore, rollback, services. It needs root.

On a managed server the installed `bananawiki` command is `banana`. It also
passes the application commands (`create-admin`, `reset-password`, `db`,
`jobs`, `config`, `setup-token`) to the running release, as the service user,
with `config/app.env` loaded. So `sudo bananawiki reset-password alice` works
there too.

## Health and status

* `GET /health` (also `/healthz`) answers `{"status": "ok"}` with 200 when the
  database answers, or `{"status": "unavailable"}` with 503. It needs no
  session, is not rate limited, and keeps answering while the updater's
  maintenance file exists, so a load balancer or the updater can use it.
* `sudo bananawiki status` prints the mode, deployed revision, update source
  and policy, whether maintenance or a recovery is pending, whether the
  systemd units are current, the last operation and update, local packages
  and the encrypted-backup status. On hosting servers `tenant_image_built`
  is when the running tenant image was built, i.e. how old its distribution
  packages are.
* **Admin → Dashboard** shows request, page-view and error counts;
  **Admin → Server** has the error log and a restart button.
* `bananawiki db check` runs SQLite's integrity check and prints the schema
  version. It exits with 1 when the check fails or the database is newer than
  the installed release.

## Logs

| What | Where |
|---|---|
| Application log | `BW_LOG_FILE` (default `<instance>/logs/bananawiki.log`, rotated at 10 MB, 5 files kept); also on standard error. Managed servers: `/opt/bananawiki/data/logs/bananawiki.log`. |
| Web server and access log | standard output/error of Gunicorn: `journalctl -u bananawiki` on managed servers, `docker compose logs wiki` with Compose. |
| Read-aloud worker | `journalctl -u bananawiki-tts`. |
| Updates, backups, restores | `sudo bananawiki status` (`last_operation`), `/opt/bananawiki/config/` history, `journalctl -u bananawiki-update` and `-u bananawiki-backup` for scheduled runs. |
| Security events | **Admin → Audit log** (sign-ins, account and role changes, deletions, settings). |

`BW_LOGGING_LEVEL` sets the detail (`medium` by default). **Admin → Server →
Error log** shows the end of the log file with passwords, tokens and cookies
masked.

## Updates

### Managed servers

```sh
sudo bananawiki update
```

What happens:

1. The configured Git source is fetched with a hardened Git (no hooks,
   credential helpers or redirects). If nothing changed, the command only
   brings the systemd units up to date and reports `current`.
2. The new commit must be a **fast-forward** of the installed one. Otherwise
   the update pauses; `--allow-divergent` accepts a reviewed switch to other
   history. If signatures are required, the commit must be SSH-signed by an
   allowed key.
3. The release is prepared next to the running one (new virtual environment,
   wheels only; on hosting servers the tenant image). The wiki keeps serving
   meanwhile. A failure here leaves everything as it was and records the
   commit as failed. The tenant image is built on a freshly pulled base image
   and runs the Debian upgrade and the package installation of its final
   stage again (no cached layers), so each update also brings the wikis the
   distribution and Python security fixes published so far. The FFmpeg and
   ACL build stages before it are rebuilt only when the pull brought a new
   base image, which then also needs `ffmpeg.org` and the Debian source
   archive. If the registry cannot be reached (or
   the pull takes more than 10 minutes), the image is built on the base
   image already on the server and the result carries `image_warnings`. The
   Debian upgrade and the package installation have no such fallback: if the
   Debian mirror or PyPI cannot be reached, preparation fails and the commit
   is recorded as failed, so automatic updates skip it until
   `update --retry-failed`. The base image a pull replaced is removed after
   the build. Every release's
   image has its own package layers, so plan Docker's disk for the full size
   of each kept tenant image (the current one, the previous one and those of
   the releases kept for rollback).
4. A snapshot of `data/`, `site/` and the configuration is taken while the
   wiki still runs (consistent SQLite copies, hard links for uploads; on
   hosting servers the files of hosted wikis are always copied, by
   descriptor and without following links, because their containers keep
   writing them).
5. The services stop, the snapshot is refreshed (only what changed), the
   `current` link switches to the new release, units are rewritten, and the
   services start. The database is upgraded by the new release when it
   starts.
6. If the new release does not pass its readiness checks (`/health`; on
   hosting servers also the published wiki routes and every wiki that
   answered its own health check when the update began and is still marked
   running), the snapshot is put back, the old release is switched back in
   and started. The result is `rolled_back`.
7. After success, a portable package `backups/before-update-<time>.tar.gz` is
   written from the snapshot **after** the site is back up, verified as a
   restore would verify it, and only then recorded as the rollback target
   (a package that fails verification is deleted, the update reports a
   `backup_warning` and the previous rollback target stays). Older `before-update-*` and `auto-*` packages beyond
   the retention count (3 by default), old releases and old tenant images
   are removed.

Downtime is the time to stop, copy what changed since the snapshot, and start.

On hosting servers one wiki never keeps the platform in maintenance. A wiki
that was already failing when an operation began (for example one whose
directory is missing, or one that fails while its maintenance marker exists)
is not waited for. A wiki that served before a backup, a recovery or a
`start` and does not serve again, whatever its health check answers, is
waited for up to the readiness timeout and then reported, and the operation
still ends; so is one the portal can no longer start. Every operation
removes the wiki containers it stopped rather than starting them again: the
runtime agent lets a wiki serve only after verifying its storage, so the
portal's maintenance service starts each wiki marked running again in a new
container (a plain `docker start` would leave it waiting for that check). Such
wikis, and those whose container was running but that were already failing
and still do not serve once an update, a backup, a restore or a recovery is
complete (checked once at the very end, after any package is written; this
check never changes the outcome), are listed as
`unready_tenants` in the result and in `status` (`last_operation`); why a
wiki that was waited for does not serve is recorded in
`config/last-readiness-failure.json`. Only an update or a restore is rolled
back, and a `restart` fails, when a wiki that served before does not serve
again. If Docker does not answer when a backup, an update, a restore or a
`restart` begins, the command fails before anything is stopped (for an
update, before the new release's tenant image is built); such an update is
not recorded as a failed commit, so the next automatic run tries it again. A Docker that stops answering later, while readiness is checked,
counts as the application not being ready: the command waits for it up to
the readiness timeout.

Options: `--allow-divergent`; `--retry-failed` retries a commit that failed
before (automatic runs skip such commits).

**Automatic updates** are off by default:

```sh
sudo bananawiki updates enable --interval 60 --keep-backups 3
sudo bananawiki updates status
sudo bananawiki updates disable
```

The timer (`bananawiki-update.timer`) runs `update --automatic`, which does
nothing while the wiki was stopped on purpose, never crosses divergent
history and never retries a failed commit. `--interval` is in minutes (5 to
10080), `--keep-backups` 2 to 30. Anything pushed to the branch you follow
reaches your server, so follow a branch you trust, or require signatures.

**The update source:**

```sh
sudo bananawiki source show
sudo bananawiki source set --repo https://github.com/BananaSuite/BananaWiki.git --branch main
sudo bananawiki source check          # what update would deploy, without deploying
```

Private repositories and forks: `--token-file FILE` (HTTPS token, read from a
file and stored privately; `--username`, default `git`) or `--ssh-key FILE
--known-hosts FILE` (a deploy key and verified host keys). `--clear-credentials`
removes them. `--fallback-branch NAME` is used only when the selected branch
disappears (`--clear-fallback` removes it). `--require-signatures
ALLOWED_SIGNERS` deploys only commits SSH-signed by a listed key
(`--clear-signatures` stops that). The same options work on `install` and
`updates enable`.

### Other installations

* Source checkout: `git pull`, `pip install -r requirements.txt` (or
  `pip install -e .`), restart the service.
* Docker Compose: `git pull && docker compose build --pull --no-cache && docker compose up -d`
  (without `--pull --no-cache` Docker reuses the old base image and package
  layers, and the image gets no security updates).
* Desktop: replace the application; the data folder stays.

Before a release that changes the schema starts, it writes a copy of the
database to `<instance>/backups/pre-upgrade-v<old>-<time>.db`. Upgrading
from 1.4 has its own guide: [UPGRADING](../UPGRADING.md).

## Backups

What to protect: the **database**, the **uploaded files** (every folder in the
instance directory) and the **secret key** (`.secret_key`, or `SECRET_KEY`).
Without the key, sessions, API tokens and encrypted settings (the GPU token,
federation keys) cannot be used.

### Managed servers: portable packages

```sh
sudo bananawiki backup                          # backups/manual-<time>.tar.gz
sudo bananawiki backup --output /root/wiki.tar.gz
```

A package is a gzip tarball with a manifest and checksums of every file: the
data directory (including the secret key), the site, the configuration
(`app.env`, source settings and credentials) and the source of the deployed
commit. **It contains secrets**: store it like a password. The format is the
one 1.4 used; packages move in both directions. The wiki is stopped only for
the moment a consistent copy is taken. Every new package is read back and
verified (checksums, database integrity) before the command reports it; a
package that fails is deleted and the command fails.

Manual packages are never pruned automatically.

### Managed servers: encrypted Git backups

Optional encrypted snapshots in a dedicated private GitHub or Forgejo
repository, encrypted with [age](https://age-encryption.org/).

```sh
sudo bananawiki backups keygen --output /root/banana-recovery.agekey
sudo bananawiki backups configure \
  --forge github --repo https://github.com/YOUR_TEAM/private-backups.git \
  --name wiki-production --username YOUR_BOT \
  --token-file /root/banana-backup.token --key-file /root/banana-recovery.agekey \
  --keep 7 --max-mib 512
sudo bananawiki backups run
sudo bananawiki backups list
sudo bananawiki backups status
```

* **Save the recovery key offline** before relying on these backups: without
  it the snapshots cannot be decrypted, even with repository access.
* Use a separate token with read/write access to that repository only, in a
  file with mode 0600. For Forgejo use `--forge forgejo` and its HTTPS URL.
* Every transfer first checks through the forge's API that the repository is
  private (not public, archived or a mirror).
* `run` creates and verifies a package, encrypts it, uploads it in parts of
  at most 32 MiB, downloads and verifies it again, and only then prunes old
  snapshots of the series (`--keep`, 2–30). `--max-mib` (1–1024, default 512)
  caps the package.
* Snapshots are **authenticated**. age encrypts to the recovery key's public
  recipient, which is stored in the repository, so anyone who can push to it
  could otherwise upload a snapshot that decrypts and verifies, and a restore
  deploys a package's source and `app.env` as root. Each snapshot's
  `index.json` therefore carries an HMAC-SHA256 (keyed from the recovery
  key, which is never uploaded) over the snapshot's series, ID and the
  checksum of every encrypted part. `verify`, `download` and `restore` check
  it before decrypting anything and refuse a snapshot without a valid tag.
  The recovery key is the only secret this needs: nothing else has to be
  saved for a restore on a new server.
* `enable --interval MINUTES` (60–10080, default 1440) schedules
  `bananawiki-backup.timer`; `disable` stops it. The schedule is independent of
  automatic updates. Failures appear in `journalctl -u bananawiki-backup` and
  `backups status`.

Restore:

```sh
sudo bananawiki backups verify SNAPSHOT_ID
sudo bananawiki backups download SNAPSHOT_ID --output /root/recovered.tar.gz
sudo bananawiki backups restore SNAPSHOT_ID [--domain NAME] [--port N] [--name SERVICE]
```

On a new server, run `sudo ./banana --root /opt/bananawiki backups configure …`
from a checkout with the same repository, series name and key, then
`backups list` and `backups restore SNAPSHOT_ID`.

Snapshots uploaded by releases that did not authenticate them (1.4, and 1.6
builds before this check) have no tag and are refused with *"This snapshot is
not authenticated"*. Such a snapshot is indistinguishable from one forged by
anyone with write access to the repository. If you trust everyone who could
push to the repository since the snapshot was made, add
`--allow-unauthenticated` to `verify`, `download` or `restore`. A snapshot
whose tag does not match is always refused. Take a new `backups run` after
updating so the series holds authenticated snapshots.

### Any installation

* `bananawiki db backup [--output FILE]` writes a consistent copy of the
  database (SQLite online backup; the wiki keeps running). Default:
  `<instance>/backups/manual-<time>.db`.
* `bananawiki export --output FILE` packs the instance directory with a
  consistent database copy (see below).
* **Admin → Site migration → Export** downloads the whole wiki as a ZIP
  (database and files, not the secret key). It asks for your password and is
  logged, because the archive contains every password hash.
* A file-level backup of the instance directory is only consistent if the
  wiki is stopped, or if you copy the database with `db backup` first.

Test a restore now and then.

## Restore and rollback

```sh
sudo bananawiki restore /root/wiki.tar.gz [--domain NAME] [--port N]
sudo bananawiki rollback [--package FILE]
```

* `restore` first saves the current state as `backups/before-restore-<time>.tar.gz`,
  then deploys the package's source and data. If the restored release fails
  its readiness checks, the previous state, repository credentials and update
  signer trust are put back. A package cannot
  change the mode (wiki or hosting). Automatic updates and backup schedules
  stay off afterwards until you enable them again.
* `rollback` restores the package recorded by the last successful update
  (`before-update-*`), i.e. the previous release **and its data as they were
  before that update**. Changes made since the update are lost; take a
  `backup` first if you may need them. `--package` restores another package.
* `sudo ./banana install --restore PACKAGE` restores into an empty root (a new
  server); add `--name`, `--domain` or `--port` to change them.
* `sudo bananawiki recover` finishes an interrupted operation (power loss
  during an update): it puts the recorded state back. Every other command
  does this first automatically. The state is put back once: if the
  restored release then does not start, the operation still ends with
  maintenance mode on, and later commands do not restore it again; fix the
  cause and run `start`. When the operation was interrupted before it
  stopped any wiki and has nothing to put back, `recover` does not need
  Docker: it brings the platform back, leaves the wikis as they are, and the
  portal starts again those not running once Docker answers (`start` and
  `restart` still need Docker).
  `recover --abandon` drops an operation that
  `recover` cannot finish (for example because its snapshot was deleted or
  its journal is unreadable) without putting anything back: release, units
  and data stay as they are and maintenance mode stays on until `start`. The
  snapshot, if any, stays under `staging/` until the next backup, update or
  restore deletes it.

New managed packages include `config/repo.allowed_signers` when updates require
SSH signatures. Packages from earlier releases may omit it. Recover the
operator's original allowed signers file and configure
`source set --require-signatures FILE` before updating that restored
installation; verification fails closed while the trust file is missing.

Without the managed controller: stop the wiki, put the instance directory
(or `db backup` copy plus the files) back, start it. A database from a newer
release is refused: *"This database has schema version N, newer than this
release supports"*; run the newer release or restore the copy taken before
the upgrade.

## Moving a wiki without `banana`

```sh
bananawiki export --output /backups/wiki.tar.gz     # on the old server
bananawiki import /backups/wiki.tar.gz              # on the new one
```

`export` packs every file of the instance directory except `backups/`, plus
a consistent copy of the database, into a tarball. It must be written
outside the instance directory. `import` only works into an **empty**
instance directory (set `BW_INSTANCE_DIR` and `BW_DATABASE_PATH` first); it
unpacks the archive (refusing unsafe paths) and upgrades the database. The
secret key travels with it, so sessions and tokens stay valid.

## Background jobs

Features register periodic jobs. A scheduler thread in each web worker runs
the jobs that are due; a lease in the `job_runs` table makes sure a job runs
in one worker at a time. Jobs of a switched-off feature do not run.

| Job | Every | What it does |
|---|---|---|
| `admin.account_cleanup` | 15 min | Lifts suspensions that ran out; deletes denied sign-ups and sign-ups never reviewed after the configured timeouts. |
| `admin.auto_logout` | 5 min | Scheduled daily sign-out of everyone (when enabled). |
| `announcements.prune` | 1 day | Deletes announcements that expired a while ago. |
| `api_service.prune` | 1 hour | Deletes API audit entries older than a year. |
| `attachments.cleanup` | 1 day | Removes attachment files no row points at, and 1.4 database copies of them. |
| `attention.notify` | 1 min | Emails administrators and reviewers about waiting requests (immediately, as a digest or daily, when switched on in **Admin → Notifications**) and people about decisions on their requests; reacts to new requests within a minute and otherwise polls the counts every 5 minutes; forgets notices after 90 days. |
| `audit.prune` | 1 day | Applies the audit log retention. |
| `auth.prune_rate_limits` | 1 hour | Removes old rate-limit records. |
| `badges.evaluate` | 15 min | Awards automatic badges. |
| `canvas.prune` | 1 hour | Trims canvas operation logs. |
| `chat.retention` | 1 hour | Applies the chat retention policy when due. |
| `chat.housekeeping` | 1 day | Removes orphaned chat files and 1.4 leftovers of deleted messages. |
| `contributions.expire` | 1 hour | Expires old proposed edits and drops long-resolved ones. |
| `deletion_slowdown.purge` | 10 min | Deletes pages whose 48-hour grace period ended. |
| `drafts.expire` | 1 hour | Deletes expired drafts. |
| `federation.poll` | 30 s | Synchronises paired wikis (only with `BW_FEDERATION_ENABLED`). |
| `kanban.prune_events` | 1 hour | Trims board sync events. |
| `kanban.sweep_files` | 1 day | Removes orphaned kanban files. |
| `leaderboard.refresh` | 5 min | Computes revision sizes for the leaderboard. |
| `page_governance.cleanup` | 1 hour | Ends expired reservations and cooldowns. |
| `pages.cleanup_uploads` | 6 hours | Deletes images no page, draft or other text mentions (older than 24 hours). |
| `pages.prune_editing_sessions` | 15 min | Forgets stale "who is editing" markers. |
| `site_admin.cleanup_exports` | 1 hour | Removes leftover export files. |
| `temporary_accounts.expire` | 5 min | Applies scheduled page/account deletions, visibility changes and role reverts. |
| `tts.sweep` | 6 hours | Removes unused audio files and refreshes outdated audio. |
| `users.collect_orphan_images` | 1 day | Removes unused avatars and backgrounds. |

If notification emails stop arriving, look for `bananawiki.mail` and
`bananawiki.attention.mail` warnings in the log (a refused delivery is retried
after 15 minutes, never in a loop) and use **Admin → Notifications → Send a
test email**. The throttling state is in `attention_recipients` (one row per
person: what they were last told) and `attention_events` (new requests, kept
30 days); deleting the rows of one person makes the next run treat them as new.

`bananawiki jobs list` prints them; `bananawiki jobs run [NAME]` runs every
job (or one) now. With `BW_BACKGROUND_JOBS=0` the web workers run no jobs;
then call `bananawiki jobs run` from cron (for example every five minutes).

## Maintenance mode

**Admin → Site settings → Maintenance mode** shows a maintenance page (with
your message) to everyone except administrators, and the REST API answers
503. Administrators sign in at `/admin` while it is on.

The managed updater uses a different mechanism: while the file named by
`BW_MAINTENANCE_FILE` exists, every request except the health check gets a
plain 503 page.

## Restarting

* Managed: `sudo bananawiki restart` (also brings units and `app.env`
  defaults up to date), `stop`, `start`.
* **Admin → Server → Restart** (at most once a minute) sends `SIGHUP` to the
  Gunicorn master, which replaces its workers without dropping requests. It is
  only offered under Gunicorn. The plugin manager uses the same mechanism.

## Uninstalling

```sh
sudo bananawiki uninstall                                   # keeps all files
sudo bananawiki uninstall --purge --confirm bananawiki      # deletes /opt/bananawiki
```

The first form removes services, timers and the command but keeps `config/`,
`data/` and `backups/` (reinstall with `install --reuse-data`).

## The `banana` command

Run as root. `--root DIR` (default `/opt/bananawiki`) selects the
installation. Results are printed as JSON. Exit status: 0 success, 1 error,
2 when an update was paused, failed or rolled back.

| Command | What it does |
|---|---|
| `install --mode wiki\|hosting [--domain D] [--portal-domain D] [--port N] [--name S] [source options]` | Install from the current checkout. |
| `install --restore PACKAGE [--name S] [--domain D] [--port N]` | Install from a package into an empty root. |
| `install --mode M --reuse-data` | Reinstall over kept configuration and data. |
| `update [--allow-divergent] [--retry-failed]` | Update with snapshot, readiness checks and automatic rollback. |
| `updates status\|enable\|disable [--interval MIN] [--keep-backups N] [source options]` | Automatic updates. |
| `source show\|set\|check [source options]` | Inspect or change the update source. |
| `backup [--output FILE]` (alias `migrate`) | Write a portable package. |
| `restore PACKAGE [--domain D] [--port N]` | Restore a package (after saving the current state). |
| `rollback [--package FILE]` | Restore the package taken before the last update. |
| `status` | Installation status. |
| `start`, `stop`, `restart`, `recover [--abandon]` | Service control; `restart` also rewrites units; `recover` finishes an interrupted operation (`--abandon` drops one it cannot finish). |
| `proxy [--install [--replace]] [--email ADDRESS]` | Print or install the Caddy configuration. |
| `backups keygen\|configure\|status\|list\|run\|enable\|disable\|verify\|download\|restore` | Encrypted Git backups (see above); `verify`, `download` and `restore` take `--allow-unauthenticated` for snapshots made before authentication. |
| `uninstall [--purge --confirm NAME]` | Remove the services. |
| `agent serve\|status [--socket PATH]` | Hosting runtime agent (`serve` is run by its unit; `status` pings it). |
| `create-admin`, `reset-password`, `db`, `jobs`, `config`, `setup-token` | Passed to the release's `bananawiki` command as the service user (wiki installations). |

Source options: `--repo URL`, `--branch NAME`, `--fallback-branch NAME`,
`--clear-fallback`, `--token-file FILE` or `--ssh-key FILE --known-hosts FILE`
or `--clear-credentials`, `--username NAME`, `--require-signatures FILE` or
`--clear-signatures`.

Note: on a managed server `migrate` means `backup` (as in 1.4). To upgrade
the database schema by hand use `db migrate`.

## The `bananawiki` command

Reads the `BW_*` environment like the server. Passwords are never taken from
the command line: they are prompted for (twice), read from a file
(`--password-file`), or read from standard input when it is not a terminal.
Results are printed as JSON; errors exit with status 1.

| Command | What it does |
|---|---|
| `serve [--host H] [--port P] [--debug]` | Development server (default `127.0.0.1:5001`, `BW_ENV=development`). `--debug` only on loopback addresses. |
| `setup-token` | Print the first-run setup token (refused once setup is done). |
| `migrate`, `db migrate` | Create or upgrade the database (with the automatic pre-upgrade copy). |
| `db check` | Integrity check and schema version. |
| `db backup [--output FILE]` | Online copy of the database. |
| `db prune-retired [--yes]` | Drop the tables of plugins removed in 1.6 (`banana_ai__*`, `meetings__*`, `bw_oauth_provider__*`, `oauth_login__*`, `git_override__*`, `feedback__*`, `beta_testers`, `beta_tester_invites`, `feedback_reports`, `feedback_banned_users`, `api_tokens`, `userbot_api_tokens`) after a backup to `backups/pre-prune-<time>.db`. Asks for confirmation unless `--yes`. |
| `create-admin NAME [--owner] [--password-file F]` | Create an administrator (or owner). Marks setup as done. |
| `reset-password NAME [--password-file F \| --generate] [--temporary]` | Set a new password, end the account's sessions and revoke its API tokens. `--generate` prints a random password and requires a change at next sign-in; `--temporary` requires the change for a password you chose. |
| `jobs list`, `jobs run [NAME]` | Background jobs. |
| `config check` | Validate the environment and print warnings. |
| `export --output FILE` | Pack the instance directory with a consistent database copy. |
| `import PACKAGE` | Unpack an export into an empty instance directory and upgrade it. |

Any other command, and any command given `--root`, is handed to `banana`
(root only).
