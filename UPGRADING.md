# Upgrading BananaWiki

This page explains how to upgrade an existing installation:

* [Upgrading from 1.6 to 1.7](#upgrading-from-16-to-17): an in-place update
  of a 1.6 installation.
* [Upgrading from 1.4](#upgrading-from-14): the rewrite takes over a 1.4
  installation (or any earlier 1.x release at schema 3) in place. A 1.4
  installation upgrades straight to the current release: follow that section,
  then read the 1.6 to 1.7 section too, because everything it describes also
  applies.

## Upgrading from 1.6 to 1.7

1.7 upgrades a 1.6 installation in place: same database, files, secret key,
environment variables and URLs. Accounts, passwords, sessions, API tokens and
content survive. Update the usual way (`sudo bananawiki update` on a managed
server, or the steps in [Managed servers](#managed-servers-banana),
[Installations from a Git checkout](#installations-from-a-git-checkout) and
[Docker](#docker) below); the first start migrates the databases. This
section lists everything that needs an operator's attention.

### Before you update

1. **Back up**, data and secret key: `sudo bananawiki backup` on a managed
   server; otherwise stop the wiki and copy the instance directory, the
   upload folders and the secret key. The first start also writes
   `<instance>/backups/pre-upgrade-v<old>-<time>.db` before migrating (a
   hosted wiki writes none: the platform's update package covers it).
2. **Keep the secret key.** Two-step verification secrets and recovery codes
   are keyed from it: with a different key, accounts that turned two-step
   verification on can no longer pass the code step (reset them with
   `bananawiki reset-two-step NAME`).
3. **Third-party plugins.** If the wiki runs plugins you uploaded, set
   `BW_ALLOW_EXTERNAL_PLUGINS=1` to keep them for good (see
   [Content, chat, plugins and read aloud](#content-chat-plugins-and-read-aloud)).
4. **Read aloud:** upgrade the web application and the read-aloud worker
   together, because a worker of an earlier release cannot open the new
   database. If you run the contrib GPU speech server, upgrade it too.
5. **Managed servers with more than about 76,000 files in `data/`:** earlier
   1.6 controllers fail every backup and update of such an installation once
   the services have stopped (they start again), so the installed controller
   cannot update to this release. Run that one update with this release's
   controller from a checkout of the update source:
   `git clone --branch BRANCH SOURCE_URL /root/bananawiki-update`, then
   `sudo /root/bananawiki-update/banana --root /opt/bananawiki update`. Later
   updates use the installed controller as usual. Packages can now hold up to
   `BANANA_PACKAGE_MAX_FILES` files (default 1,000,000).
6. **Hosting platforms**, before updating:
   * **check which controller will run the update**: `sudo bananawiki
     preflight` answers with a line of JSON from a 1.7 controller and with
     "invalid choice: 'preflight'" from an earlier one. An earlier controller
     stops the wikis before anything can refuse the update: read
     [Updating with a controller from before 1.7](#updating-with-a-controller-from-before-17)
     first;
   * **install Docker's BuildKit component**: the tenant image now needs it.
     `docker buildx version` must answer; otherwise run
     `sudo apt install docker-buildx` (Debian or Ubuntu with their `docker.io`)
     or `sudo apt install docker-buildx-plugin` (Docker CE). Without it a 1.7
     controller refuses the update before changing anything; an earlier one
     fails while building the image ("the --mount option requires BuildKit"),
     records the commit as failed and leaves the wikis running;
   * **wiki storage**: 1.7 can give every wiki hard byte and inode limits
     with XFS project quotas, but no update or installation requires XFS. An
     existing platform gets `BW_HOSTING_TENANT_QUOTAS=auto`, the default
     everywhere: on XFS mounted with `prjquota` the limits are enforced; on
     any other storage (ext4, say) the wikis keep running without them, as
     under 1.6, held to their plans by the application, and the agent's log,
     `status` and the portal's administration page say so. Nothing has to
     change before the update; see [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)
     to enforce them. One difference from 1.6 without hard quotas: wikis
     without a plan (the apex wiki and administrators' wikis), unlimited
     under 1.6, are held to `HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB) by
     their own upload check, and one already above it accepts no more
     uploads. Check their size first
     (`sudo du -sh /opt/bananawiki/data/instances/*`) and raise
     `HOSTING_AGENT_MAX_STORAGE_BYTES` in `config/app.env` if one is near it;
   * do not update an arm64 hosting server: hosting now runs only on Linux
     x86_64 (the portal and the maintenance service refuse to start
     elsewhere, so the update is rolled back);
   * in port or onion mode (no domain, or `BASE_DOMAIN` empty, an IP address
     or `localhost`), remove `HOSTING_ALLOW_TENANT_PLUGINS=1` from
     `config/app.env` (or set it to `0`, or configure a domain for subdomain
     mode). Otherwise the portal, the maintenance service and `hosting-admin`
     refuse to start: a 1.7 controller (or `preflight` from a checkout)
     refuses the update before anything stops; an earlier controller rolls it
     back after its readiness timeout and records the commit as failed (fix
     the setting, then run `update --retry-failed`);
   * check that the kernel has IPv6: `ls /proc/sys/net/ipv6` must list
     files. Every 1.7 wiki container switches IPv6 off with sysctls that
     Docker cannot set on a host booted with `ipv6.disable=1`, so no wiki
     would start (a 1.7 controller's preflight refuses the update; an earlier
     controller rolls it back after its readiness timeout). Remove
     `ipv6.disable=1` from the kernel command line and reboot; to keep IPv6
     off on the host, set the sysctl `net.ipv6.conf.all.disable_ipv6=1`
     instead;
   * where hard quotas apply, wikis already above their plan, the 10 GiB
     ceiling or 100,000 files are not refused: on their first start under
     quota they are adopted at what they hold
     plus 64 MiB and 1,024 files, the portal records `storage.over_plan` and
     emails the owner. Raise their plans, or have owners delete files. A wiki
     with more than 200,000 files (twice `HOSTING_AGENT_MAX_INODES`) needs
     `HOSTING_AGENT_MAX_INODES` raised in `config/app.env` before the update.
     Wikis from 1.6 are adopted without checking their plans against the
     volume (1.6 never did): if the plans add up to more than it holds,
     update and backup snapshots leave each such wiki only its adoption
     headroom and print a warning with the shortfall, and those wikis can
     run out of space before reaching their limits. Lower plans (or
     `HOSTING_AGENT_MAX_INODES`) or free space: their next start admits them;
   * review the wikis currently suspended by the automatic storage protection:
     those that fit within their limit reopen at the first maintenance pass
     (see [Hosting platform](#hosting-platform)). Suspend again, with a reason
     of your own, any that must stay closed;
   * the Docker registry, the Debian mirror and PyPI must be reachable during
     updates, because every update rebuilds the tenant image on a fresh base
     (a mirror or PyPI outage records the commit as failed until
     `update --retry-failed`).

### After you update

* **Managed servers: switch to release tags and signed updates.** An
  existing server keeps following its branch. Once the version you want is
  tagged, switch with
  `sudo bananawiki source set --track tags --require-signatures ALLOWED_SIGNERS`
  (`source check` shows what an update would deploy), using the maintainers'
  published allowed-signers lines; see
  [signed releases](docs/operations.md#signed-releases).
* **Hosting: load the host firewall** with
  `sudo bananawiki firewall | sudo nft -f -`, which fills in the private DNS
  resolvers this host uses (wikis may send DNS only to those, no longer to
  port 53 anywhere on the LAN), and make it persistent from a saved copy of
  that output, not from the shipped `deploy/nftables/bananawiki-tenants.nft`:
  its resolver set is empty, so on a host with a VPC or LAN resolver wikis
  would get no DNS (see the hosting guide, "Host firewall"). Tenant
  bridges are renamed `bwt…` when each wiki first starts under 1.7: review
  hand-written rules that matched `br+`, and remove them only if nothing else
  on the host needs them.
* **Hosting: decide about hard storage quotas.** `sudo bananawiki status`
  shows `tenant_storage` as the runtime agent sees it: right after an update
  by an older controller it is false until this release's units are
  installed (`units_current: true`). When `hard_quotas` is false, decide
  whether the portal's storage checks are enough for this platform or move
  the wiki storage to XFS (see [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)).
  When it is true, `auto` keeps using them, and should the storage ever stop
  enforcing them the wikis keep running without them while the agent's log,
  `status` and the administration page warn loudly. To have the wikis refused
  instead, run `sudo bananawiki config set tenant-quotas required`.
* **Hosting: decide the recovery parallelism.** The portal now restarts
  `BW_HOSTING_RECOVERY_MAX_WORKERS` wikis at once (default 2; 1.6 always used
  4). Set `BW_HOSTING_RECOVERY_MAX_WORKERS=4` in `config/app.env` to keep the
  1.6 restart speed; the updater's readiness timeout follows.
* **Hosting: decide about expiry and renewals.** Wikis still expire after
  `INSTANCE_DURATION_DAYS` (14). For production wikis turn on **Owners may
  renew their wikis** (Admin → Settings) or set `INSTANCE_DURATION_DAYS=0`.
* **Two-step verification for administrators** is off until you turn it on.
  Once your administrators have set it up (**Settings → Two-step
  verification**), an owner or superuser who uses it can switch on **Admin →
  Settings → Security → Require two-step verification for administrators**.

### Updating with a controller from before 1.7

`sudo bananawiki update` runs the controller of the **installed** release,
and one from before 1.7 cannot know what 1.7 needs. It does not check
BuildKit, never runs 1.7's own preflight and, when installed before
8 October 2026 (`b8bc352`, for example), does not check the wiki storage
either. It stops the wikis, switches to 1.7, writes its own (older) systemd
units, starts the services and waits up to its readiness timeout
(`waves × (2 × INSTANCE_STARTUP_TIMEOUT_SECONDS + 120)` seconds: 360 s for
two wikis) before it rolls everything back.

1.7.0 is built to come up under such a controller on any storage, as long as
`BW_HOSTING_TENANT_QUOTAS` is unset, `auto` or `off` and BuildKit is
installed: the older units do not let the runtime agent use quotas (nor
inspect tenant mounts), so in `auto` it starts the wikis without hard
quotas and logs why. Then run `sudo bananawiki update` once more (or
`sudo bananawiki restart`): now the 1.7 controller installs its own units
(`status` showed `units_current: false`), and on XFS with `prjquota` the
maintenance service restarts each wiki once to put it under quota. If the
services do not become ready under those units, that update puts the
previous units back, records it (`status` shows `failed_convergence`) and
automatic updates do not try again, so it is not repeated every hour; look
at the services' journal, fix the cause, then run `update` or `restart`
again. A release installed this way also keeps the dependencies the older
controller installed, from `requirements.txt` ranges without the locks'
hash checks, until the next release is installed.

The safer way, recommended for production and needed when
`BW_HOSTING_TENANT_QUOTAS=required`:
run this one update with the 1.7 controller from a checkout. It checks
Docker, BuildKit and the storage, and runs the new release's preflight,
before it stops anything, prepares the release from its hash-checked locks
and installs its own units in the same step:

```sh
git clone --branch BRANCH_OR_TAG SOURCE_URL /root/bananawiki-update   # a 1.7 checkout of your update source
sudo /root/bananawiki-update/banana --root /opt/bananawiki preflight   # one line of JSON, "ok": true
sudo /root/bananawiki-update/banana --root /opt/bananawiki update
```

It deploys what the installed configuration selects (`source show`), not the
checkout; later updates use the installed controller as usual.

If an older controller already tried and rolled back:

* the commit is recorded in `config/failed-revision.json`: automatic runs
  skip that commit but try every newer one, each time stopping the wikis for
  up to the readiness timeout. Keep automatic updates off
  (`sudo bananawiki updates disable`) until the update has succeeded. A manual
  `sudo bananawiki update` retries a failed commit at once, so fix the cause
  first (and prefer the checkout above). An older controller never removes
  the record after a later update succeeds; a 1.7 controller ignores and
  removes it once the installed release contains that commit;
* the failed attempt left `releases/<commit>/` and the image
  `bananawiki-tenant:<commit>`, prepared without the dependency locks: a 1.7
  controller prepares that release again before it deploys it;
* when wikis are large, raise `INSTANCE_STARTUP_TIMEOUT_SECONDS` (30–600) in
  `config/app.env` first: each wiki migrates its database at its first 1.7
  start, and an older controller rolls the whole platform back when a single
  wiki is not ready in time (1.7 isolates a few unready wikis instead);
* the other items of [Before you update](#before-you-update) apply too:
  `HOSTING_ALLOW_TENANT_PLUGINS=1` in port or onion mode keeps the 1.7 portal
  from starting, a kernel booted with `ipv6.disable=1` keeps every 1.7 wiki
  from starting, and an older controller fails on more than about 76,000
  files in `data/`.

At their first 1.7 start the wikis get new tenant networks (bridges named
`bwt…`). The agent waits for Caddy to forget the old addresses only where
Caddy routes the wikis: behind another reverse proxy (Traefik sending the
wikis to the portal, for example) or in port and onion mode it replaces them
at once.

### Wiki storage and hard quotas

XFS is not required: installations and updates work on any storage, and
each wiki's size limit is enforced by the application. With hard quotas the
kernel also stops a wiki at its plan (at most
`HOSTING_AGENT_MAX_STORAGE_BYTES`, 10 GiB, and `HOSTING_AGENT_MAX_INODES`
files), so no wiki can fill the disk. They need the wiki storage
(`INSTANCES_DIR`, by default `/opt/bananawiki/data/instances`) on XFS mounted
with project quotas (`prjquota`), and are then used automatically.
`BW_HOSTING_TENANT_QUOTAS` in `config/app.env` decides what happens:

| Setting | XFS with `prjquota` | Other storage |
|---|---|---|
| `auto` (the default, new platforms too) | hard quotas | no hard quotas, with a notice; a loud warning when they were enforced here before |
| `required` (only when you choose it) | hard quotas | the wikis are refused |
| `off` | no hard quotas | no hard quotas |

Without hard quotas each wiki is held to its plan as in 1.6, by its own
upload check and by the portal, which measures every wiki every 5 minutes and
suspends one above 110% of its plan, and a new wiki is refused while the volume
has less free space than `HOSTING_AGENT_STORAGE_RESERVE_BYTES` (256 MiB) plus
its plan. Unlike 1.6, a wiki without a plan (the apex wiki, administrators'
wikis) is held to `HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB) by its upload
check, as a hard quota would hold it; one already above it accepts no more
uploads until the variable is raised. That is enough where every wiki belongs
to the same people (a school or a company running its own wikis); on a
platform open to strangers, one wiki could fill the disk between two checks
and stop every other one. Do not combine it with
`HOSTING_ALLOW_TENANT_PLUGINS=1`: plugins run their owners' code with write
access to the wiki's files, which can fill the disk at disk speed (`status`
warns about it).

```sh
findmnt -no SOURCE,FSTYPE,OPTIONS --target /opt/bananawiki/data/instances   # xfs ... prjquota
sudo bananawiki status                           # tenant_storage
sudo bananawiki config get tenant-quotas
sudo bananawiki config set tenant-quotas auto    # or required, off: checked against the storage first
```

`auto` never loses hard quotas quietly: once the agent has put a wiki under
quota (`/var/lib/<service>-quotas/allocations.sqlite3` exists), storage that
no longer enforces quotas (remounted without `prjquota`, or replaced) still
starts the wikis, without hard quotas, and the agent's log, `status`
(`tenant_storage.lost`, in `warnings`) and the portal's administration page
(a warning banner) say that hard quotas were enforced on this platform
before and are no longer available, and why. Restore the `prjquota` mount
(or, when `status` says the services run under units an older controller
wrote, run `sudo bananawiki restart`) and the maintenance service puts the
wikis under quota again within 5 minutes. To have the wikis refused rather than run without hard quotas,
choose `required` (`config set` accepts it only while the storage enforces
them): wikis then start only under verified quotas, and an update or a
restart refuses, before it stops anything, when they could not start again.
That memory belongs to the server: the registry is not in backup packages,
so a platform moved to another server with `install --restore` keeps the
setting of its package (`auto` for a platform from before 1.7) and, if the
new storage lacks quotas, runs without them there with the notice of
storage that never had them. `required` is part of the package. New
platforms are installed with `auto` on any storage: `install` says which
limits apply and how to get hard quotas, and only
`install --tenant-quotas required` refuses storage without them
(`install --restore` refuses `--tenant-quotas`: change the setting after
the restore).

`off` on XFS with `prjquota`, after wikis were under quota, leaves the
limits already set on each wiki in place: the kernel keeps enforcing them,
and later plan changes no longer reach them. `config set tenant-quotas off`
says how to clear them with `xfs_quota`.

#### Moving the wiki storage to XFS on an attached block volume

Project quotas are switched on when a filesystem is mounted, so the root
filesystem usually cannot provide them: put the whole installation
directory on a dedicated XFS filesystem (the managed restore renames `data/`,
so mount the installation directory itself, not `data/` or
`data/instances/`). A block volume attached to the server, a spare disk or
an unused partition all work the same way:

1. Note the space used: `sudo du -sh /opt/bananawiki`. Provide a device at
   least twice that size plus room to grow (update snapshots are staged on
   it): attach a block volume in your provider's console (without letting the
   provider mount it automatically), or set aside a spare disk or partition.
2. Find its device: `lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS` lists it with
   the size you chose and no mount point (below `DEVICE`, such as
   `/dev/sdb`). Unless it already holds an empty XFS filesystem, create one:
   `sudo mkfs.xfs DEVICE` (this erases everything on the device, so check the
   name twice). Note the filesystem's UUID for `/etc/fstab`:
   `sudo blkid -s UUID -o value DEVICE` (below `UUID`; device names can
   change between boots, the UUID does not).
3. Mount it with project quotas and check:
   `sudo mkdir -m 700 /mnt/bananawiki-xfs && sudo mount -o prjquota DEVICE /mnt/bananawiki-xfs`,
   then `findmnt -no FSTYPE,OPTIONS /mnt/bananawiki-xfs` shows `xfs` and
   `prjquota`.
4. Stop the platform: `sudo bananawiki updates disable`,
   `sudo bananawiki backup` (keep the package off the server too), then
   `sudo bananawiki stop`.
5. Copy everything, keeping owners, permissions, hard links, ACLs and extended
   attributes: `sudo rsync -aHAX --numeric-ids /opt/bananawiki/ /mnt/bananawiki-xfs/`.
6. Swap: `sudo umount /mnt/bananawiki-xfs`,
   `sudo mv /opt/bananawiki /opt/bananawiki.before-xfs`,
   `sudo mkdir /opt/bananawiki`, then add to `/etc/fstab`
   `UUID=UUID /opt/bananawiki xfs defaults,prjquota,nofail 0 0`, run
   `sudo systemctl daemon-reload && sudo mount /opt/bananawiki` and check with
   `findmnt -no FSTYPE,OPTIONS /opt/bananawiki` (`prjquota`; a `remount` cannot
   add it).
7. `sudo bananawiki config get tenant-quotas` shows `hard_quotas: true` once
   the new storage enforces quotas (not while the agent still runs under the
   units of an older controller: run `sudo bananawiki restart` first in that
   case). `auto`, the default, then uses them; to have the wikis refused
   rather than ever run without them, also run
   `sudo bananawiki config set tenant-quotas required`. Then
   `sudo bananawiki start`. The maintenance service puts
   every wiki under quota as it starts it (each wiki's files are examined
   once; raise `HOSTING_AGENT_MAX_INODES` first for wikis with more than
   200,000 files). `sudo bananawiki status` then shows
   `tenant_storage.hard_quotas: true`.
8. Re-enable automatic updates if you use them, and delete
   `/opt/bananawiki.before-xfs` once everything works.

To undo the move while `/opt/bananawiki.before-xfs` still exists:
`sudo bananawiki stop`, `sudo umount /opt/bananawiki`, remove the line from
`/etc/fstab`, `sudo rmdir /opt/bananawiki && sudo mv /opt/bananawiki.before-xfs /opt/bananawiki`,
`sudo systemctl daemon-reload`, then `sudo bananawiki start` (the copy you
go back to keeps the settings it had at step 4: a `required` chosen in step 7
stayed on the volume). The wikis start without hard quotas; as
they were under quota before, the agent's log, `status` and the
administration page warn that hard quotas are no longer available. Going
back was your decision, so `sudo bananawiki config set tenant-quotas off`
turns that warning into the calmer notice (set `auto` again once the storage
enforces quotas). Step 4 turned automatic updates off in the copy you
went back to: re-enable them if you use them (`sudo bananawiki updates
enable`, with the options you used before). What changed in the wikis since
step 5 stays on the XFS filesystem, not in the copy you went back to.

A custom `INSTANCES_DIR` outside the installation also needs a reviewed
`ReadWritePaths` override for the agent unit (see
[docs/hosting.md](docs/hosting.md)).

### What changes in the databases

Each migration runs once, in its own transaction; if one fails, nothing is
changed and the application does not start (the log names the problem). The
wiki copies its database to `<instance>/backups/pre-upgrade-v<old>-<time>.db`
first; the hosting database is covered by the rollback package a managed
update writes before it switches release.

* **Wiki database, schema 4 to 11** (1.6.0 wrote schema 4):
  * 5: a chat upload ledger keeps the 24-hour upload allowance consumed when a
    message or conversation is deleted. Retained uploads from the preceding
    24 hours are counted during the upgrade; uploads deleted before it cannot
    be reconstructed.
  * 6: records whether an owner or superuser imposed a suspension, indexes
    former user names, and grants `page.view_all` and `category.view_all` to
    every saved permission set, because both are now enforced (nothing
    changes until an administrator saves permissions without them).
  * 7: counts read-aloud claims lost with their worker.
  * 8: `api_service__tokens.parent_id` (which token issued a token),
    `chats.cleared_upto_user1/2` (each participant clears a direct
    conversation for themselves; existing conversations start uncleared) and
    the `chat__reports` table.
  * 9: the `auth__two_step` table, the setting
    `site_settings.two_step_required_for_admins` (default 0) and
    `users.device_epoch` / `device_epoch_stamp` (NULL until each account's
    next sign-in). Two-step verification starts off for everyone and is not
    required.
  * 10: the `chat__blocks` table (who blocked whom, deleted with either
    account, nobody blocks themselves) and `chat_messages.withheld` (direct
    messages sent to someone who had blocked their sender; existing messages
    start at 0). Nobody starts out blocked.
  * 11: `kanban_columns.is_done` (a done column, whose tickets are never
    overdue), off for every column.
* **Hosting database, version 3 to 7:**
  * 4: a new wiki stays reserved as "Creating" until its provisioning
    finishes; existing wikis are marked ready. An interrupted or failed
    creation is never started or recovered: terminate it and create it again.
  * 5: collaborators and merge records survive the deletion of the account
    that created them.
  * 6: `hosting_email_log` (email budgets shared by every worker),
    `instances.expiry_warning_for` and `expiry_warning_days`,
    `hosting_settings.owner_renewal_enabled` (default 0),
    `accounts.email_claimed_at` (filled in for existing addresses),
    `accounts.email_claims_since` and `email_claims` (the account's current
    30-day series of address changes), `accounts.mfa_epoch` (retires portal
    device cookies when two-step verification is turned on or off),
    `instances.admin_expiry` (an end date an administrator brought forward)
    and the `instance_domain_claims` table (claims of a lapsed custom domain,
    waiting for the claimant's proof).
  * 7: a stored denied-account deletion delay of 0
    (`hosting_activation_denied_timeout_seconds`, the 1.4 column default,
    which the 1.6 approval form also saved whatever it showed) becomes one
    day, the documented default. "Never" (-1) and other values are kept. If
    you really want denied accounts deleted at once, save 0 days, 0 hours and
    0 minutes again in **Admin → Settings → Approval**.

A 1.6 release refuses these newer databases; see
[Going back to 1.6](#going-back-to-16).

### Updates, backups and dependencies (managed servers)

* **Release tags.** New installations follow release tags (`vX.Y.Z`) instead
  of a branch's latest commit; until the source has a tag, updates report
  that there is nothing to update to. Existing servers keep following their
  branch until switched (see [After you update](#after-you-update)).
* **Signed automatic updates.** `updates enable` now refuses without
  `--require-signatures ALLOWED_SIGNERS` (a server that follows release tags
  needs an annotated tag signed for that version, `git tag -s vX.Y.Z`; one
  that follows a branch needs signed commits; higher tags that do not verify
  are skipped and reported as `skipped_releases`), unless `--allow-unsigned`
  is given. Automatic updates enabled before 1.7 keep running, and `status`
  warns while they deploy unsigned revisions. Clearing the signature
  requirement while automatic updates are on needs `--allow-unsigned` too.
  Only the 20 highest tags are tried, so anyone who can push tags to the
  update source can still hold updates back (as they could by deleting the
  release tags): let only maintainers create `v*` tags there (protected tags
  on the forge).
* **Locked dependencies.** Releases install `requirements.lock` and
  `requirements-build.lock` with `--require-hashes --only-binary=:all:`:
  every package at one version, accepted only with its SHA-256, wheels only.
  A release without locks (a rollback to 1.6) still installs its
  `requirements.txt`. Manual installations should install the lock too (see
  [Installations from a Git checkout](#installations-from-a-git-checkout));
  `requirements.txt` keeps the ranges for development. nh3 must be 0.3.7 or
  later. A 1.7 or later release that lacks either lock (or has only one) is
  refused before anything changes; `--allow-unpinned` on `update`, `restore`,
  `rollback` or `backups restore` installs it from its ranges anyway.
* **Restores check the source they deploy.** A restore no longer runs the
  source bundled in a package unchecked: it reuses a release this server
  already prepared, or builds the revision from this server's update source
  (signature-checked when signatures are required). Otherwise it stops before
  changing anything, shows the revision, and asks you to confirm it with
  `--trust-source REVISION`. This covers `install --restore` and
  `backups restore` onto a new or empty root (moving to a new server), and
  `restore` or `rollback` of a package whose revision is neither prepared
  here nor available from the update source. Scripts that migrate servers
  must pass `--trust-source`; the error message prints the exact revision.
  When the update source requires SSH-signed revisions, a restore never
  deploys an unverified revision from a package, even with `--trust-source`:
  to restore an older unsigned package, clear the requirement first
  (`source set --clear-signatures`). A restore onto an installed server keeps
  that server's update source settings, repository credentials and allowed
  signers (`source_warning` says when the package's differ); only a new
  server takes the package's. With signatures required, a release whose tag
  is signed restores like one whose commit is signed.
* **Backups include the allowed SSH commit signers** for required signed
  updates. Earlier packages may omit `config/repo.allowed_signers`; after
  restoring one, recover the original trust file and configure
  `source set --require-signatures FILE` before updating. Failed restores
  put back repository credentials and signer trust along with the previous
  data.
* **Remote backups:** `repository.json` may now contain `append_only`
  (default false; set with `backups configure --append-only`, then prune with
  `backups prune --token-file FILE`). Older releases ignore it and keep
  pruning on upload; with a token that cannot delete branches, that pruning
  only produces retention warnings. Retention counts only snapshots the
  series' recovery key authenticated, and `--allow-unauthenticated` refuses a
  snapshot dated at or after the series' first authenticated one (each
  authenticated snapshot records it).
* **Uploads named like databases** (`*.db`, `*.sqlite`, `*.sqlite3`,
  `*-wal`) are backed up as plain files and no longer fail backups. Only the
  wiki's and the portal's own databases are integrity-checked.
* Updates take an online snapshot first; `before-update-*` packages, old
  releases and old tenant images are pruned (keep count:
  `updates enable --keep-backups`). The default source link (`BW_SOURCE_URL`)
  and the default repository of new managed installations are
  `https://github.com/BananaSuite/BananaWiki`; existing installations keep
  their configured repository.

### Accounts, sign-in and passwords

* **Session cookie:** over HTTPS (or with `BW_SECURE_COOKIES=1`) the wiki
  session cookie is named `__Host-<name>` (for example `__Host-bw_session`).
  Users stay signed in: a session moves to the new name on its first HTTPS
  request. Going back to an earlier release signs HTTPS users out once, and
  tools that read the cookie by name over HTTPS must use the new name.
* **Device cookie:** a persistent cookie `<BW_SESSION_COOKIE_NAME>_device`
  (`__Host-` prefixed on HTTPS, HttpOnly, SameSite=Lax, 365 days) is set at
  sign-in. It gives a browser that signed in before its own sign-in failure
  budget; nothing to configure. It is signed with the account's device
  epoch: a new password, and turning two-step verification on, off or
  resetting it, retire the account's device cookies; a browser becomes known
  again at its next complete sign-in, and the browser that made the change
  gets a new one at once.
* **"Remember me"** really lasts 30 days: `PERMANENT_SESSION_LIFETIME` is
  now the longer of the session and remember-me lengths. Ordinary sessions
  still end after 7 days through their server-side row.
* **Password hashes** made with another method or cost than
  `BW_PASSWORD_HASH_METHOD` (for example 1.4 hashes) are re-hashed at each
  account's next successful sign-in. The password stays the same, so the
  account's API tokens and its known browsers keep working.
* **Two-step verification** is optional and starts off for every account.
  Accounts that use it are also asked for a code after the hosting portal's
  single sign-on (the portal's own two-step verification is unchanged), and
  when they link a portal account at `/platform-oauth/merge`. Only owners and
  superusers who use it themselves can switch on **Require two-step
  verification for administrators**; the setting is not writable through
  `PUT /api/v1/settings`. Lost app and recovery codes: an owner or superuser
  resets it in **Admin → Users**, or run `bananawiki reset-two-step NAME`
  (`sudo bananawiki reset-two-step NAME` on a managed server; on a hosting
  platform, the portal's "Reset admin password"). Wrong codes are limited to
  10 in 5 minutes and 50 a day per known browser (or for all other browsers
  together), at most 100 a day per account from all browsers, and after 10
  wrong codes in a day from one browser each further code waits 30 seconds,
  doubling up to an hour.
* **Password policy:** nothing to do for existing accounts; the policy
  applies only when a password is set. New passwords may not contain the
  user name or the part of the email address before the @, or be one of the
  10,000 most common passwords. The minimum length stays 8. Scripts that set
  passwords may get `password_common` or `password_personal` (HTTP 400 on the
  REST API).
* **Administration:** only owners and superusers change administrators,
  everywhere (REST API, owner toggle, temporary accounts, self-reactivation,
  token revocation, data export, profile moderation, mass sign-out). A plain
  administrator no longer sees other administrators' sign-in addresses.
  Suspensions recorded before the upgrade are lifted by an owner or
  superuser, not by the suspended administrator.
* **Whole-site export and import** (**Admin → Site migration**) need an
  owner or superuser; plain administrators get 403 and an explanation. On
  hosted wikis the provisioned account is an owner, so nothing changes there.
  Imported archives lose any triggers or views a fresh database of this
  release would not create (external plugins recreate theirs at their next
  start), and the live audit log is kept. Accounts whose two-step
  verification was set up under another secret key (the archive never
  contains it) have it turned off by the import, with a notice and an audit
  entry (`site.import_two_step_off`); they sign in with their password and set
  it up again. Keep the secret key when moving a wiki to keep two-step
  verification. Importing an export of the same wiki no longer duplicates
  the audit log.
* **Audit log:** only owners and superusers can switch it off (**Admin →
  Plugins**, or the onboarding wizard, which no longer offers it to plain
  administrators) or change its retention; plain administrators see the
  retention read-only. If it was switched off before the upgrade, it stays
  off. Every feature the wizard switches is recorded too.
* **Daily automatic sign-out** (**Admin → Sessions**) now ends only the
  sessions of users and editors; administrators, owners and superusers stay
  signed in (any administrator can set it, and the account hierarchy
  applies). If you relied on it for administrators, sign them out with
  **Admin → Sessions → Sign everyone out** as an owner or superuser.
* **Invite codes** made by editors beyond their creator's current rights, and
  "admin" invites made by since-demoted administrators, now create plain
  member accounts.
* **Bulk delete** forms and POSTs to `/admin/bulk/*` require
  `confirm_text=DELETE`, or the interface-language word (`ELIMINA` in
  Italian). Scripts posting to these URLs must add it.
* **Renaming and deleting accounts** no longer edits pages; members rename
  themselves at most 3 times a day and former names stay reserved.
* **Per-address limits** (sign-in, sign-up, platform sign-in, the global
  request limit, anonymous page, canvas, read-aloud and chat limits, group
  joins) count an IPv6 client by its /64; IPv4 is unchanged.

### Content, chat, plugins and read aloud

* **Permissions:** `page.view_all` and `category.view_all` are enforced. The
  upgrade grants them to every saved permission set (see
  [Permissions](#permissions)).
* **Categories:** deleting a category with its pages (web, API, bulk) is all
  or nothing and refuses hidden, forbidden, protected, checked-out or
  scheduled pages before anything changes.
* **Custom pages** on hosts that forbid public wikis are for signed-in
  members.
* **Page styles:** existing page content whose style attributes use negative
  margins, `vw`/`vh` units, `calc()`, `var()`, `!important`, or lengths above
  4000px, 100% or 250em loses those declarations when rendered. Ordinary
  layout styles are kept.
* **Heading anchors:** a heading called Main, Sidebar or Attachments now has
  the anchor `#main_1`, `#sidebar_1` or `#attachments_1` (a page builder
  **Heading** block: `#main-2` and so on), because those names belong to the
  skip link, the menu and the attachments panel. A saved link ending in
  `#attachments` that meant such a heading now reaches the attachments panel;
  links to `#main` and `#sidebar` never reached the heading. Other anchors
  are unchanged. Headings in the announcement bar have no anchor.
* **Kanban:** deleting an account no longer deletes its tickets and comments
  on other people's boards: they go to the board owner (comments start with a
  note naming the former author), its own boards go to an administrator, and
  it is removed from assignees and shares. A ticket holds at most 300
  comments; tickets that already hold more keep them. A listing of a ticket's
  comments renders them newest first, up to 300 and about 4 MB of HTML; older
  ones are shown as plain text. A column can now be marked as a done column
  (the check-mark button among the column tools, **Done column**): its
  tickets are never shown as overdue or due soon. No column is one after the
  upgrade, so boards look as before until someone marks their finished
  columns.
* **Chat:** clearing a direct conversation hides it for you only; the other
  participant keeps their copy (administrators can still clear it for both).
  A sender can delete a direct message for both participants only within 15
  minutes of sending it; older messages stay in the recipient's copy
  (clearing still hides them from your own view). Deleting an account still
  removes its direct conversations for both participants; a reported message
  keeps its copy in the report. Members can report messages (**Admin → Chats
  → Reports**) and block other members (**Settings → Blocked people**): what
  a blocked member sends in a conversation they already have is withheld
  from the one who blocked them, never delivered (administrators see it
  marked "Not delivered"). Chat exports still contain blocked members' group
  messages; a withheld direct message is in its sender's export only (and in
  administrators' exports). Moderation pages and exports hide the
  IP addresses of owners, superusers and (for plain administrators) other
  administrators.
* **Third-party plugins** are off by default for new installations, and only
  the owner and superusers can install, update or enable one (administrators
  can still switch plugins off and delete them). An upgraded installation
  whose plugins folder already holds plugins keeps them allowed while
  `BW_ALLOW_EXTERNAL_PLUGINS` is unset, and logs a warning at every start:
  set `BW_ALLOW_EXTERNAL_PLUGINS=1` to keep them (and silence the warning),
  or `0` to turn them off. New installations need
  `BW_ALLOW_EXTERNAL_PLUGINS=1` before any plugin can be uploaded.
* **Read aloud:** a job whose worker dies three times is marked failed;
  `BW_TTS_MAX_JOB_SECONDS` (default 3600) bounds a job. On Linux, Piper runs
  in a child process per job (about 1-2 s more per job) whose memory can be
  limited with `BW_TTS_PIPER_MEMORY_MB`. A wiki that sets
  `BW_TTS_REMOTE_GPU_AUTH_TOKEN` in the environment without
  `BW_TTS_REMOTE_GPU_URL` now ignores the URL saved on the admin page: set
  `BW_TTS_REMOTE_GPU_URL` in the environment as well.
* **GPU speech server** (contrib): drops clients that stay silent for
  `TTS_SOCKET_TIMEOUT` (30 s) and accepts at most `TTS_MAX_CONNECTIONS` (64)
  connections; `/health` reports `"version": "1.7.0"`. Hosting platforms give
  each wiki its own derived GPU token (`HOSTING_TTS_GPU_TENANT_TOKENS=1`),
  which needs this release's GPU server: upgrade it first, or set
  `HOSTING_TTS_GPU_TENANT_TOKENS=0` until you do.
* **Downloads:** `Content-Disposition` for stored files and for canvas and
  kanban exports carries an ASCII `filename` plus an RFC 5987 `filename*`. A
  token-safe ASCII name is sent unquoted (`filename=board.md`), which matters
  only to clients that parse the header literally.
* **Stalled downloads:** a response is abandoned once a write to the client
  has waited `BW_WRITE_TIMEOUT` seconds (`HOSTING_WRITE_TIMEOUT` for the
  portal; default 300, `0` waits forever). Readers that kept taking in 256 KiB
  to 4 MiB per period were not cut off in tests; raise the value to serve
  slower links (see [deployment](docs/deployment.md#reverse-proxy)). Gunicorn
  logs each such disconnect as `Socket error processing request` with
  `TimeoutError: timed out`.
* **Whole-site imports** refuse archives with encrypted members or members
  compressed other than stored or deflated (BananaWiki exports never contain
  them).

### REST API changes

* Changing your own account's password through `PUT /users/<id>` needs
  `current_password` (403 `current_password_required`,
  `current_password_wrong`). Wrong values count against the account page's
  password limit (10 in 15 minutes, shared with it); then 429
  `too_many_attempts`. Administrators still reset other accounts'
  passwords without it. New passwords may be refused with
  `password_common` or `password_personal` (400).
* Tokens created through `POST /tokens` are revoked with the token that
  issued them and cannot request the `tokens` or `userbot` scope. Scripts
  that call `/userbot/*` with an ordinary token need the automation key
  (automation mode on). No API call creates owners or superusers.
* `POST /api/preview` and `POST /api/code/highlight` answer 403 to accounts
  below the editor role; `/api/preview` allows 60 requests a minute per
  account (was 120).
* `DELETE /api/v1/categories/<id>?page_action=delete` also needs the `pages`
  write scope and answers 403 (`category_pages_hidden`,
  `category_pages_forbidden`), 409 (`category_pages_blocked`) or 202 (pages
  only scheduled for deletion). Token expiries are at most 10 years ahead
  (`expiry_too_far`); existing tokens keep theirs.
* The API reports version 2.3.0 (`/status`, `/openapi.json`). Kanban columns
  carry `done`, and `PUT /kanban/columns/<id>` accepts it (a boolean).
* JSON with non-finite numbers, unpaired surrogates or more than 64 nesting
  levels is refused (400). `Idempotency-Key` replays re-check current access
  and may answer 409 `idempotency_replay_unavailable` without running again;
  server, storage and routing errors under `/api/v1` carry `code` and
  `request_id`. Details in [REST API](#rest-api) below.

### Hosting platform

* **One broken wiki no longer rolls an update back.** A few wikis that do not
  start under the new release (at most one in ten) are stopped and reported
  (`isolated_tenants`, exit status 2) while the rest keep it. Set
  `BANANA_UPDATE_UNREADY_WIKIS=rollback` in `config/app.env` for the 1.6
  behaviour; any other value refuses updates before anything stops. An
  isolated wiki stays stopped across restarts of the maintenance service,
  reboots and policy changes until it is started or restarted from the portal
  (`platform_state/isolated-wikis.json`).
* **Wikis whose own plugins fail under a new release** (with
  `HOSTING_ALLOW_TENANT_PLUGINS=1`): when every wiki that does not come back
  holds plugins, the update first quarantines those plugins and is kept if
  the wikis then serve. The update result and `status` list them as
  `quarantined_tenants`; a portal administrator lifts the quarantine on the
  wiki's page, and the plugins it switched off stay off until the wiki's
  administrators switch them on. A wiki that still fails, or whose plugins
  cannot be quarantined (data the new release cannot migrate or a full
  storage quota fails the quarantine too, as does a release without the
  `hosting-admin instance quarantine-plugins` command), is isolated as above
  (plugins that were quarantined stay so; when only the quarantine marker
  could be set, the warning says so, and lifting the quarantine turns that
  wiki's plugins on again) or, with
  `BANANA_UPDATE_UNREADY_WIKIS=rollback` or when more wikis fail than
  isolation takes, rolls the update back.
* **Storage:** where `BW_HOSTING_TENANT_QUOTAS` and the storage allow (see
  [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)), the runtime
  agent assigns and verifies hard XFS project byte
  and inode quotas before every seed, import, copy, restore and launch
  (ceilings `HOSTING_AGENT_MAX_STORAGE_BYTES`, `HOSTING_AGENT_MAX_INODES`).
  Capacity is checked only when a wiki is admitted or its limit raised.
  Update and backup snapshots staged on the wikis' XFS volume must leave
  every admitted wiki's unused limit plus
  `HOSTING_AGENT_STORAGE_RESERVE_BYTES` free, or they stop nothing and report
  the space needed: put `backups/` on another filesystem if that volume is
  tight. Wikis adopted from 1.6 count only their adoption headroom there
  until a start admits them (see [Before you update](#before-you-update)).
* **Storage suspensions end by themselves.** Wikis suspended only by the
  automatic storage protection (in 1.4, 1.6 or 1.7) that fit within their
  limit are reopened at the first maintenance pass after the upgrade, and
  their owners are emailed. A suspension made by an administrator is never
  lifted automatically.
* **Firewall and isolation:** tenant bridges are renamed `bwt<hex>` (see
  [After you update](#after-you-update)). Every wiki still runs as the
  portal's service UID: per-tenant UIDs, userns-remap and rootless Docker are
  not supported in 1.7 (see "One UID for every wiki" in
  [docs/hosting.md](docs/hosting.md) for the threat model and mitigations).
  Backups and updates copy each hosted wiki's database as bytes and never
  open it as root.
* **Tenant plugins:** hosted third-party Python plugins need
  `HOSTING_ALLOW_TENANT_PLUGINS=1` (default off; files and settings are
  kept, built-in features are unaffected). Operators who trust their
  tenants' plugins set it and restart the portal and the tenant containers.
  It applies only in subdomain mode (see
  [addresses](docs/hosting.md#addresses)); quarantined wikis always keep
  external plugins disabled.
* **New accounts:** self-service accounts younger than
  `HOSTING_NEW_ACCOUNT_DAYS` (7) that no administrator approved may hold
  `HOSTING_NEW_ACCOUNT_MAX_INSTANCES` (1) wiki, and all such accounts
  together may use at most `HOSTING_NEW_ACCOUNT_SHARE_PERCENT` (50) of the
  platform's wiki limit; the sign-up form creates at most
  `HOSTING_SIGNUPS_PER_IP_PER_DAY` (10) accounts per address per day. Sign-ups
  from invite codes count as new too: approve those accounts, or set
  `HOSTING_NEW_ACCOUNT_DAYS=0` if you trust your invitees. Suspended wikis
  count toward `MAX_INSTANCES_PER_ACCOUNT`, and so do accepted transfers.
* **Email:** verification no longer turns itself off when the daily budget
  is spent. `HOSTING_EMAIL_DAILY_LIMIT` is a rolling 24-hour budget shared by
  every worker (before, each worker counted its own), with 20% reserved for
  account recovery; each account may cause at most
  `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` (10) messages a day. Raise the limit if
  you relied on it being multiplied by the number of workers. Password resets
  and user-name reminders use only that fifth (reminders at most half of it),
  each address receives at most 3 of each a day, and each client network (an
  IPv4 address or an IPv6 /64) may make 10 requests a day across the two
  "forgot" forms, an IPv6 /48 30. When the recovery fifth is spent, the forms
  say so and administrators see it in `/admin/attention`; a platform that
  relied on sending more recovery mail needs a higher daily limit.
* **Addresses:** within 30 days, only the first three addresses an account
  takes (signing up counts) are held against other accounts from the moment
  they are typed; later ones count from the first, confirmed or not.
* **Portal device cookies** are signed over the account's two-step epoch.
  Cookies issued by 1.6 are not recognised after the upgrade, so each browser
  uses the shared sign-in and code budgets until its next successful
  sign-in. Nothing to do.
* **Transfers:** accepting an ownership transfer no longer keeps the
  previous owner as a full-access collaborator (the recipient chooses no
  access or view-only). Collaborator rows created by past transfers are left
  as they are.
* **Expiry:** `INSTANCE_DURATION_DAYS=0` means no expiry. Owners are emailed
  before a wiki expires (`HOSTING_EXPIRY_WARNING_DAYS`, default 3 and 1 days)
  and when it expires. **Owners may renew their wikis** is off by default;
  when on, only the owner (or an administrator) renews, and an end date an
  administrator brought forward cannot be renewed away. A lapsed custom
  domain moves to another wiki only once the claimant verifies it.
* **Two-step verification for administrators:** when it is required,
  administrators without it are sent to `/account/mfa` from every portal
  page, not only `/admin`, and have no administrator rights over customers'
  wikis until they turn it on.
* **"Reset admin password"** (owner dashboard, admin page, and
  `sudo bananawiki hosting-admin instance reset-password`) also turns off the
  reset account's two-step verification on the wiki and records it in the
  wiki's audit log. It takes effect with the 1.7 tenant image, because the
  task runs inside the tenant; the CLI's JSON output gains
  `"two_step_reset": true`.
* **Per-address limits** of the portal group IPv6 clients by /64 (20 failed
  sign-ins per 15 minutes, 300 requests per minute per worker, daily
  sign-ups); IPv4 is unchanged.
* **Moderation log retention:** the maintenance service deletes
  `hosting_events` entries older than `HOSTING_EVENT_RETENTION_DAYS` (3650,
  the 10 years the privacy policy promises; 0 keeps everything). The first
  pass after the upgrade deletes entries older than 10 years, which only
  matters for platforms that old.
* **Terms:** `HOSTING_TERMS_VERSION` (default: the release's
  `TERMS_VERSION`, 2026-07-22) is the version date of the terms and privacy
  pages you publish, recorded when an account accepts them: set it to
  2026-10-09 if you publish the 1.7 site pages.
  `HOSTING_TERMS_REQUIRED_VERSION` (unset by default, so nobody is asked)
  sends every account that accepted an older version of the terms, or none,
  to `/account/terms` before anything else in the portal. Set it to the same
  date as `HOSTING_TERMS_VERSION` on the date new terms take effect (see
  [docs/hosting.md](docs/hosting.md)); the portal, the maintenance service
  and the CLI refuse to start if it is later than `HOSTING_TERMS_VERSION`, or
  if either is not a real date. API tokens and wiki sign-in tokens of an
  account that has not accepted are refused until it does; revoking them
  always works. For code: `auth.GATES` gains `'terms'`; code that exempts
  `*auth.GATES` is unaffected, views that exempt named gates do not skip it.
* **Legal pages:** `/terms`, `/privacy` and `/compliance` send visitors whose
  portal language is Italian to the site's Italian pages; German visitors get
  the English ones. With `HOSTING_TERMS_URL` and `HOSTING_PRIVACY_URL` they
  lead to your own pages instead, and each other interface language can have
  its own pair (`HOSTING_TERMS_URL_IT` and `HOSTING_PRIVACY_URL_IT`, `_DE` for
  German); see
  [docs/hosting.md](docs/hosting.md#help-centre-and-legal-pages).
* **Service name:** `HOSTING_SERVICE_NAME` (default `BananaWiki Hosting`)
  names the platform in page titles, the top bar, emails, the maintenance
  page, wikis' status pages and new authenticator enrolments; the footer
  keeps naming the software.
* **Pause deletion countdown** really pauses the purge; 1.4 wikis whose
  download was blocked stay paused until an administrator resumes them.
  Hosting dates (wiki expiry, suspensions, invites, banners) accept the years
  1900-9998; expiries already stored after 9998 are kept and can be
  shortened.
* **Platform backups** no longer stop at the first wiki that fails: such a
  wiki is listed as incomplete on the settings card (and in
  `backup_manifest.json`), and a backup is "complete" only when every wiki
  was saved in full. Each wiki may add at most its storage limit plus a tenth
  and 64 MiB. Tenant archives with bzip2 or LZMA members are refused.
* **Wiki exports** (grace-period and administrator downloads) fail with a
  message when the wiki's files or its database copy exceed its storage limit
  plus a tenth and 64 MiB; further hard links to a file are listed in the
  export's `manifest.json` instead of being stored again. For a wiki adopted
  above its plan under hard quotas, exports and platform backups count from
  the hard limit it was adopted at instead of its plan. Without hard quotas a
  wiki whose plan was lowered below what it holds has no such limit: raise
  its plan back before its next backup or download.
* **Account merges** done before this release may have left the merged
  account as a collaborator of its own wikis, which would keep it access after
  it transfers one. To remove such rows, run once against the portal database
  (`HOSTING_DATABASE_PATH`):
  `sqlite3 hosting.db "DELETE FROM instance_collaborators WHERE (instance_id, account_id) IN (SELECT id, account_id FROM instances);"`
* **Upgrade from 1.4:** the takeover of `hosting.db` repairs chains of
  orphaned rows of any length (it stopped after 8 passes).

### Container images

Building the images now needs Docker's BuildKit component, because both
Dockerfiles use `RUN --mount`: `docker buildx version` must answer. Install
`docker-buildx` (Debian and Ubuntu with their `docker.io`) or
`docker-buildx-plugin` (Docker CE); without it `docker build` falls back to
the legacy builder and stops with "the --mount option requires BuildKit".

Images use Debian 13 and apply available distribution updates. Runtime
images omit `pip`, `setuptools`, `wheel` and `ensurepip`; add custom
dependencies during the image build. Both Dockerfiles install
`requirements.lock`. Rebuild your images (`docker compose build --pull
--no-cache`) to receive these changes; instance data and existing plugin
files remain in `/data`. The common-password list ships inside the package
(`bananawiki/core/data/`), so no Dockerfile change is needed for it.

### Interface

The interface refresh keeps existing URLs, themes and display preferences.
Hosting account settings have a section index, and administrators open the
account and invite creation forms from their dashboard section headings.
Secondary page actions are in **Page actions**, and new pages and categories
start from **New** in the sidebar. The editor groups less common commands
under **Formatting** and **Insert**; Tab moves to the next field.
Administration links are grouped by task, and the feature list has a search
field. Tables whose row actions do not fit become one card per row. Italian
now calls a wiki feminine everywhere (*la wiki*, as the site and README.it
did), and the portal uses the wiki's names for shared features (for example
*Editor visuale delle pagine*, *verifica in due passaggi*); in English the
portal says "Two-step verification" instead of "Two-step sign-in". Update
screenshots or user instructions that quote the old labels. These interface
changes require no configuration or database changes.

German is a new built-in interface language, switched on in every wiki after
the update: it appears in the language menus, and signed-out visitors whose
browser prefers German get it (the portal and its help centre too). A
browser that sends a bare `Accept-Language: *`, or prefers several languages
equally including the default, gets the default language (on the portal,
English) rather than the alphabetically first enabled one, which would now
be German. To hide German, switch it off under **Admin → Languages** (choose
another default language first if German is the default). A `de` file
uploaded earlier (or moved from a 1.4 `translations/` folder) stays in
`<instance>/translations/` and now overrides the bundled German: its strings
win, the strings it lacks come from the bundled German instead of English,
and the on/off switch saved with it still applies. **Remove changes** on the
same page deletes the file and keeps the bundled German, also when German is
the default. A hosting platform that sets its own legal pages can add
`HOSTING_TERMS_URL_DE` and `HOSTING_PRIVACY_URL_DE` for German visitors, who
otherwise get the default addresses (or the static site's English pages).
Read aloud does not follow the interface language: pages are read in their
detected language among the read-aloud languages (English and Italian unless
changed), so German pages are read with the English or Italian voice until
German is ticked under **Admin → Read aloud** (voice `de_DE-thorsten-medium`,
downloaded on first use); detecting German pages automatically also needs
`langdetect`.

In the hosting portal, "Sign out other devices" now keeps the browser that
asks signed in, and the two-step page's "Back to sign in" ends the pending
sign-in.

After the update every signed-in account sees the mascot, a pixel-art banana,
in place of the top bar logo (feature id `mascot`, a built-in plugin that is
on by default). A click on it makes it hop instead of opening the home page;
the site name next to it still links home. To hide it for everyone, use
**Hide for everyone** under **Admin → Appearance** (people can switch it back
on under **Customise**), or switch the plugin off under **Admin → Plugins**,
which brings the logo back. Installations that must never show it add
`mascot` to `BW_MANAGED_PLUGIN_DENYLIST` (on a hosting platform,
`HOSTING_TENANT_PLUGIN_DENYLIST`). Its two choices are keys of the account's
display preferences, so nothing changes in the database; see
[docs/features.md](docs/features.md#mascot).

### New and changed settings

All are optional. They are documented in
[docs/configuration.md](docs/configuration.md), and the `BW_`/`HOSTING_` ones
are in `.env.example`.

| Setting | Default | What it does |
|---|---|---|
| `BW_ALLOW_EXTERNAL_PLUGINS` | off; kept on, with a warning, while unset on an installation that already has plugins | Third-party plugins (changed meaning, see above) |
| `BW_TTS_MAX_JOB_SECONDS` | 3600 | Longest read-aloud job |
| `BW_TTS_PIPER_MEMORY_MB` | 0 (none) | Memory limit of the Piper process (Linux) |
| `BW_LOG_FORMAT`, `HOSTING_LOG_FORMAT` | text | `json` writes one JSON object per line |
| `BW_WRITE_TIMEOUT`, `HOSTING_WRITE_TIMEOUT` | 300 | Seconds a response write may wait for a client that stopped reading (0: forever) |
| `BW_HOSTING_RECOVERY_MAX_WORKERS` | 2 | Wikis restarted at once after an update or a backup (1.6: fixed 4) |
| `BANANA_PACKAGE_MAX_FILES` | 1000000 | Files a managed package may hold (`config/app.env`) |
| `BANANA_UPDATE_UNREADY_WIKIS` | `isolate` | `rollback` for the 1.6 update behaviour (`config/app.env`) |
| `BW_HOSTING_TENANT_QUOTAS` | `auto` (new platforms too) | Hard per-wiki XFS quotas: where the storage enforces them, always (`required`, refusing other storage), or never (see [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)) |
| `HOSTING_AGENT_MAX_STORAGE_BYTES`, `HOSTING_AGENT_MAX_INODES` | 10 GiB, 100000 | Per-wiki hard quota ceilings |
| `HOSTING_AGENT_PROJECT_ID_START` | 1000000 | First XFS project ID |
| `HOSTING_AGENT_STORAGE_RESERVE_BYTES` | 268435456 | Reserve kept free when admitting wikis and staging snapshots |
| `HOSTING_ALLOW_TENANT_PLUGINS` | off | Hosted third-party plugins (subdomain mode only) |
| `HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED` | on | Accept custom domains proxied by Cloudflare |
| `HOSTING_TTS_GPU_TENANT_TOKENS` | 1 | A derived GPU speech token per wiki (needs this release's GPU server) |
| `HOSTING_SIGNUPS_PER_IP_PER_DAY` | 10 | Accounts the sign-up form creates per address per day |
| `HOSTING_NEW_ACCOUNT_DAYS` | 7 | How long an unapproved self-service account counts as new |
| `HOSTING_NEW_ACCOUNT_MAX_INSTANCES` | 1 | Wikis a new account may hold |
| `HOSTING_NEW_ACCOUNT_SHARE_PERCENT` | 50 | Share of the platform's wiki limit all new accounts may use |
| `HOSTING_EXPIRY_WARNING_DAYS` | 3,1 | When owners are warned before a wiki expires |
| `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` | 10 | Messages one account may cause per day |
| `HOSTING_EVENT_RETENTION_DAYS` | 3650 | Moderation-log retention (0 keeps everything) |
| `HOSTING_SERVICE_NAME` | `BananaWiki Hosting` | The platform's name in its pages, emails and authenticator apps |
| `HOSTING_TERMS_URL`, `HOSTING_PRIVACY_URL` | unset (the static site's pages) | Your own terms and privacy pages, both or neither |
| `HOSTING_TERMS_URL_<LANG>`, `HOSTING_PRIVACY_URL_<LANG>` | the two above | A pair for each interface language but English: `_IT`, `_DE` |
| `HOSTING_TERMS_VERSION` | the release's `TERMS_VERSION` | Version date of the terms you publish, recorded on acceptance |
| `HOSTING_TERMS_REQUIRED_VERSION` | unset | Terms version every account must have accepted |
| `INSTANCE_DURATION_DAYS` | 14 | Now accepts 0 (no expiry) |
| `TTS_SOCKET_TIMEOUT`, `TTS_MAX_CONNECTIONS` | 30, 64 | GPU speech server (contrib) connection limits |

`HOSTING_EMAIL_SMTP_PORT`, `HOSTING_EMAIL_SMTP_USERNAME` and
`HOSTING_EMAIL_SMTP_PASSWORD` are unchanged; only their documentation now
spells out the full names.

### Going back to 1.6

1.6 refuses a database newer than the schema it knows ("newer than this
release supports"), so going back always means restoring the data from
before the update. **Changes made after the update are lost.**

* **Managed:** `sudo bananawiki rollback` restores the package written before
  the update (source, data and configuration). When the old release is no
  longer prepared on the server and its revision is not available from the
  update source, `rollback` asks you to confirm the revision with
  `--trust-source REVISION` (a server that requires signed revisions says
  how to allow an unsigned one). A rollback to 1.6 installs 1.6's
  `requirements.txt`, because 1.6 has no lock. It keeps this server's update
  source settings and allowed signers. The 1.6 controller ignores
  `--track tags` and follows the branch again, and with signatures required it
  needs each commit signed: run `source set --clear-signatures` before
  updating from 1.6 if only the release tags are signed.
* **Checkout:** stop the wiki; check out the commit it ran before the update
  (`git checkout v1.6.0` for the 1.6.0 release) and reinstall its
  `requirements.txt`. Replace `bananawiki.db` with the copy the first 1.7
  start saved, `<instance>/backups/pre-upgrade-v<old>-<time>.db`: its log
  names it ("Database schema N will be upgraded to 11; a copy was saved to
  …"), and `<old>` is the schema of the commit you go back to (4 for 1.6.0;
  development builds after 1.6.0 use 5 to 7). Take the newest copy with that
  number: an older one, or one with another number, was saved by an earlier
  upgrade and lacks everything written since. 1.6.0 cannot open a copy of
  schema 5 to 7; go back to the commit that wrote it instead. Delete
  `bananawiki.db-wal` and `bananawiki.db-shm`, then start the wiki. (A
  hosted wiki saves no such copy: a hosting platform goes back with
  `rollback`.)
* **Docker/desktop:** restore the backup you took before.

Going back signs HTTPS users out once (the `__Host-` cookie name).

## Upgrading from 1.4

BananaWiki 1.6 was a rewrite that takes over a 1.4 installation (or any
earlier 1.x release at schema 3) **in place**: same database, same tables and
columns, same files, same secret key, same environment variables, same URLs.
Accounts, passwords, pages, history, sessions ("remember me" cookies
included) and API tokens survive. 1.7 takes over a 1.4 installation the same
way. This section explains how to upgrade each kind of installation, what
happens on the first start, how to go back, and every behaviour change since
1.4; what changed after 1.6 is in
[Upgrading from 1.6 to 1.7](#upgrading-from-16-to-17), which applies to a 1.4
installation too.

The upgrade is covered by an automated test (`tests/test_upgrade_from_1x.py`)
that boots the current release on a real 1.4 instance and checks that users,
sessions, tokens, content and files survive.

### Before you start

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

### Managed servers (`banana`)

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

### Installations from a Git checkout

```sh
cd /path/to/BananaWiki
git fetch --tags
git checkout v1.7.0       # the release tag you upgrade to
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock   # inside the wiki's virtual environment
# restart the service, e.g.
sudo systemctl restart bananawiki
```

`requirements.lock` holds the exact, hash-pinned wheels the release was
tested with; `requirements.txt` keeps the ranges for development. The start
command does not change: `gunicorn -c gunicorn.conf.py wsgi:app`
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

### Docker

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

### Desktop app

Install BananaWiki Desktop and point it at the folder the 1.4 Easy
Deployment App used (the `bananawiki/` folder next to the old program): it is
opened in place. A ZIP made with **Export All** in the old app can be
restored with **Restore…**. See [docs/desktop.md](docs/desktop.md).

### What happens on the first start

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
5. **Migrations 5 → 10** follow, each in its own transaction (see
   [What changes in the databases](#what-changes-in-the-databases)).
6. Every later start only reads the schema version.

Tables of plugins that 1.6 no longer ships are left untouched. When you no
longer need their data, `bananawiki db prune-retired` drops them after another
backup.

If the migration fails, nothing is changed and the wiki does not start; the
log names the problem. The `pre-upgrade` copy is there in any case.

### Going back to 1.4

1.4 refuses a database at schema 4 or later, so going back always means
restoring the data from before the upgrade. **Changes made after the upgrade are lost.**

* **Managed:** `sudo bananawiki rollback`. It restores the package the 1.4
  updater wrote before the upgrade (source, data and configuration), with the
  1.4 units. `sudo bananawiki backup` first if you want to keep the current
  state. If the 1.4 release is no longer prepared on the server and cannot
  be built from the update source, `rollback` asks you to confirm its
  revision with `--trust-source REVISION`; while the update source requires
  signed revisions it refuses an unsigned one, so clear that requirement
  first (`source set --clear-signatures`).
* **Checkout:** stop the wiki; `git checkout` the 1.4 commit and reinstall its
  `requirements.txt`; replace `bananawiki.db` with
  `<instance>/backups/pre-upgrade-v3-*.db` and delete `bananawiki.db-wal` and
  `bananawiki.db-shm`; either move the relocated folders back or set the
  `BW_*` folder variables to their new locations; start 1.4.
* **Docker/desktop:** restore the backup of the data you took before.

### Behaviour changes

#### Accounts, sessions and administration

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

#### Permissions

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

#### REST API

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

#### Features

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

#### Configuration and operations

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
  inode quotas before every seed, import, copy, restore and launch, unless
  `BW_HOSTING_TENANT_QUOTAS` is `off`, or `auto` on storage that does not
  enforce them (then the wikis run without hard quotas, with a notice, or a
  loud warning when they were enforced there before). Where quotas apply,
  changed project identities or too little capacity make it refuse, as does
  storage without enforced project quotas under `required`, and the wiki
  stays stopped. Existing wikis are adopted
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

#### Removed

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
