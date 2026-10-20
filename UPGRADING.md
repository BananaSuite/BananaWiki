# Upgrading BananaWiki

* [Upgrading from 1.7.1 to 1.7.2](#upgrading-from-171-to-172)
* [Upgrading from 1.7.0 to 1.7.1](#upgrading-from-170-to-171)
* [Upgrading from 1.6 to 1.7](#upgrading-from-16-to-17)
* [Upgrading from 1.4](#upgrading-from-14)

Read every section between the release you run and the one you install. A
1.6 installation that updates straight to 1.7.2 follows the 1.6 to 1.7
section, then the 1.7.0 to 1.7.1 and 1.7.1 to 1.7.2 sections. A 1.4
installation follows all of them.

## Upgrading from 1.7.1 to 1.7.2

There is no database migration. Only hosting platforms need to act:

### Hosting platforms: certificates

`sudo bananawiki update` rewrites the Caddyfile: the header timeout becomes 60
seconds and certificates come from Let's Encrypt only, without the TLS-ALPN
challenge. Port 80 must reach Caddy. Behind Cloudflare, either use
`sudo bananawiki proxy --tls cloudflare-dns` (one wildcard certificate, no
issuance when a wiki is created) or keep `acme` mode with "Always Use HTTPS"
off, or a rule that skips `/.well-known/acme-challenge/`. A DNS-only setup
needs a wildcard A or AAAA record for the platform's domain. If a new wiki
still has no certificate, `journalctl -u caddy` shows why (`rateLimited`,
a failed challenge) and the portal log says which names were refused.

## Upgrading from 1.7.0 to 1.7.1

1.7.1 updates a 1.7.0 installation in place. The first start migrates the
wiki database from schema 11 to 12 (a new index on page history, a repair of
stored idempotency tokens and the new default colours). The time grows with
the page history: a wiki with 55,000 revisions takes about half a second.

### Before updating to 1.7.1

1. Back up. On a managed server run `sudo bananawiki backup` (the update also
   writes its own package). Otherwise stop the wiki and copy the instance
   directory, the upload folders and the secret key. You need this copy to go
   back to 1.7.0.
2. Managed servers: every backup and update now prunes `remote-*` packages
   (kept after a failed encrypted Git backup) and `before-restore-*` packages,
   keeping the newest `--keep-backups` of each (3 by default). Copy the ones
   you want to keep elsewhere, or rename them (for example to `manual-...`).
3. Managed servers: check free space. The pre-update snapshot now reserves
   room for a second copy of every database, each hosted wiki's included,
   because a rollback copies databases back. Uploads are linked and need no
   extra room. A nearly full server can refuse an update that 1.7.0 would
   have started.
4. Image builds (the Docker images and the hosting tenant image) need HTTPS
   access to `snapshot.debian.org` as well as `deb.debian.org`.

Then update the usual way (`sudo bananawiki update` on a managed server).

### Changes to check in 1.7.1

Wikis:

* The default colours are now yellow on charcoal. A theme (dark or light)
  whose six colours were all still the old blue defaults takes the new ones;
  a theme with any colour chosen in **Admin → Appearance** keeps all of its
  colours.
* Administrators change their own password in **Settings**, which asks for
  the current password; **Admin → Users** links there for their own account.
* The in-app JSON endpoints (not `/api/v1`) return a readable sentence in
  `error` when maintenance or the account's state refuses a request. The
  machine token is in a new `code` field (`maintenance`, `suspended`,
  `pending`, `denied`, `account_step_required`): scripts must read `code`.
* An automatic badge that an administrator revoked is not awarded again. To
  let members earn it again, revoke it with **Delete the award instead of
  marking it revoked**.
* The REST API audit log returns `ip_address: null` and `ip_hidden: true`
  for accounts the caller may not manage.
* Changing the read-aloud GPU server's scheme, host or port clears its saved
  token, and changing the SMTP server name clears the SMTP password: enter
  them again in the same form.
* GPU speech server (`contrib/tts-gpu-server`): copy the new `server.py` to
  the GPU machine and restart `bananawiki-tts-gpu`. No configuration changes.
* `bananawiki import` into a new or empty instance directory uses the
  export's `.secret_key` when `SECRET_KEY` is unset. A `SECRET_KEY` you set
  must match the old server's, or two-step verification and API tokens stop
  working.

Desktop app:

* Third-party plugins (for example from a 1.4 installation) stay installed
  but do not load until you add `"allow_plugins": true` to the launcher's
  `desktop.json` (in `%APPDATA%\BananaWiki`,
  `~/Library/Application Support/BananaWiki` or `~/.config/bananawiki`) or
  run `BananaWiki serve --data-dir DIR --allow-plugins`.
* Backups no longer include `plugins/`. Restoring an older archive skips its
  `plugins/` folder, switches off the third-party plugins the restored
  database had enabled, and moves the plugins installed before the restore
  to `previous/<time>/`.
* Building the app installs from the hash-checked locks; see
  [docs/desktop.md](docs/desktop.md).

Managed servers:

* `backups configure` with a different recovery key than the one the server
  holds needs `--replace-key`; scripts that rotate keys must add it.
  `--key-file` is optional when the server already holds the key.
* `backups restore` holds the backup lock throughout, so a scheduled
  `backups run` that starts meanwhile fails and keeps its local package.
* `backups run` needs four times the package size plus 144 MiB free in
  `config/remote-backup` and checks this before uploading.

Hosting platforms:

* Lifting an account suspension with its wikis (**Reactivate**, or
  `hosting-admin account unsuspend --also-instances`) restarts only the
  wikis whose latest suspension came from the account. Wikis suspended by
  the 1.7.0 command `account suspend --also-instances` carry the duration
  (for example `24h`) as their label instead of `account`, so they stay
  suspended: reactivate them on their own pages.
* Wikis that a timed account suspension took offline under 1.7.0 were
  suspended with no end date. If the account suspension has ended or was
  lifted and those wikis are still suspended, reactivate each one from its
  own page. From 1.7.1 such wikis come back when the account suspension ends
  or is lifted, unless they are offline because of storage.
* Saving or disabling the instance URL suffix is refused while a wiki would
  lose its address: rename the listed wikis first. Where the suffix is
  already disabled and an apex wiki and a hosted wiki share a name, the apex
  wiki answers at that address; rename one of the two.
* Platform backups include each wiki's plugin quarantine
  (`platform_state/<instance id>/plugin_quarantine`). Restoring with 1.7.0 or
  earlier ignores it.
* Under hard quotas, a restore requests the agent's ceiling for
  administrators', apex and unlimited wikis, as a normal start does, so on a
  small XFS volume it can be refused for lack of capacity.
* With `SUBDOMAIN_MAX_LENGTH` above 42, restoring a wiki terminated from now
  on brings it back under the first 42 characters of its name.
* New JSON output fields: `storage_held` in `account unsuspend
  --also-instances`, `two_step_off` in `instance duplicate` and
  `instance import-archive`. Third-party runtime implementations must return
  the list of names from `import_archive` and `duplicate`.

### Going back to 1.7.0

1.7.0 refuses a schema-12 database, so going back means restoring the data
from before the update. Changes made after the update are lost. 1.7.0 also
refuses whole-site archives exported from 1.7.1.

* Managed: `sudo bananawiki rollback` restores the package written before the
  update.
* Checkout: stop the wiki, `git checkout v1.7.0`, reinstall
  `requirements.lock`, replace `bananawiki.db` with the newest
  `<instance>/backups/pre-upgrade-v11-<time>.db`, delete `bananawiki.db-wal`
  and `bananawiki.db-shm`, then start the wiki.
* Docker and desktop: restore the backup from step 1.

## Upgrading from 1.6 to 1.7

1.7 upgrades a 1.6 installation in place: same database, files, secret key,
environment variables and URLs. Accounts, passwords, sessions, API tokens and
content survive. The first start migrates the databases.

### Before you update

1. Managed servers: check the update source with
   `sudo bananawiki source show`. Servers installed with the old default
   follow `https://github.com/OverloadedTech/BananaWiki.git`, which no longer
   exists, and every update fails with "Git ls-remote failed. Check
   repository access, credentials and the branch." Switch first (the 1.6
   controller accepts this):

   ```sh
   sudo bananawiki source set --repo https://github.com/BananaSuite/BananaWiki.git --branch main
   ```

   The next update also points the wiki's source link (`BW_SOURCE_URL`) at
   the new repository, unless you changed that link by hand.
2. Back up data and secret key: `sudo bananawiki backup` on a managed server;
   otherwise stop the wiki and copy the instance directory, the upload
   folders and the secret key. The first start also writes
   `<instance>/backups/pre-upgrade-v<old>-<time>.db` before migrating (hosted
   wikis do not; the platform's update package covers them).
3. Keep the secret key. Two-step verification secrets are derived from it:
   with a different key, accounts that use two-step verification cannot pass
   the code step until reset with `bananawiki reset-two-step NAME`.
4. If the wiki runs plugins you uploaded, set `BW_ALLOW_EXTERNAL_PLUGINS=1`
   to keep them (see [Accounts, content and plugins](#accounts-content-and-plugins)).
5. Upgrade the read-aloud worker together with the web application: a worker
   of an earlier release cannot open the new database. Upgrade the contrib
   GPU speech server too.
6. Managed servers with more than about 76,000 files in `data/`: 1.6
   controllers fail every backup and update of such an installation, so run
   this one update with the 1.7 controller from a checkout (see
   [Updating with a controller from before 1.7](#updating-with-a-controller-from-before-17)).
   Packages now hold up to `BANANA_PACKAGE_MAX_FILES` files (default
   1,000,000).
7. Hosting platforms:
   * Check which controller will run the update: `sudo bananawiki preflight`
     prints a line of JSON from a 1.7 controller and "invalid choice:
     'preflight'" from an earlier one. An earlier controller stops the wikis
     before anything can refuse the update: read
     [Updating with a controller from before 1.7](#updating-with-a-controller-from-before-17)
     first.
   * Install Docker's BuildKit component: `docker buildx version` must
     answer. Install `docker-buildx` (Debian or Ubuntu `docker.io`) or
     `docker-buildx-plugin` (Docker CE). Without it a 1.7 controller refuses
     the update; an earlier one fails while building the image ("the --mount
     option requires BuildKit"), records the commit as failed and leaves the
     wikis running.
   * Wiki storage: nothing has to change. Any storage works; on XFS mounted
     with `prjquota` every wiki also gets hard limits (see
     [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)). Without
     hard quotas, wikis without a plan (the apex wiki, administrators' wikis)
     are now held to `HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB), and one
     already above it accepts no more uploads. Check their size with
     `sudo du -sh /opt/bananawiki/data/instances/*` and raise the variable in
     `config/app.env` if needed.
   * Do not update an arm64 hosting server: hosting runs only on Linux
     x86_64, and the update would be rolled back.
   * In port or onion mode (no domain, or `BASE_DOMAIN` empty, an IP address
     or `localhost`), remove `HOSTING_ALLOW_TENANT_PLUGINS=1` from
     `config/app.env` or set it to `0`. Otherwise the portal refuses to
     start: a 1.7 controller refuses the update, an earlier one rolls it back
     and records the commit as failed (fix the setting, then run
     `update --retry-failed`).
   * The kernel must have IPv6: `ls /proc/sys/net/ipv6` must list files. Wiki
     containers set IPv6 sysctls that Docker cannot set on a host booted with
     `ipv6.disable=1`, so no wiki would start. Remove that option and reboot;
     to keep IPv6 off on the host, set `net.ipv6.conf.all.disable_ipv6=1`
     instead.
   * Under hard quotas, a wiki above its plan, the 10 GiB ceiling or 100,000
     files still starts: it is held at what it holds plus 64 MiB and 1,024
     files, and its owner is emailed. Raise its plan or have the owner delete
     files. A wiki with more than 200,000 files needs
     `HOSTING_AGENT_MAX_INODES` raised before the update. If the plans add up
     to more than the volume holds, see [docs/hosting.md](docs/hosting.md).
   * Wikis suspended by the automatic storage protection reopen at the first
     maintenance pass if they fit within their limit. Suspend again, with a
     reason of your own, any that must stay closed.
   * The Docker registry, the Debian mirror and PyPI must be reachable:
     every update rebuilds the tenant image.

### After you update

* Managed servers: switch to release tags and signed updates once the
  version you want is tagged:
  `sudo bananawiki source set --track tags --require-signatures ALLOWED_SIGNERS`
  (`source check` shows what an update would deploy). See
  [signed releases](docs/operations.md#signed-releases).
* Hosting: load the host firewall with
  `sudo bananawiki firewall | sudo nft -f -`, and make it persistent from a
  saved copy of that output, not from the shipped
  `deploy/nftables/bananawiki-tenants.nft` (its resolver set is empty, so
  wikis on a host with a LAN resolver would get no DNS). Tenant bridges are
  now named `bwt…`: review hand-written rules that matched `br+`.
* Hosting: check `tenant_storage` in `sudo bananawiki status`. Right after an
  update by an older controller it reads false until this release's units are
  installed (`units_current: true`). If `hard_quotas` is false, decide whether
  the portal's storage checks are enough or move the storage to XFS. To have
  wikis refused rather than run without hard quotas, run
  `sudo bananawiki config set tenant-quotas required`.
* Hosting: the portal restarts `BW_HOSTING_RECOVERY_MAX_WORKERS` wikis at once
  (default 2; 1.6 used 4). Set it to 4 in `config/app.env` to keep the 1.6
  speed.
* Hosting: wikis still expire after `INSTANCE_DURATION_DAYS` (14). For
  production wikis turn on **Owners may renew their wikis** (Admin →
  Settings) or set `INSTANCE_DURATION_DAYS=0`.
* Two-step verification is off for everyone. Once your administrators have
  set it up (**Settings → Two-step verification**), an owner or superuser who
  uses it can turn on **Admin → Site settings → Security → Require two-step
  verification for administrators**.

### Updating with a controller from before 1.7

`sudo bananawiki update` runs the controller of the installed release. A 1.6
controller does not check BuildKit, does not run 1.7's preflight and may not
check the wiki storage. It stops the wikis, switches to 1.7, writes its own
older systemd units and waits up to its readiness timeout
(`waves × (2 × INSTANCE_STARTUP_TIMEOUT_SECONDS + 120)` seconds, 360 s for
two wikis) before it rolls everything back.

With BuildKit installed and `BW_HOSTING_TENANT_QUOTAS` unset, `auto` or
`off`, 1.7 starts under such a controller on any storage, without hard
quotas. Then:

1. Run `sudo bananawiki update` again (or `sudo bananawiki restart`). The 1.7
   controller installs its own units, and on XFS with `prjquota` the
   maintenance service restarts each wiki once to put it under quota.
2. If the services do not become ready under those units, the previous units
   are put back, `status` shows `failed_convergence` and automatic updates do
   not retry. Read the services' journal, fix the cause, and run `update` or
   `restart` again.

A release installed by the older controller keeps the dependencies it
installed from `requirements.txt`, without hash checks, until the next
release.

For production, and always with `BW_HOSTING_TENANT_QUOTAS=required`, run this
one update with the 1.7 controller from a checkout instead. It checks Docker,
BuildKit and the storage and runs the preflight before it stops anything:

```sh
git clone --branch BRANCH_OR_TAG SOURCE_URL /root/bananawiki-update   # a 1.7 checkout of your update source
sudo /root/bananawiki-update/banana --root /opt/bananawiki preflight   # one line of JSON, "ok": true
sudo /root/bananawiki-update/banana --root /opt/bananawiki update
```

It deploys what the installed configuration selects (`source show`), not the
checkout, so switch the update source first (step 1 of
[Before you update](#before-you-update)). Later updates use the installed
controller.

If an older controller already tried and rolled back:

* The commit is recorded in `config/failed-revision.json`. Automatic runs
  skip it but try every newer commit, stopping the wikis each time. Keep
  automatic updates off (`sudo bananawiki updates disable`) until an update
  succeeds. A manual `update` retries the failed commit at once, so fix the
  cause first.
* With large wikis, raise `INSTANCE_STARTUP_TIMEOUT_SECONDS` (30-600) in
  `config/app.env` first: each wiki migrates its database at its first 1.7
  start, and an older controller rolls the whole platform back when one wiki
  is late.
* The other items of [Before you update](#before-you-update) still apply.

### Wiki storage and hard quotas

XFS is not required. With the wiki storage (`INSTANCES_DIR`, by default
`/opt/bananawiki/data/instances`) on XFS mounted with `prjquota`, the kernel
also holds every wiki to its plan, capped at `HOSTING_AGENT_MAX_STORAGE_BYTES`
(10 GiB) and `HOSTING_AGENT_MAX_INODES` files, so no wiki can fill the disk.
Without hard quotas the application checks each wiki's uploads and the
portal suspends a wiki above 110% of its plan every 5 minutes; that is enough
where every wiki belongs to the same people. Do not combine it with
`HOSTING_ALLOW_TENANT_PLUGINS=1`. The `auto`, `required` and `off` modes of
`BW_HOSTING_TENANT_QUOTAS` are described in
[hard storage quotas](docs/operations.md#hard-storage-quotas-hosting).

```sh
findmnt -no SOURCE,FSTYPE,OPTIONS --target /opt/bananawiki/data/instances   # xfs ... prjquota
sudo bananawiki status                           # tenant_storage
sudo bananawiki config get tenant-quotas
sudo bananawiki config set tenant-quotas auto    # or required, off
```

#### Moving the wiki storage to XFS on an attached block volume

Project quotas are enabled at mount time, so the root filesystem usually
cannot provide them. Put the whole installation directory on a dedicated XFS
filesystem: the managed restore renames `data/`, so mount the installation
directory itself, not `data/` or `data/instances/`. A block volume, a spare
disk or an unused partition all work.

1. Check the space used with `sudo du -sh /opt/bananawiki`. Provide a device
   of at least twice that size plus room to grow (update snapshots are staged
   on it). If your provider attaches a block volume, do not let it mount the
   volume automatically.
2. Find the device with `lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS` (below
   `DEVICE`, such as `/dev/sdb`). Unless it already holds an empty XFS
   filesystem, run `sudo mkfs.xfs DEVICE`. This erases the device: check the
   name twice. Note its UUID with `sudo blkid -s UUID -o value DEVICE`.
3. Mount it and check:
   `sudo mkdir -m 700 /mnt/bananawiki-xfs && sudo mount -o prjquota DEVICE /mnt/bananawiki-xfs`,
   then `findmnt -no FSTYPE,OPTIONS /mnt/bananawiki-xfs` shows `xfs` and
   `prjquota`.
4. Stop the platform: `sudo bananawiki updates disable`,
   `sudo bananawiki backup` (keep a copy of the package off the server), then
   `sudo bananawiki stop`.
5. Copy everything: `sudo rsync -aHAX --numeric-ids /opt/bananawiki/ /mnt/bananawiki-xfs/`.
6. Swap: `sudo umount /mnt/bananawiki-xfs`,
   `sudo mv /opt/bananawiki /opt/bananawiki.before-xfs`,
   `sudo mkdir /opt/bananawiki`, then add
   `UUID=UUID /opt/bananawiki xfs defaults,prjquota,nofail 0 0` to
   `/etc/fstab`, run `sudo systemctl daemon-reload && sudo mount /opt/bananawiki`
   and check `findmnt -no FSTYPE,OPTIONS /opt/bananawiki` shows `prjquota`
   (a `remount` cannot add it).
7. `sudo bananawiki config get tenant-quotas` shows `hard_quotas: true` (if
   the agent still runs under an older controller's units, run
   `sudo bananawiki restart` first). Optionally run
   `sudo bananawiki config set tenant-quotas required`. Then
   `sudo bananawiki start`. Each wiki is put under quota as it starts; raise
   `HOSTING_AGENT_MAX_INODES` first for wikis with more than 200,000 files.
8. Re-enable automatic updates if you use them, and delete
   `/opt/bananawiki.before-xfs` once everything works.

To undo the move while `/opt/bananawiki.before-xfs` still exists:

1. `sudo bananawiki stop`, `sudo umount /opt/bananawiki`, and remove the line
   from `/etc/fstab`.
2. `sudo rmdir /opt/bananawiki && sudo mv /opt/bananawiki.before-xfs /opt/bananawiki`,
   then `sudo systemctl daemon-reload` and `sudo bananawiki start`.
3. The wikis start without hard quotas and the platform warns that they are
   no longer available. Run `sudo bananawiki config set tenant-quotas off` to
   turn the warning into a notice.
4. Re-enable automatic updates (`sudo bananawiki updates enable` with your
   usual options).

Changes made to the wikis after step 5 stay on the XFS filesystem.

A custom `INSTANCES_DIR` outside the installation also needs a reviewed
`ReadWritePaths` override for the agent unit (see
[docs/hosting.md](docs/hosting.md)).

### What changes in the databases

Each migration runs once, in its own transaction. If one fails, nothing is
changed, the application does not start and the log names the problem.

* Wiki database, schema 4 (1.6.0) to 11. Schema 6 grants `page.view_all` and
  `category.view_all` to every saved permission set, because both are now
  enforced. The other steps add tables and columns that start empty or off.
* Hosting database, version 3 to 7. A new wiki stays "Creating" until
  provisioning finishes; a failed creation is never started, so terminate it
  and create it again. A stored denied-account deletion delay of 0 becomes
  one day; to delete denied accounts at once, save 0 days, 0 hours and 0
  minutes again in **Admin → Settings → Approval**.

### Managed servers

* `updates enable` refuses without `--require-signatures ALLOWED_SIGNERS`
  unless `--allow-unsigned` is given. Automatic updates enabled before 1.7
  keep running, and `status` warns while they deploy unsigned revisions. Let
  only maintainers create `v*` tags on the update source.
* Releases install `requirements.lock` and `requirements-build.lock` with
  `--require-hashes --only-binary=:all:`. A release without both locks is
  refused unless you pass `--allow-unpinned` to `update`, `restore`,
  `rollback` or `backups restore`.
* A restore deploys only a release this server prepared or can build from its
  update source. Otherwise confirm the revision with
  `--trust-source REVISION`; scripts that move servers must pass it. With
  signed revisions required, run `source set --clear-signatures` first to
  restore an old unsigned package.
* After restoring a package without `config/repo.allowed_signers`, recover
  the trust file and run `source set --require-signatures FILE` before
  updating.
* Updates prune old `before-update-*` packages, releases and tenant images
  (keep count: `updates enable --keep-backups`).

### Accounts, content and plugins

* Over HTTPS the session cookie is named `__Host-<name>` (for example
  `__Host-bw_session`). Sessions move to it on their own; tools that read
  the cookie by name must use the new name.
* Two-step verification is off for every account. An owner or superuser
  resets it for a user who lost their codes in **Admin → Users**, or run
  `bananawiki reset-two-step NAME` (with `sudo` on a managed server; the
  portal's "Reset admin password" on a hosting platform).
* Only owners and superusers change administrators, export or import the
  whole site, and switch off the audit log. A site import turns off two-step
  verification for accounts set up under another secret key.
* The daily automatic sign-out (**Admin → Sessions**) no longer signs out
  administrators; use **Sign everyone out** as an owner or superuser.
* Third-party plugins are off by default, and only the owner and superusers
  install, update or enable one. An installation that already has plugins
  keeps them while `BW_ALLOW_EXTERNAL_PLUGINS` is unset and logs a warning at
  every start: set it to `1` to keep them, or `0` to turn them off.
* Page style attributes with negative margins, `vw`/`vh`, `calc()`, `var()`,
  `!important`, or lengths above 4000px, 100% or 250em are dropped when
  rendered.
* A heading called Main, Sidebar or Attachments gets the anchor `#main_1`,
  `#sidebar_1` or `#attachments_1`.
* A wiki that sets `BW_TTS_REMOTE_GPU_AUTH_TOKEN` in the environment must set
  `BW_TTS_REMOTE_GPU_URL` there too. Hosting platforms give each wiki its own
  GPU speech token, which needs the 1.7 GPU speech server: upgrade it first,
  or set `HOSTING_TTS_GPU_TENANT_TOKENS=0` until you do.
* A response is abandoned once a write to the client has waited
  `BW_WRITE_TIMEOUT` seconds (`HOSTING_WRITE_TIMEOUT` for the portal; default
  300, `0` waits forever). Raise it to serve very slow links.

### REST API changes

Scripts may need changes for these (see [docs/api.md](docs/api.md)):

* Changing your own password through `PUT /users/<id>` needs
  `current_password`. New passwords may be refused with `password_common` or
  `password_personal`.
* POSTs to `/admin/bulk/*` require `confirm_text=DELETE` (or the
  interface-language word, `ELIMINA` in Italian).
* Tokens created through `POST /tokens` are revoked with the token that
  issued them and cannot request the `tokens` or `userbot` scope.
* `POST /api/preview` and `POST /api/code/highlight` answer 403 below the
  editor role.
* `DELETE /api/v1/categories/<id>?page_action=delete` needs the `pages` write
  scope. Token expiries are at most 10 years ahead.
* JSON with non-finite numbers, unpaired surrogates or more than 64 nesting
  levels is refused (400). `Idempotency-Key` replays may answer 409
  `idempotency_replay_unavailable`.

### Hosting platform changes

* A few wikis that do not start under a new release (at most one in ten) are
  stopped and reported as `isolated_tenants` while the rest keep it; they
  stay stopped until started from the portal. With
  `HOSTING_ALLOW_TENANT_PLUGINS=1`, a wiki whose own plugins fail is first
  retried with them quarantined (`quarantined_tenants`). Set
  `BANANA_UPDATE_UNREADY_WIKIS=rollback` to roll back instead. See
  [operations](docs/operations.md#managed-servers).
* Under hard quotas, snapshots staged on the wikis' XFS volume must leave
  every wiki's unused limit plus `HOSTING_AGENT_STORAGE_RESERVE_BYTES` free.
  Put `backups/` on another filesystem if that volume is tight.
* Storage suspensions end by themselves once a wiki fits within its limit.
  Suspensions made by an administrator are never lifted automatically.
* Hosted third-party plugins need `HOSTING_ALLOW_TENANT_PLUGINS=1` (off by
  default, subdomain mode only). Every wiki runs as the portal's service UID;
  see [docs/hosting.md](docs/hosting.md) for what that means for plugins.
* Unapproved self-service accounts younger than `HOSTING_NEW_ACCOUNT_DAYS`
  (7) may hold one wiki. Invite-code sign-ups count as new: approve them, or
  set `HOSTING_NEW_ACCOUNT_DAYS=0`.
* `HOSTING_EMAIL_DAILY_LIMIT` is one rolling 24-hour budget shared by every
  worker, with 20% kept for account recovery. Raise it if you relied on each
  worker counting its own.
* `HOSTING_TERMS_VERSION` is the version date of the terms you publish. Set
  `HOSTING_TERMS_REQUIRED_VERSION` to the same date on the day new terms take
  effect, and every account must accept them before using the portal. Code
  that exempts named gates must add `'terms'`. Your own legal pages go in
  `HOSTING_TERMS_URL` and `HOSTING_PRIVACY_URL` (see
  [docs/hosting.md](docs/hosting.md#help-centre-and-legal-pages)).
* The maintenance service deletes `hosting_events` entries older than
  `HOSTING_EVENT_RETENTION_DAYS` (3650; 0 keeps everything).
* Wiki exports and backups fail for a wiki above its storage limit plus a
  tenth and 64 MiB. Without hard quotas, raise the plan of a wiki whose plan
  was lowered below what it holds before its next backup or download.
* Account merges done before 1.7 may have left the merged account as a
  collaborator of its own wikis. Remove those rows once against the portal
  database (`HOSTING_DATABASE_PATH`):

  ```sh
  sqlite3 hosting.db "DELETE FROM instance_collaborators WHERE (instance_id, account_id) IN (SELECT id, account_id FROM instances);"
  ```

### Container images

Both Dockerfiles use `RUN --mount`, so building needs BuildKit
(`docker buildx version` must answer; install `docker-buildx` or
`docker-buildx-plugin`). Images use Debian 13, install `requirements.lock`
and omit `pip`, `setuptools`, `wheel` and `ensurepip`: add custom
dependencies during the build. Rebuild with
`docker compose build --pull --no-cache`. Data in `/data` is kept.

### Interface

* Many labels and menus changed. Update screenshots and user instructions
  that quote them. URLs, themes and display preferences are kept.
* German is a new built-in language, on in every wiki after the update.
  Switch it off under **Admin → Languages** if you do not want it. An
  uploaded `de` file now overrides the bundled German; **Remove changes**
  deletes it. Read aloud uses German only once it is ticked under
  **Admin → Read aloud**.
* The mascot replaces the top bar logo for signed-in accounts (feature
  `mascot`). Switch it off under **Admin → Plugins**, or add `mascot` to
  `BW_MANAGED_PLUGIN_DENYLIST` (`HOSTING_TENANT_PLUGIN_DENYLIST` on a
  hosting platform). See [docs/features.md](docs/features.md#mascot).

### New and changed settings

All are optional and documented in
[docs/configuration.md](docs/configuration.md); the `BW_` and `HOSTING_` ones
are in `.env.example`.

| Setting | Default | What it does |
|---|---|---|
| `BW_ALLOW_EXTERNAL_PLUGINS` | off; on with a warning while unset if plugins are installed | Third-party plugins |
| `BW_TTS_MAX_JOB_SECONDS` | 3600 | Longest read-aloud job |
| `BW_TTS_PIPER_MEMORY_MB` | 0 (none) | Memory limit of the Piper process (Linux) |
| `BW_LOG_FORMAT`, `HOSTING_LOG_FORMAT` | text | `json` writes one JSON object per line |
| `BW_WRITE_TIMEOUT`, `HOSTING_WRITE_TIMEOUT` | 300 | Seconds a write may wait for a client that stopped reading (0: forever) |
| `BW_HOSTING_RECOVERY_MAX_WORKERS` | 2 | Wikis restarted at once after an update or a backup |
| `BANANA_PACKAGE_MAX_FILES` | 1000000 | Files a managed package may hold (`config/app.env`) |
| `BANANA_UPDATE_UNREADY_WIKIS` | `isolate` | `rollback` to roll back when any wiki fails (`config/app.env`) |
| `BW_HOSTING_TENANT_QUOTAS` | `auto` | Hard per-wiki XFS quotas (see [Wiki storage and hard quotas](#wiki-storage-and-hard-quotas)) |
| `HOSTING_AGENT_MAX_STORAGE_BYTES`, `HOSTING_AGENT_MAX_INODES` | 10 GiB, 100000 | Per-wiki ceilings |
| `HOSTING_AGENT_PROJECT_ID_START` | 1000000 | First XFS project ID |
| `HOSTING_AGENT_STORAGE_RESERVE_BYTES` | 268435456 | Space kept free when admitting wikis and staging snapshots |
| `HOSTING_ALLOW_TENANT_PLUGINS` | off | Hosted third-party plugins (subdomain mode only) |
| `HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED` | on | Accept custom domains proxied by Cloudflare |
| `HOSTING_TTS_GPU_TENANT_TOKENS` | 1 | A derived GPU speech token per wiki |
| `HOSTING_SIGNUPS_PER_IP_PER_DAY` | 10 | Accounts the sign-up form creates per address per day |
| `HOSTING_NEW_ACCOUNT_DAYS` | 7 | How long an unapproved self-service account counts as new |
| `HOSTING_NEW_ACCOUNT_MAX_INSTANCES` | 1 | Wikis a new account may hold |
| `HOSTING_NEW_ACCOUNT_SHARE_PERCENT` | 50 | Share of the platform's wiki limit all new accounts may use |
| `HOSTING_EXPIRY_WARNING_DAYS` | 3,1 | When owners are warned before a wiki expires |
| `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` | 10 | Messages one account may cause per day |
| `HOSTING_EVENT_RETENTION_DAYS` | 3650 | Moderation log retention (0 keeps everything) |
| `HOSTING_SERVICE_NAME` | `BananaWiki Hosting` | The platform's name in its pages, emails and authenticator apps |
| `HOSTING_TERMS_URL`, `HOSTING_PRIVACY_URL` | unset | Your own terms and privacy pages, both or neither |
| `HOSTING_TERMS_URL_<LANG>`, `HOSTING_PRIVACY_URL_<LANG>` | the two above | A pair per interface language other than English (`_IT`, `_DE`) |
| `HOSTING_TERMS_VERSION` | the release's `TERMS_VERSION` | Version date of the terms you publish |
| `HOSTING_TERMS_REQUIRED_VERSION` | unset | Terms version every account must have accepted |
| `INSTANCE_DURATION_DAYS` | 14 | Accepts 0 (no expiry) |
| `TTS_SOCKET_TIMEOUT`, `TTS_MAX_CONNECTIONS` | 30, 64 | GPU speech server (contrib) connection limits |

### Going back to 1.6

1.6 refuses the newer databases, so going back means restoring the data from
before the update. Changes made after the update are lost, and HTTPS users
are signed out once.

* Managed: `sudo bananawiki rollback` restores the package written before the
  update. If the old release is no longer prepared and its revision is not
  available from the update source, confirm it with
  `--trust-source REVISION`. The 1.6 controller ignores `--track tags` and
  follows the branch again; with signatures required it needs signed
  commits, so run `source set --clear-signatures` before updating from 1.6 if
  only the tags are signed.
* Checkout: stop the wiki, check out the commit it ran before (`v1.6.0`) and
  reinstall its `requirements.txt`. Replace `bananawiki.db` with
  `<instance>/backups/pre-upgrade-v<old>-<time>.db`, where `<old>` is the
  schema of that commit (4 for 1.6.0; the log line "Database schema N will be
  upgraded" names it). Take the newest copy with that number: older ones lack
  later changes. Delete `bananawiki.db-wal` and `bananawiki.db-shm`, then
  start the wiki.
* Docker and desktop: restore the backup you took before.
* Encrypted Git backups: run `sudo bananawiki backups disable` before going
  back and `backups enable` after updating again. Otherwise 1.6 uploads
  snapshots that 1.7 then refuses to restore (see
  [operations](docs/operations.md#managed-servers-encrypted-git-backups)).

## Upgrading from 1.4

BananaWiki takes over a 1.4 installation (or any earlier 1.x release at
schema 3) in place: same database, files, secret key, environment variables
and URLs. Accounts, passwords, pages, history, sessions and API tokens
survive. A 1.4 installation upgrades straight to the current release; the
[1.6 to 1.7](#upgrading-from-16-to-17) and later sections apply too.

### Before you start

1. The database must be at schema 3, which every 1.4 release since the public
   release writes:

   ```sh
   sqlite3 /path/to/bananawiki.db 'PRAGMA user_version'     # must print 3
   ```

   If it prints `0`, start the last 1.4 release once to upgrade it to
   schema 3 first.
2. Back up. Managed servers: `sudo bananawiki backup`. Others: stop the wiki
   and copy the instance directory, the upload folders and the secret key
   (`instance/.secret_key` or your `SECRET_KEY`).
3. Keep the secret key. A different key signs everyone out and invalidates
   API tokens and encrypted settings.
4. Read the [behaviour changes](#behaviour-changes), in particular if you use
   the REST API, custom pages with scripts, or third-party plugins.

### Managed servers (`banana`)

```sh
sudo bananawiki source show      # which repository and branch the server follows
sudo bananawiki update
sudo bananawiki update           # a second time: see below
sudo bananawiki status
```

1. If `source show` gives a URL other than
   `https://github.com/BananaSuite/BananaWiki`, switch with
   `sudo bananawiki source set --repo https://github.com/BananaSuite/BananaWiki.git --branch main`.
2. The first `update` is run by the installed 1.4 updater. It fetches the new
   release, writes a `before-update-*.tar.gz` package, switches and waits for
   `/health`. If the new release is not healthy, it restores 1.4.
3. The second `update` (or `sudo bananawiki restart`) runs the new
   controller and installs its systemd units. On a hosting server this also
   installs the runtime agent (`bananawiki-agent.service`), removes the
   portal from the `docker` group, adds `BW_RUNTIME_AGENT_SOCKET` to
   `config/app.env` and routes wikis through Caddy straight to their
   containers. If the readiness checks fail, the old units, `app.env` and
   Caddyfile are put back. On a hosting server the wikis are unreachable
   between the two updates, so run the second one right away. A Caddyfile you
   wrote yourself must contain `import /var/lib/bananawiki-routes/*.caddy`
   (`update` warns until it does; see `bananawiki proxy`).
4. `status` should show `"units_current": true` (and on hosting servers
   `"runtime_agent": true`).

`config/app.env`, the update policy, source credentials and backup settings
are kept. Automatic updates upgrade the server the same way on their next run.

### Installations from a Git checkout

```sh
cd /path/to/BananaWiki
git fetch --tags
git checkout v1.7.2       # the release tag you upgrade to
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock   # inside the wiki's virtual environment
sudo systemctl restart bananawiki
```

The start command is still `gunicorn -c gunicorn.conf.py wsgi:app`, and the
read-aloud worker is still `python scripts/tts_worker.py`.

| 1.4 | Now |
|---|---|
| `python -c 'import config; print(config.SETUP_TOKEN)'` | `bananawiki setup-token` |
| `python reset_password.py …` | `bananawiki reset-password NAME` |
| `./dev.sh` | `bananawiki serve` |
| `./start.sh`, `setup_wizard.py` | Gunicorn directly, or `banana install` (see [docs/deployment.md](docs/deployment.md)) |

`bananawiki` is available after `python -m pip install -e .`; otherwise use
`python -m bananawiki.cli …` from the checkout.

On the first start, for every folder whose variable is not set, the files
move from the source tree into the instance directory. Nothing is
overwritten, a file that cannot be moved is logged, and setting the variable
keeps the old location. Managed servers set every variable, so nothing moves
there.

| Files | From (1.4 default) | To | Variable |
|---|---|---|---|
| Page images, avatars | `app/static/uploads/` | `instance/uploads/` | `BW_UPLOAD_FOLDER` |
| Custom favicons (`custom_*`) | `app/static/favicons/` | `instance/favicons/` | `BW_FAVICON_UPLOAD_FOLDER` |
| Attachments | `instance/attachments/` (source tree) | `<instance>/attachments/` | `BW_ATTACHMENT_FOLDER` |
| Chat files | `instance/chat_attachments/` | `<instance>/chat_attachments/` | `BW_CHAT_ATTACHMENT_FOLDER` |
| Kanban files | `instance/kanban_attachments/` | `<instance>/kanban_attachments/` | `BW_KANBAN_ATTACHMENT_FOLDER` |
| Custom page files | `instance/custom_page_files/` | `<instance>/custom_page_files/` | `BW_CUSTOM_PAGE_FILES_FOLDER` |
| Uploaded interface languages | `translations/*.json` (other than `en`, `it`) | `<instance>/translations/` | none |

The log moves from `logs/bananawiki.log` to `<instance>/logs/bananawiki.log`
unless `BW_LOG_FILE` is set.

### Docker

Use the `Dockerfile` and `compose.yaml` (see
[docs/deployment.md](docs/deployment.md#docker-and-docker-compose)). The
image keeps its data in `/data` (`bananawiki.db`, `.secret_key`, `uploads/`,
`attachments/`, `chat_attachments/`, `kanban_attachments/`,
`custom_page_files/`, `tts/`). To move a 1.4 wiki into it, copy its files
into the volume in that layout (owned by UID 10001), or export it from the 1.4 **Admin → Site
migration** page and import the archive in the new wiki, which replaces the
new wiki's accounts. Wikis on a 1.4 hosting platform are upgraded with the
platform.

### Desktop app

Install BananaWiki Desktop and point it at the folder the 1.4 Easy
Deployment App used (the `bananawiki/` folder next to the old program). A
ZIP made with **Export All** in the old app can be restored with
**Restore…**. Third-party plugins do not load until you allow them (see
[Changes to check in 1.7.1](#changes-to-check-in-171)). See
[docs/desktop.md](docs/desktop.md).

### What happens on the first start

The database is copied to `<instance>/backups/pre-upgrade-v3-<date>-<time>.db`
(mode 0600), the data folders move as described above, and the migrations
run, each in its own transaction. Migration 3 to 4 clears references to
missing users, rewrites timestamps as UTC, and erases the secrets of retired
features (Telegram bot, feedback bot). If a migration fails, nothing is
changed and the wiki does not start; the log names the problem.

Tables of plugins that are no longer shipped are left untouched;
`bananawiki db prune-retired` drops them (back up first).

### Going back to 1.4

1.4 refuses a database at schema 4 or later, so going back means restoring
the data from before the upgrade. Changes made after the upgrade are lost.

* Managed: `sudo bananawiki rollback` restores the package the 1.4 updater
  wrote, with the 1.4 units. Run `sudo bananawiki backup` first to keep the
  current state. If the 1.4 release cannot be rebuilt, confirm its revision
  with `--trust-source REVISION`; while signed revisions are required, run
  `source set --clear-signatures` first.
* Checkout: stop the wiki, `git checkout` the 1.4 commit and reinstall its
  `requirements.txt`, replace `bananawiki.db` with
  `<instance>/backups/pre-upgrade-v3-*.db`, delete `bananawiki.db-wal` and
  `bananawiki.db-shm`, move the relocated folders back (or set the `BW_*`
  folder variables to their new locations), then start 1.4.
* Docker and desktop: restore the backup you took before.

### Behaviour changes

#### Accounts, sessions and administration

* Cookies without a server-side session (1.4 issued them to suspended users
  and through `/admin` and `/session-conflict/force`) are signed out.
* Without **remember me** the cookie ends when the browser closes.
* Only owners and superusers demote, suspend, delete or reset the password of
  administrators. The account made at `/setup` is an owner and a superuser;
  on an upgraded wiki, make sure a trusted person is one.
* Suspensions recorded before the upgrade are lifted by an owner or
  superuser.
* Sign-up approval is enforced: pending and denied accounts cannot use the
  wiki. Check **Admin → Users** for pending accounts if approval was on.
* Sign-in limits: 20 failures per address and 8 per account name in 15
  minutes.
* The setup token is accepted only in the setup form, not in the URL.
* 1.4 did not reserve former user names, so several accounts may have given
  up the same name. Releasing it from the latest one passes it to the one
  before, and the administrator is told so.

#### Permissions

* Every permission in the catalogue is enforced. Editors whose permissions
  or custom role lack `page.create`, `page.edit_all`, `page.edit_metadata`,
  `history.revert` or `category.*` lose those actions. Review **Admin →
  Custom roles** and individual permissions after the upgrade.
* Category actions need write access to the categories involved, and the page
  builder needs the right to edit the page.
* Deleting uploaded images (`/api/upload/delete`) is for administrators only.
* `page.view_all` and `category.view_all` are enforced and granted once to
  every saved permission set. To list accounts that would read nothing
  after you remove them:

  ```sql
  SELECT username FROM users u WHERE role IN ('user', 'editor')
    AND custom_role_id IS NULL
    AND EXISTS (SELECT 1 FROM user_category_access a WHERE a.user_id = u.id)
    AND NOT EXISTS (SELECT 1 FROM user_permissions p
                    WHERE p.user_id = u.id AND p.permission_key = 'page.view_all');
  ```

#### REST API

* Error bodies are `{"ok": false, "error": "…", "code": "…"}`; timestamps are
  ISO 8601 UTC (`2025-01-31T09:30:00Z`).
* Tokens are revoked when their account's password changes or is reset, and
  when the account is suspended.
* Deleting a category needs `category.delete`; changing a page's title or
  category needs `page.edit_metadata`; userbot profile updates need
  `profile.edit_own`.
* The rate limit counts all tokens of an account together.
* Maintenance mode answers 503, and accounts with a pending password change
  or onboarding get 403.
* Banana Mode and its endpoints (`/api/v1/banana-mode`, `/admin/banana…`) are
  gone; use maintenance mode.
* New: history, search, single category, `expected_revision`, paging,
  `/api/v1/openapi.json`. See [docs/api.md](docs/api.md).

#### Features

* Switching a feature off stops its background jobs and keeps its settings.
* Events such as `page.deleted` fire on every path (bulk deletions, purges,
  temporary items, federation forks), so plugins that listen see more events.
* Deleting a chat message erases its text and attachments for everyone.
* Custom pages with HTML, CSS or JavaScript run in a sandboxed document:
  their scripts cannot read the wiki's cookies or call its APIs as the
  visitor. On hosts that forbid public wikis they are for signed-in members.
* Whole-site import replaces everything, accounts included, and never writes
  code, configuration or the secret key. Under managed hosting it needs
  `BW_ALLOW_SITE_IMPORT=1`.
* Read aloud needs an account and reads pages only in enabled languages; the
  GPU server needs a token of at least 16 characters. Install
  `langdetect==1.0.9` to detect languages other than English and Italian.
  Existing read-aloud audio is kept.
* Page builder documents are saved as version 2, which 1.4 cannot open.
* Contribution approval has its own setting (`contribution_approval_enabled`,
  kept on where 1.4 had it on).
* Still images are re-encoded on upload, which removes EXIF and GPS data.
* Third-party plugins load only at start-up. 1.4 plugins run through an
  adapter; its limits are in
  [docs/plugins/README.md](docs/plugins/README.md#plugins-written-for-bananawiki-14).
* Notification email stays off until an administrator enables it in
  **Admin → Notifications**.

#### Configuration and operations

* Default `BW_LOGGING_LEVEL` is `medium`.
* No longer read: `BW_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES`,
  `BW_SITE_EXPORT_COMPRESS_LEVEL`, `BW_SITE_EXPORT_STORE_FILE_BYTES`,
  `BW_SITE_EXPORT_STORE_EXTENSIONS`, `BW_DB_OBSERVABILITY`,
  `BANANAWIKI_SKIP_BACKGROUND_SERVICES` (use `BW_BACKGROUND_JOBS=0`),
  `HOSTING_DEBUG`.
* New: `BW_WORKERS`, `BW_THREADS`, `BW_WORKER_TIMEOUT`, `BW_ACCESS_LOG`,
  `BW_SECURE_COOKIES`, `BW_BACKGROUND_JOBS`, `BW_ALLOW_SITE_IMPORT`, and the
  `BW_MAIL_*`, `BW_SMTP_*` and `BW_BASE_URL` mail settings, which override
  the administrator's. See [docs/configuration.md](docs/configuration.md).
* Hosting: approval emails come from the maintenance service. The 1.4 digest
  interval in hours becomes `approval_notify_interval_minutes`.
* Hosting: the portal has no Docker access (the runtime agent does that
  work), and `HOSTING_CONTAINER_IMAGE` defaults to
  `bananawiki-tenant:latest`.
* Hosting: a terminated wiki whose download was blocked in 1.4 is kept until
  an administrator resumes its deletion countdown.
* Hosting: every update rebuilds the tenant image on a freshly pulled base
  image, so the Docker registry, the Debian mirror and PyPI must be
  reachable. If only the base image pull fails, the cached base is used and
  `update` reports `image_warnings`; if the mirror or PyPI fails, the commit
  is recorded as failed until `update --retry-failed`. When the pulled base
  image changed, the FFmpeg and ACL stages are rebuilt too, which also needs
  `ffmpeg.org`, the Debian source archive and `snapshot.debian.org` (the ACL
  stage installs `libacl1` 2.4.0-1 and `tar` 1.35+dfsg-6 from a fixed
  snapshot of Debian unstable). If that rebuild fails, the build falls back
  to the previous base image, still with fresh Debian and Python packages,
  and `update` reports `image_warnings`. Only a build that fails on the
  previous base image too records the commit as failed.
* Hosting: `recover --abandon` drops an operation that cannot finish.
* Hosting runs only on Linux x86_64.

#### Removed

* Obsidian vault sync (`scripts/obsidian_sync.py`); use the REST API.
* Banana Mode.
* Joke audio conversion of chat and kanban uploads (`.mp5`/`.mp7`).
* The BananaChat installation modes of the `banana` controller (only `wiki`
  and `hosting` remain) and the shared-file manifests with BananaChat.
* `setup_wizard.py`, `dev.sh`, `start.sh`, `reset_password.py`.
* The `legacy_redirect/` daemon; use `deploy/Caddyfile.legacy-redirect`.
* The hosting portal's Arabic mirror (right-to-left) mode.
* Demo wiki spawning on the hosting portal.
* The `api_tokens` and `userbot_api_tokens` tables are no longer used (drop
  them with `bananawiki db prune-retired`).

The eight plugins retired in late 1.4 (`banana_ai`, `bw_oauth_provider`,
`file_manager`, `git_override`, `oauth_login`, `feedback`, `meetings`,
`beta_testers`) and other 1.4-only plugins are listed as retired on the
plugin page; nothing under those ids loads.
