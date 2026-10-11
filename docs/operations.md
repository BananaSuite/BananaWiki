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
passes the application commands (`create-admin`, `reset-password`,
`reset-two-step`, `db`, `jobs`, `config`, `setup-token`) to the running release, as the service user,
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
  packages are. `package_files` is how many files a package would hold now
  and `package_file_limit` the most it may hold; `warnings` appears when
  backups and updates are near that limit or would fail. `failed_revision`
  is the commit automatic updates skip, with the stage that failed
  (`preparation`, `preflight` or `rollback`) and why; `failed_convergence`
  says that installing this release's units failed and was not repeated (see
  below). On hosting servers `tenant_storage` says whether the wikis get hard
  per-wiki storage quotas, as the runtime agent sees it (`setting` is
  `BW_HOSTING_TENANT_QUOTAS`; `hard_quotas`, false while the agent runs under
  units an older controller wrote; `refused` when wikis cannot start at all,
  which only `required` does; `lost` when hard quotas were enforced on this
  platform before and are no longer available; `reason`). A loss (or a
  refusal) is in `warnings`; wikis that never had hard quotas here, or with
  `off`, get a calmer entry in `notes`, with how to get them (see
  [hard storage quotas](#hard-storage-quotas-hosting)).
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

`Socket error processing request` followed by `TimeoutError: timed out` in
Gunicorn's output is a client that stopped reading a response, or read it
too slowly, for `BW_WRITE_TIMEOUT` seconds and was disconnected (see
[deployment](deployment.md#reverse-proxy)); occasional ones are harmless.

## Updates

### Managed servers

```sh
sudo bananawiki update
```

What happens:

1. The configured Git source is fetched with a hardened Git (no hooks,
   credential helpers or redirects): by default its highest **release tag**
   (`vX.Y.Z`; pre-release and other tags are ignored), never what was pushed
   after it. A source configured before 1.7 keeps following its branch until
   `source set --track tags` (see [the update source](#the-update-source)). If
   nothing changed, or the installed revision already contains the release,
   the command only brings the systemd units up to date and reports `current`.
2. The new commit must be a **fast-forward** of the installed one. Otherwise
   the update pauses; `--allow-divergent` accepts a reviewed switch to other
   history. If signatures are required, a release must be an annotated tag
   SSH-signed by an allowed key for its own name (see
   [signed releases](#signed-releases)); a server that follows a branch needs
   the commit itself SSH-signed by an allowed key.
3. The release is prepared next to the running one (new virtual environment,
   wheels only; on hosting servers the tenant image). The wiki keeps serving
   meanwhile. A failure here leaves everything as it was and records the
   commit as failed. On hosting servers Docker must first answer and be able
   to build with BuildKit (`docker buildx version`; the Dockerfiles need it
   since 1.7): otherwise nothing is changed and the commit is not recorded as
   failed. With Docker from Debian or Ubuntu (`docker.io`) install
   `docker-buildx`; with Docker CE (`docker-ce`), `docker-buildx-plugin`. A
   build that fails prints the last lines of Docker's output (the whole of it
   is in `config/last-command.log`). A build that succeeds must leave the
   image in Docker: a buildx builder that keeps its results to itself (one
   with the `docker-container` driver, chosen with `docker buildx use`) fails
   the preparation, before anything stops; `docker buildx use default`
   selects Docker's own. The tenant image is built on a freshly pulled base image
   and runs the Debian upgrade and the package installation of its final
   stage again (no cached layers), so each update also brings the wikis the
   distribution and Python security fixes published so far. The FFmpeg and
   ACL build stages before it are rebuilt only when the pull brought a new
   base image, which then also needs `ffmpeg.org` and the Debian source
   archive. The ACL stage installs two exact versions from Debian unstable
   (`libacl1` 2.4.0-1 and `tar` 1.35+dfsg-6), so its rebuild fails once
   unstable has replaced them. If the registry cannot be reached (or
   the pull takes more than 10 minutes), the image is built on the base
   image already on the server and the result carries `image_warnings`. If
   the build fails on a newly pulled base image, the previous base image is
   tagged again and the build is repeated once on it (the FFmpeg and ACL
   stages then normally come from Docker's build cache; the Debian upgrade
   and the package installation still run again); when that succeeds, the
   result carries `image_warnings` with the first error. Only a build that
   fails on the previous base image too records the commit as failed. The
   Debian upgrade and the package installation themselves have no fallback: if the
   Debian mirror or PyPI cannot be reached, preparation fails and the commit
   is recorded as failed, so automatic updates skip it until
   `update --retry-failed`. The base image a build no longer needs is
   removed afterwards: the one a pull replaced, or after a fallback the newly
   pulled one, which the next update pulls and tries again. Every release's
   image has its own package layers, so plan Docker's disk for the full size
   of each kept tenant image (the current one, the previous one and those of
   the releases kept for rollback).
4. The new release checks this server itself, before anything stops (from
   1.7 on; earlier releases have no such check): the update runs the new
   release's own `banana preflight` as root, with the system Python and a
   3-minute limit. It knows what that release needs, which the installed
   controller cannot: on hosting servers, that the running wikis can start
   again on this storage under its quota policy (see
   [hard storage quotas](#hard-storage-quotas-hosting)), that its settings
   are valid (`HOSTING_ALLOW_TENANT_PLUGINS=1` needs subdomain mode), and
   that the kernel has the IPv6 sysctls its wiki containers set (not booted
   with `ipv6.disable=1`). When it refuses (or fails) nothing is stopped, the
   update fails with its reason, and the commit is recorded as failed with
   that reason, so automatic updates do not try it again every interval.
   When the host held it up (Docker did not answer, no answer within the
   limit) nothing is stopped and nothing is recorded: the next run asks
   again. Its warnings are printed and listed as `preflight_warnings`, its
   notes (wiki storage without hard quotas, a supported configuration) as
   `preflight_notes`. Once the new release runs, `storage_warning` or
   `storage_note` in the result say what its runtime agent decided about hard
   quotas (printed only when the preflight did not just say the same).
   You can run it yourself from a checkout of the release:
   `sudo ./banana --root /opt/bananawiki preflight`.
5. A snapshot of `data/`, `site/` and the configuration is taken while the
   wiki still runs (consistent SQLite copies, hard links for uploads; on
   hosting servers the files of hosted wikis are always copied, by
   descriptor and without following links, because their containers keep
   writing them).
6. The services stop, the snapshot is refreshed (only what changed), the
   `current` link switches to the new release, units are rewritten, and the
   services start. The database is upgraded by the new release when it
   starts (by the Gunicorn master, before its workers start).
7. If the new release does not pass its readiness checks (`/health`; on
   hosting servers also the published wiki routes and every wiki that
   answered its own health check when the update began and is still marked
   running), the snapshot is put back, the old release is switched back in
   and started. The result is `rolled_back`, and the commit is recorded as
   failed with what did not become ready. On hosting servers with
   third-party plugins allowed, wikis whose own plugins may be why they do
   not serve first have their plugins quarantined, and a few wikis that
   still do not come back are **isolated** instead (see below): one tenant's
   own data never keeps every wiki on the previous release.
8. After success, a portable package `backups/before-update-<time>.tar.gz` is
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
again.

Plugins that fail only while a wiki runs no longer hold an update back. With
`HOSTING_ALLOW_TENANT_PLUGINS=1` a wiki's administrators can install Python
plugins, and one can answer while the update quiesces and fail once the new
release starts it (or only for its first minutes): rolling back would then
record the commit as failed and keep every later release, security fixes
included, off the server. So when the platform itself is ready and every
wiki that served before and does not serve again has something in its plugin
folder (`external_plugins/` in its data directory) and no plugin quarantine
in force yet, the update quarantines their plugins exactly as the portal's
**Quarantine plugins** button does (`hosting-admin instance
quarantine-plugins`, as the service account: the quarantine marker, a copy
of the wiki's database kept with its plugin snapshots, its external plugins
switched off, the wiki started again without them, and a `plugins.quarantined`
event in the portal) and waits for those wikis once more. If they now
serve, the update stands: it is not recorded as failed, the result and
`status` (`last_operation`, `last_update`) list them as
`quarantined_tenants`, and `config/history.jsonl` has an `update` event
`plugins_quarantined` before the `complete` one. A portal administrator lifts the quarantine on the wiki's
administration page once its plugins are fixed; the plugins it switched off
stay off until the wiki's administrators switch them on again. If one of
them still does not serve, or one of the failing wikis has no plugins or is
already quarantined, the update goes on as without plugins: a few wikis that
still fail are isolated (below; one whose plugins were quarantined keeps the
quarantine too), and otherwise, or with `BANANA_UPDATE_UNREADY_WIKIS=rollback`,
the update is rolled back as before (a regression of the release itself still
rolls back). The rollback restores `data/` from the
snapshot, which undoes the quarantine (marker, database copy and plugin
switches) unless `HOSTING_PLATFORM_STATE_DIR`, `INSTANCES_DIR` or
`HOSTING_DATABASE_PATH` were moved out of `data/`. Each quarantine is one
`hosting-admin` call of up to 15 minutes, one wiki after the other, followed
by up to another readiness timeout, while every wiki stays behind its
maintenance page; a release that breaks plugins on many wikis therefore
makes a long update. A quarantine that cannot be applied (the quarantine
upgrades the wiki's database first, so data the new release cannot migrate
or a full storage quota fails it too, as does code that damaged the
database) is that wiki's own failure: it is isolated with the wikis that
still fail, its isolation recording why, and the update rolls back only when
isolation cannot take them, or with `BANANA_UPDATE_UNREADY_WIKIS=rollback`
(then naming the wiki whose plugins could not be quarantined). The portal
sets the quarantine marker before that step, so such a wiki usually keeps
its plugin code off while its plugins are still switched on in its
database: the isolation and the update's warning say so, and lifting the
quarantine turns those plugins on again. Restores,
backups and recoveries never quarantine plugins. With third-party plugins
off (the default) nothing changes.

`start` and `restart` wait only for the wikis that served when they
began (after a `stop`, none); the other wikis running in the portal
database that do not serve once the command is complete are checked once,
without waiting, and listed as `unready_tenants` too, as after
`install --restore` on a new server. If Docker does not answer when a
backup, an update (also one that only brings the units up to date), a
restore, a `stop` or a `restart` begins, the command fails before anything
is stopped (for an update, before the new release's tenant image is built);
such an update is not recorded as a failed commit, so the next automatic run
tries it again. A Docker that stops answering later, while readiness is checked,
counts as the application not being ready: the command waits for it up to
the readiness timeout. While a wiki is running, a backup, an update, a
restore or a `restart` also checks, before anything is stopped, that the
wikis can start again on the wiki storage under the quota policy of the
release that will run them (see [hard storage quotas](#hard-storage-quotas-hosting)),
as does an update that brings the units of a release up to date (the
maintenance service, started again, restarts every wiki that is not under
quota while quotas apply): otherwise the command fails before anything is
stopped, instead of stopping wikis that would stay down until the readiness
timeout. These checks are made by the **installed** controller, so they only
protect updates run by a 1.7 controller: one from before 1.7 checks neither
BuildKit nor runs the new release's preflight, and one installed before
8 October 2026 (such as `b8bc352`) does not check the storage either, so it
stops the wikis before anything can refuse (see
[UPGRADING](../UPGRADING.md#updating-with-a-controller-from-before-17)).

**Wikis that fail under a new release (hosting).** A wiki can serve under
the old release and fail under the new one for reasons of its own: its
storage quota is full, so its database cannot be migrated, or its data trips
a migration. By default (`BANANA_UPDATE_UNREADY_WIKIS=isolate` in
`config/app.env`) the update keeps the new release when the platform itself
is ready and only a few of the wikis that served before do not (at most one
in ten, at least one, and never all of them): those are **stopped** and
listed as `isolated_tenants` in the result, in `status` (`last_operation`,
`last_update`) and on standard error, and the command exits with status 2,
so a timer-run update shows as a failed unit. Their users see the platform's
"unavailable" page. Why each one failed is in
`config/last-readiness-failure.json` and its container log; fix the cause,
then start the wiki again from the portal (an administrator's start or
restart), which lifts the isolation. Until then the portal never starts it
by itself, not after a restart of its maintenance service or of the host,
nor when a changed policy (an account made administrator, an approved
feature request, a platform setting) restarts the running wikis
(`data/platform_state/isolated-wikis.json` lists the isolated wikis),
and later backups and updates report it among `unready_tenants`. A wiki is
reported isolated only once it is stopped: one that the portal was still
starting is stopped again, and one that cannot be stopped fails the update,
which is then rolled back. Its data stays as the new
release left it; `rollback` puts the whole platform,
data included, back on the previous release. When more wikis fail, the
release is the likely cause and the update is rolled back as before.
`BANANA_UPDATE_UNREADY_WIKIS=rollback` rolls back whenever any wiki that
served before does not serve again (the 1.6 behaviour); another value
refuses the update before anything stops. A restore is always rolled back.

Options: `--allow-divergent`; `--retry-failed` lets an automatic run retry a
commit that failed before. A commit that failed (in preparation, refused by
its preflight, or rolled back) is recorded with the stage and the reason in
`config/failed-revision.json` and shown by `status` (`failed_revision`):
automatic updates skip it, so a release that cannot run here never takes the
wikis down every interval, until a newer commit appears; a manual `update`
always tries it again (fix the cause first). A successful update clears the
record, and a record of a commit the installed release already contains (left
by an updater from before 1.7, which never cleared it) is ignored and
removed.

**Units that do not start.** An `update` with nothing new to deploy still
installs this release's systemd units when the installed ones are outdated
(a release an older updater installed), which stops and restarts the
services and waits for readiness. If they do not become ready, the previous
units are put back and started, and the failure is recorded in
`config/failed-convergence.json` (`status`: `failed_convergence` and a
warning): automatic updates do not install the same units again (`paused`),
so the services are not stopped every interval. Look at the services'
journal, fix the cause, then run `update` or `restart`; a new revision is
installed as usual.

**Automatic updates** are off by default:

```sh
sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS --interval 60 --keep-backups 3
sudo bananawiki updates status
sudo bananawiki updates disable
```

The timer (`bananawiki-update.timer`) runs `update --automatic`, which does
nothing while the wiki was stopped on purpose, never crosses divergent
history and never retries a failed commit. `--interval` is in minutes (5 to
10080), `--keep-backups` 2 to 30. An automatic update installs new code as
root with nobody reviewing it, so `updates enable` requires signature checks
(`--require-signatures`, now or configured before): only releases signed by
a key you trust are deployed (see [signed releases](#signed-releases)).
`--allow-unsigned` turns them on without signatures anyway; then anyone who
can push to the source (a stolen maintainer account or token) can run code
on your server, and `status` warns about it. Clearing the signature
requirement while automatic updates are on needs `--allow-unsigned` too.
Automatic updates enabled before 1.7 keep running as they were; enable them
again with `--require-signatures` to add the checks.

**The update source:**

```sh
sudo bananawiki source show
sudo bananawiki source set --repo https://github.com/BananaSuite/BananaWiki.git --track tags
sudo bananawiki source check          # what update would deploy, without deploying
```

`--track tags` (the default for installations made by 1.7 and later)
deploys the highest release tag `vX.Y.Z`; until the source has one, updates
report that there is nothing to update to. `--track branch --branch NAME`
deploys the latest commit of a branch instead, work in progress included
(sources configured before 1.7 do this until you change it). Private repositories and forks: `--token-file FILE` (HTTPS token, read from a
file and stored privately; `--username`, default `git`) or `--ssh-key FILE
--known-hosts FILE` (a deploy key and verified host keys). `--clear-credentials`
removes them. `--fallback-branch NAME` is used only when the selected branch
disappears (`--clear-fallback` removes it). `--require-signatures
ALLOWED_SIGNERS` deploys only releases (following a branch: commits)
SSH-signed by a listed key (`--clear-signatures` stops that). The same options work on `install` and
`updates enable`.

### Hard storage quotas (hosting)

XFS is not required: no installation or update needs it. Every hosted wiki
is held to its plan by the application, and from 1.7 the runtime agent also
gives every hosted wiki hard byte and inode limits, enforced by the kernel
with XFS project quotas, wherever the wiki storage supports them. Whether it
does is `BW_HOSTING_TENANT_QUOTAS` in `config/app.env`:

| Setting | Storage enforcing XFS project quotas | Other storage (ext4, ...) |
|---|---|---|
| `auto` (the default, also for new platforms) | hard quotas | the application's limits, with a notice; a loud warning when hard quotas were enforced here before |
| `required` (only when you choose it) | hard quotas | wikis are refused |
| `off` | no hard quotas | no hard quotas |

Without hard quotas each wiki is held to its plan as in 1.6: its own upload
check (with the 10 GiB `HOSTING_AGENT_MAX_STORAGE_BYTES` for wikis without a
plan, apex and administrators' wikis) and the portal's storage check every 5
minutes, which suspends a wiki above 110% of its plan; a new wiki is refused
while the volume has less free space than `HOSTING_AGENT_STORAGE_RESERVE_BYTES`
plus its plan. A wiki can still go over its plan, and fill the disk, between
two checks. The agent logs a notice at every start, `status` lists it under
`notes` and the portal's administration page shows an information banner.

When hard quotas were enforced on this platform before (the agent's registry
`/var/lib/<service>-quotas/allocations.sqlite3` exists) and the storage no
longer enforces them (remounted without `prjquota`, or replaced), `auto`
still starts the wikis, without hard quotas, and never quietly: "hard
quotas were enforced on this platform before and are no longer available"
with the reason, as a warning in the agent log (at its start, and when it
happens while the agent runs), in `status` `warnings` and as a warning
banner on the administration page. Restoring the `prjquota` mount (or, when
`status` says the services run under units an older controller wrote,
`sudo bananawiki restart`) puts the wikis under quota again within 5 minutes. To keep wikis from ever running
without hard quotas, choose `required` once they are enforced: wikis then
start only under verified quotas, and `update`, `restart` and the other
operations refuse, before they stop anything, when the running wikis could
not start again. The registry is this server's and is not in backup
packages: on another server a restored platform keeps the setting of its
package and reports storage without quotas as a notice, so choose
`required` there if hard quotas must apply. `off` on XFS leaves the limits
already set on each wiki in place (later plan changes no longer reach them);
`config set` says how to clear them with `xfs_quota`. With
`HOSTING_ALLOW_TENANT_PLUGINS=1` and no hard quotas, plugins can fill the
disk at disk speed: `status` warns about it, so keep tenant plugins off
without hard quotas.

```sh
sudo bananawiki config get tenant-quotas
sudo bananawiki config set tenant-quotas auto   # or required, off
```

`config set` checks the storage first and refuses `required` when it would
keep every wiki from starting. When quotas start to apply, the maintenance service
restarts each running wiki within 5 minutes to put it under quota (a stopped
wiki's files are all examined, once). To move the wiki storage to XFS, see
[UPGRADING](../UPGRADING.md#wiki-storage-and-hard-quotas).

### Signed releases

A release is the commit a `vX.Y.Z` tag names. The maintainers create the
tags (`git tag -s vX.Y.Z -m "BananaWiki X.Y.Z"` with an SSH signing key,
`git config gpg.format ssh`) after bumping `bananawiki.__version__` and
regenerating the dependency locks (`python scripts/lock_requirements.py`),
and publish their allowed-signers lines (`email ssh-ed25519 AAAA…`). Put
those lines in a file and pass it to `--require-signatures`; Git checks
signatures with `git verify-tag` / `git verify-commit` against that file
only. Verify a fingerprint through a second channel before trusting it.

With signatures required, a server that follows release tags deploys only
an annotated tag signed for its own name: a signed commit under a
lightweight or unsigned tag, or a signed tag object pushed under another
name, releases nothing, because anyone who can push tags could otherwise
release any signed work in progress under a higher version. It deploys the
highest release that verifies; higher tags that do not are skipped, listed
in the update result (`skipped_releases`) and in a warning, so a stray tag
never holds back a security release. Only the 20 highest tags are tried
(each is fetched to be checked): if none of them verifies, the update stops
and asks you to remove the tags that are not releases. Anyone who can push
tags can therefore still hold updates back, as they could by deleting the
release tags, so let only maintainers create `v*` tags on the update source
(protected tags on the forge). A server that follows a branch needs
each commit signed instead. A restore accepts the same signatures: the
commit's own, or a signed release tag of the update source naming it.

### Other installations

* Source checkout: `git fetch --tags && git checkout vX.Y.Z`, then
  `pip install --require-hashes --only-binary=:all: -r requirements.lock`
  (the exact, hash-pinned wheels the release was tested with;
  `requirements.txt` keeps ranges for development), restart the service.
* Docker Compose: `git pull && docker compose build --pull --no-cache && docker compose up -d`
  (without `--pull --no-cache` Docker reuses the old base image and package
  layers, and the image gets no security updates).
* Desktop: replace the application; the data folder stays.

Before a release that changes the schema starts, it writes a copy of the
database to `<instance>/backups/pre-upgrade-v<old>-<time>.db` (under a
temporary name first, so a file with that name is always complete). Under
Gunicorn the master process copies and upgrades the database before it
starts any worker, so the worker timeout (`BW_WORKER_TIMEOUT`) does not
limit an upgrade; other processes that open the database meanwhile (the
read-aloud worker, `bananawiki` commands) wait for it, logging every
minute. An upgrade that failed or was interrupted is tried again at the
next start without a new copy as long as the database has not changed
since the last one (a copy that an earlier release left half-written, with
a `-journal` beside it, is never kept as that copy). The master imports
BananaWiki's modules to do this, so the plugin manager's restart button
(SIGHUP) loads plugins again but not an updated BananaWiki: restart the
service for that. Upgrading from 1.4 has its own guide:
[UPGRADING](../UPGRADING.md).

A hosted wiki (a hosting platform's tenant) saves no such copy: its folder
is under the hard storage quota of its plan, where the copy would take its
owner's room for good, and the platform's update keeps a package of every
wiki from before the update. A standalone wiki that cannot save its copy (a
full disk) does not start and says where it needs free space; nothing is
upgraded.

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
verified (checksums, and the integrity of the wiki's or the portal's
database) before the command reports it; a package that fails is deleted and
the command fails. Only those databases are checked as databases: a file a
user uploads under a name such as `notes.db`, or a hosted wiki's own
database (which its wiki can damage), is packaged and checked as an ordinary
file, so it can never make every backup fail.

A package holds at most 1,000,000 files by default (`data/`, `site/`, the
configuration and the source archive). `BANANA_PACKAGE_MAX_FILES=N` in
`config/app.env` changes that (1000 to 100000000); it applies to backups,
updates and restores on this server, and a server restoring the package
needs a limit at least as high (`install --restore` on a new server uses
the default). A backup or update of an installation with more files, with
an invalid value, or without the free space for its snapshot is refused
before anything is stopped (an update is then not recorded as a failed
commit), and `status` warns from 80% of the limit (`package_files`,
`package_file_limit`, `warnings`). While a backup or update runs, the
controller needs about 1.5 GB of memory per million files. 1.4 and earlier
1.6 releases restore packages of at most 100,000 files.

Earlier 1.6 controllers fail every backup and update of an installation
with more than about 76,000 files, after the services stopped (they start
again). Such a server cannot update to this release with its installed
controller: run that one update with this release's controller, from a
checkout of the update source, for example
`git clone --branch BRANCH SOURCE_URL /root/bananawiki-update` then
`sudo /root/bananawiki-update/banana --root /opt/bananawiki update`. Later
updates run the installed controller as usual.

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
  --keep 7 --max-mib 512  # add --append-only for an append-only series
sudo bananawiki backups run
sudo bananawiki backups list
sudo bananawiki backups status
```

* **Save the recovery key offline** before relying on these backups: without
  it the snapshots cannot be decrypted, even with repository access.
* Use a separate token with read/write access to that repository only, in a
  file with mode 0600. For Forgejo use `--forge forgejo` and its HTTPS URL.
* By default each upload deletes the snapshots beyond `--keep`, so the token
  can delete branches, and so can anyone who takes over the server.
  `configure --append-only` makes uploads only create branches; then protect
  `banana-backups/**` against deletion and force pushes on the forge (a GitHub
  repository ruleset, or a Forgejo protected-branch rule) and apply retention
  now and then with `backups prune --token-file FILE`, where FILE holds a
  token allowed to delete branches that is used for that command only.
* Retention (`prune`, and the pruning after each upload) counts only the
  snapshots this series' recovery key authenticated, newest first, and
  deletes only what is older than the `--keep`-th of them, so it never
  deletes while fewer than `--keep` authenticated snapshots would remain.
  Snapshot IDs are branch names, chosen by whoever can push: unauthenticated
  snapshots newer than that are kept and not counted, and IDs dated in the
  future (beyond a day of clock skew) or that are no dates are neither
  counted nor deleted, so branches pushed with a stolen upload token cannot
  make retention delete genuine snapshots, nor break later uploads. Both are
  listed in the result's `warnings`: check who pushed them, and delete them
  on the forge. Retention reads only each snapshot's index where the forge
  allows partial fetches (GitHub does); elsewhere it downloads each
  snapshot it checks.
* Every transfer first checks through the forge's API that the repository is
  private (not public, archived or a mirror).
* `run` creates and verifies a package, encrypts it, uploads it in parts of
  at most 32 MiB, downloads and verifies it again, and only then prunes old
  snapshots of the series (`--keep`, 2–30). `--max-mib` (1–1024, default 512)
  caps the package.
* Snapshots are **authenticated**. age encrypts to the recovery key's public
  recipient, which is stored in the repository, so anyone who can push to it
  could otherwise upload a snapshot that decrypts and verifies, and a restore
  deploys a package's data and `app.env`. Each snapshot's
  `index.json` therefore carries an HMAC-SHA256 (keyed from the recovery
  key, which is never uploaded) over the snapshot's series, ID and the
  checksum of every encrypted part. `verify`, `download` and `restore` check
  it before decrypting anything and refuse a snapshot without a valid tag.
  The recovery key is the only secret this needs: nothing else has to be
  saved for a restore on a new server.
* The server keeps a working copy of the recovery key (it authenticates each
  upload and verifies it by downloading it again), so whoever takes over the
  server can read every snapshot still in the series, including pages and
  files deleted since. Keep `--keep` as small as your recovery needs allow,
  and after a suspected compromise start a new series with a new key, keeping
  the old key offline.
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
`backups list` and `backups restore SNAPSHOT_ID`. A new server cannot check
the snapshot's source against an update source: the restore shows its
revision and stops, and `backups restore SNAPSHOT_ID --trust-source REVISION`
deploys it (see [Restore and rollback](#restore-and-rollback)).
A snapshot of a 1.7 or later release that lacks its dependency locks (one
deployed with `update --allow-unpinned`) is refused before anything changes;
`backups restore SNAPSHOT_ID --allow-unpinned`, like `restore` and `rollback`,
installs its dependencies from the `requirements.txt` ranges instead.

Snapshots uploaded by releases that did not authenticate them (1.4, and 1.6
builds before this check) have no tag and are refused with *"This snapshot is
not authenticated"*. Such a snapshot is indistinguishable from one forged by
anyone with write access to the repository. If you trust everyone who could
push to the repository since the snapshot was made, add
`--allow-unauthenticated` to `verify`, `download` or `restore`; the command
then prints a warning when it uses such a snapshot. Even then a snapshot is
refused that cannot be from before authentication: one dated at or after the
series' first authenticated snapshot (every authenticated snapshot records
it, so it holds after retention deleted that one), or dated in the future. A
snapshot whose tag does not match is always refused. Take a new `backups run` after
updating so the series holds authenticated snapshots.

### Any installation

* `bananawiki db backup [--output FILE]` writes a consistent copy of the
  database (SQLite online backup; the wiki keeps running). Default:
  `<instance>/backups/manual-<time>.db`.
* `bananawiki export --output FILE` packs the instance directory with a
  consistent database copy (see below).
* **Admin → Site migration → Export** downloads the whole wiki as a ZIP
  (database and files, not the secret key). It asks for your password and is
  logged, because the archive contains every password hash. Imported into a
  wiki with another secret key, two-step verification is turned off for the
  accounts that use it (they set it up again); keep the secret key
  (`<instance>/.secret_key` or `SECRET_KEY`) when you move a wiki to keep it.
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
  its readiness checks, the previous state is put back. A package cannot
  change the mode (wiki or hosting). Automatic updates and backup schedules
  stay off afterwards until you enable them again.
* `rollback` restores the package recorded by the last successful update
  (`before-update-*`), i.e. the previous release **and its data as they were
  before that update**. Changes made since the update are lost; take a
  `backup` first if you may need them. `--package` restores another package.
* `sudo ./banana install --restore PACKAGE` restores into an empty root (a new
  server); add `--name`, `--domain` or `--port` to change them.
* A package holds the source of its release, and that code runs as the
  service and, through the installed `bananawiki` command, as root. So a
  restore (`restore`, `rollback`, `install --restore`, `backups restore`)
  reuses the release if this server already prepared that revision, and
  otherwise builds it from this server's update source when the source has
  it (on a branch or under a release tag), checking its signature when
  signatures are required: the commit's own, or a signed release tag naming
  it, as updates do. The copy in the package is then not used. When neither
  has the revision (a new server, or a package from elsewhere) the restore
  stops before changing anything and shows the revision, the source and
  branch recorded in the package, and when it was made. If you made the
  package or trust whoever did, run the command again with
  `--trust-source REVISION`. A server whose update source requires signed
  revisions never deploys an unverified revision from a package: the
  refusal explains how to turn the checks off for that restore
  (`source set --clear-signatures`, the restore with `--trust-source`, then
  `source set --require-signatures FILE` again). A restore keeps this
  server's update source settings, credentials and allowed signers, which
  decided what code may run (the result's `source_warning` says when the
  package's differ); only a server without any, a new one, takes the
  package's. A restore otherwise restores the package's data and
  configuration: only restore packages you or your team made.
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

A restore onto a new server takes its update source from the package. New
managed packages include `config/repo.allowed_signers` when updates require
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
| `admin.auto_logout` | 5 min | Scheduled daily sign-out of users and editors (when enabled; administrators, owners and superusers stay signed in). |
| `announcements.prune` | 1 day | Deletes announcements that expired a while ago. |
| `api_service.prune` | 1 hour | Deletes API audit entries older than a year. |
| `attachments.cleanup` | 1 day | Removes attachment files no row points at, and 1.4 database copies of them. |
| `attention.notify` | 1 min | Emails administrators and reviewers about waiting requests (immediately, as a digest or daily, when switched on in **Admin → Notifications**) and people about decisions on their requests; reacts to new requests within a minute and otherwise polls the counts every 5 minutes; forgets notices after 90 days. |
| `audit.prune` | 1 day | Applies the audit log retention. |
| `auth.prune_rate_limits` | 1 hour | Removes old rate-limit records. |
| `badges.evaluate` | 15 min | Awards automatic badges. |
| `canvas.prune` | 1 hour | Trims canvas operation logs and histories (200 entries, 32 MB per canvas). |
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
2 when an update was paused, failed or rolled back, or completed but isolated
wikis that did not start under the new release.

| Command | What it does |
|---|---|
| `install --mode wiki\|hosting [--domain D] [--portal-domain D] [--port N] [--name S] [--tenant-quotas auto\|required\|off] [source options]` | Install from the current checkout (`--tenant-quotas`: hosting, see [hard storage quotas](#hard-storage-quotas-hosting); default `auto`). |
| `install --restore PACKAGE [--name S] [--domain D] [--port N] [--trust-source REVISION]` | Install from a package into an empty root. |
| `install --mode M --reuse-data` | Reinstall over kept configuration and data. |
| `update [--allow-divergent] [--retry-failed]` | Update with snapshot, readiness checks and automatic rollback. |
| `updates status\|enable\|disable [--interval MIN] [--keep-backups N] [source options]` | Automatic updates. |
| `source show\|set\|check [source options]` | Inspect or change the update source. |
| `backup [--output FILE]` (alias `migrate`) | Write a portable package. |
| `restore PACKAGE [--domain D] [--port N] [--trust-source REVISION]` | Restore a package (after saving the current state). |
| `rollback [--package FILE] [--trust-source REVISION]` | Restore the package taken before the last update. |
| `status` | Installation status. |
| `preflight` | Check, changing nothing, that this release can run on this server (an update runs the new release's own copy before it stops anything). JSON on one line; exit 0, or 3 with the reason. |
| `config get\|set tenant-quotas [auto\|required\|off]` | Hosting: show or change `BW_HOSTING_TENANT_QUOTAS` (checked against the storage first). |
| `start`, `stop`, `restart`, `recover [--abandon]` | Service control; `restart` also rewrites units; `recover` finishes an interrupted operation (`--abandon` drops one it cannot finish). |
| `proxy [--install [--replace]] [--email ADDRESS]` | Print or install the Caddy configuration. |
| `backups keygen\|configure\|status\|list\|run\|enable\|disable\|prune\|verify\|download\|restore` | Encrypted Git backups (see above); `configure --append-only` never deletes snapshots while uploading and `prune [--token-file FILE]` applies retention; `verify`, `download` and `restore` take `--allow-unauthenticated` for snapshots made before authentication, and `restore` takes `--trust-source REVISION`. |
| `uninstall [--purge --confirm NAME]` | Remove the services. |
| `agent serve\|status [--socket PATH]` | Hosting runtime agent (`serve` is run by its unit; `status` pings it). |
| `create-admin`, `reset-password`, `reset-two-step`, `db`, `jobs`, `config`, `setup-token` | Passed to the release's `bananawiki` command as the service user (wiki installations). |

Source options: `--repo URL`, `--track tags|branch`, `--branch NAME`,
`--fallback-branch NAME`, `--clear-fallback`, `--token-file FILE` or
`--ssh-key FILE --known-hosts FILE` or `--clear-credentials`,
`--username NAME`, `--require-signatures FILE` or `--clear-signatures`,
`--allow-unsigned` (automatic updates without signature checks).

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
| `reset-two-step NAME` | Turn off two-step verification for an account that lost its authenticator app and recovery codes (owners and superusers included), end its sessions and record it in the audit log. |
| `jobs list`, `jobs run [NAME]` | Background jobs. |
| `config check` | Validate the environment and print warnings. |
| `export --output FILE` | Pack the instance directory with a consistent database copy. |
| `import PACKAGE` | Unpack an export into an empty instance directory and upgrade it. |

Any other command, and any command given `--root`, is handed to `banana`
(root only).
