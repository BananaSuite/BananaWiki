# Blockers

Decisions and actions that only the owner can take before 1.7.0 is released.
The safest default is in place in each case. State on 11 October 2026; the
findings behind them are in [AUDIT.md](AUDIT.md), the checks in
[READINESS.md](READINESS.md).

## Publish the code (AUDIT #81, #86)

* 1.7.0 is pushed as a single commit on the `release` branch, on top of
  `main` (`519ac7d`, which already holds pull requests #21 to #25), with a
  pull request. Merge it once CI passes ("Before tagging" below): servers
  that installed `519ac7d` or an earlier commit of `main` then see an
  ordinary update.
* The release commit is not signed. Managed servers that follow release
  tags accept only an annotated tag signed by an allowed signer for its own
  name (`git tag -s vX.Y.Z`); a signed commit under an unsigned or
  lightweight tag releases nothing. Servers that follow a branch with
  signatures required will not take an unsigned commit: sign the tag and
  move them to `--track tags`.
* Protect `v*` tags on the forge so that only maintainers can create them:
  anyone who can push tags can hold back updates of signature-checking
  servers by pushing 20 tags that do not verify above the release (servers
  try only the 20 highest and say which they skipped), as they could by
  deleting the release tags (AUDIT #112).

## Release tags and signing (AUDIT #82)

* New installations follow release tags. Until a `v1.7.0` tag exists, they
  report that there is nothing to update to, and the README and
  docs/deployment.md tell people to `git checkout v1.7.0`.
* When tagging: replace "1.7.0 (unreleased)" in CHANGELOG.md with the date;
  if `requirements.txt` changed since, run `python scripts/lock_requirements.py`
  and `tests/test_requirements_lock.py`; then `git config gpg.format ssh` and
  `git tag -s v1.7.0 -m "BananaWiki 1.7.0"` (BananaChat likewise).
* Publish the maintainers' allowed-signers lines (`email ssh-ed25519 AAAA…`)
  somewhere operators can verify through a second channel.
  docs/operations.md ("Signed releases") tells operators to pass them to
  `--require-signatures`; it does not yet say where to find them.

## Hosting terms and privacy (AUDIT #83, #109)

* **Terms version.** The site's terms and privacy pages say "Version
  2026-10-09"; the code's default `TERMS_VERSION` is "2026-07-22". Set
  `HOSTING_TERMS_VERSION=2026-10-09` in the portal's `config/app.env` so
  sign-ups record the version the pages show.
* **Publishing the site**: bananawiki.com is served by GitHub Pages from
  the site repository's `main`. Its `release` branch has this release's text
  (moderation-log retention, storage suspensions that end by themselves,
  acceptance of new terms), the Italian pages and new screenshots, and
  removes `img/bananachat-claude.png`; merging its pull request publishes
  all of it. All sixteen product screenshots were recaptured from the 1.7.0
  working trees on 10 October 2026 (every footer reads 1.7.0); the kanban
  image again after its Done column toggle became a check-mark button, and
  the nine BananaChat images again after BananaChat moved to British
  spelling (its menu says Customise, as BananaWiki's does). All sixteen
  were captured again on 11 October 2026 from the final trees, so the apps'
  top bars show the pixel-art banana; the wiki images show the mascot
  standing still, as a new wiki does (the site's AUDIT.md #36; whether the
  pictures may show it is a decision in the site's BLOCKERS.md).

## Licences and attribution

* The common-password list (`bananawiki/core/data/common-passwords.txt`) is
  the first 10,000 entries of at least 8 characters, lower-cased, of SecLists'
  `100k-most-used-passwords-NCSC.txt` (MIT, Copyright (c) 2018 Daniel
  Miessler; data from the UK NCSC's PwnedPasswordsTop100k, derived from Have
  I Been Pwned). Attribution is in NOTICE and the MIT text next to the list.
  Confirm you are happy to ship it.
* Credits: NOTICE and both READMEs now read "Luca Zani and contributors"
  (started by Luca Zani; maintained by Luca Zani and contributors), and only
  the website's about page tells where the project started. No sponsor or
  organisation is named in the 1.7.0 tree, and
  `tests/test_project_names.py` keeps it that way. The commits already
  published (NOTICE, the READMEs and `docs/1.4-review.md` in `origin/main`)
  still name them: removing them from the history means rewriting published
  commits, which is your decision. Default: the history is left as it is.

## Before tagging: the hosting runtime on a real Docker (AUDIT, production incident)

Nothing in this environment runs Docker, so the 1.7 runtime agent's own
command lines (custom seccomp profile, IPv6 sysctls, `bwt…` bridges with
`--ipv6=false`, the mount gate and its release, the mount comparison,
one-shot task containers) have only met simulated Docker. CI's `images` job
now has a step that starts two wikis through the agent (a published port and
an isolated network), runs a task in each state and stops them against the
runner's Docker ("Start wikis through the runtime agent, run tasks, stop
them"). It was checked against the simulated Docker only: let it run once
before tagging, and treat a failure there as a release blocker. The same job's
image builds confirm that the Dockerfiles build without their former
`# syntax=` line (built-in BuildKit frontend). Also run
`sudo ./banana --root /opt/bananawiki preflight` from the release checkout on
your own hosting server before updating it.

## Hosting isolation: one UID for every wiki (AUDIT #77, #76)

Every tenant container runs as the portal's service UID. A container escape
would expose every wiki and the portal's secrets. Per-tenant UIDs,
userns-remap or rootless Docker need the portal's tenant file operations
moved into the runtime agent: a redesign, not a fix. docs/hosting.md ("One UID
for every wiki") documents the threat model and the mitigations. Decide
whether 1.7 hosting is offered to untrusted tenants under this model
(defaults: tenant plugins off, the nftables ruleset shipped but loaded by the
operator), and whether the redesign is planned for a later release. The same
work would allow per-wiki online backups instead of the platform-wide stop,
a narrower start lock in the agent, and restores that isolate a broken wiki
instead of rolling everything back.

## Your own hosting platform when you deploy 1.7

These are documented in UPGRADING.md for every operator; on the platform you
run, they are your decisions:

* load the host firewall with `sudo bananawiki firewall | sudo nft -f -` and
  persist a saved copy of that output (not the shipped
  `deploy/nftables/bananawiki-tenants.nft`, whose DNS resolver set is empty);
* wikis expire after 14 days by default and owners cannot renew unless you
  turn on **Owners may renew their wikis** (or set
  `INSTANCE_DURATION_DAYS=0`);
* wikis currently suspended for storage reopen at the first maintenance pass
  if they fit; suspend again, with your own reason, any that must stay
  closed;
* the portal restarts 2 wikis at a time unless
  `BW_HOSTING_RECOVERY_MAX_WORKERS=4`;
* a stored denied-account deletion delay of 0 becomes one day (hosting schema
  7); save 0/0/0 again in **Admin → Settings → Approval** if you really want
  immediate deletion;
* switch existing managed servers to `--track tags --require-signatures`
  once `v1.7.0` is tagged;
* a server whose installed controller is from before 1.7 (on ext4 storage
  or without Docker's BuildKit component in particular): run that one update
  with the 1.7 controller from a checkout (UPGRADING.md, "Updating with a
  controller from before 1.7");
* hard storage quotas: every platform, new ones included, runs under
  `BW_HOSTING_TENANT_QUOTAS=auto` (XFS is optional, see "Product decisions");
  on XFS with `prjquota`, set `required` if the wikis must be refused rather
  than ever run without hard quotas; elsewhere decide whether the
  application's limits are enough, and do not enable tenant plugins without
  hard quotas;
* without hard quotas, wikis without a plan (apex, administrators') are held
  to `HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB) by their upload check: check
  their size before updating.

## Product decisions (current default in place)

* **XFS is optional (decided by the owner).** No installation or update
  requires XFS. `BW_HOSTING_TENANT_QUOTAS=auto`, the default for every
  platform including new installations, uses hard per-wiki quotas where the
  wiki storage enforces them (XFS mounted with `prjquota`); elsewhere each
  wiki is held to its plan by the application: its upload checks, the
  portal's storage scan every 5 minutes (suspension above 110% of the plan)
  and, for a wiki without a plan, `HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB).
  `auto` is not sticky: when hard quotas were enforced on the platform
  before (its quota registry exists) and the storage no longer enforces
  them, the wikis still start, and the agent's log, `status` and the
  administration page warn loudly. To have the wikis refused instead, opt
  into `required`: `sudo bananawiki config set tenant-quotas required`
  (accepted while the storage enforces quotas) or
  `install --tenant-quotas required`. The security trade-off: without hard
  quotas a wiki can exceed its plan between two 5-minute scans, and in that
  time fill the disk and stop every other wiki on the volume, which matters
  on a platform open to strangers; tenant plugins
  (`HOSTING_ALLOW_TENANT_PLUGINS`) should stay off without hard quotas,
  because their code writes at disk speed (`status` warns). Default: `auto`.
* **Chat reports keep their copies.** `chat__reports` keeps the reported
  message's text with no retention or purge, so administrators can act on a
  report after the message is gone. Decide on a retention (and say it in the
  wiki's own privacy notice if you publish one). Default: kept, also after
  the report is resolved.
* **Members can block administrators.** A member may block a site
  administrator like anyone else: the administrator then cannot start a
  conversation with them, and messages in an existing one are withheld.
  Moderation views hide nothing from administrators. Decide whether
  administrators should be unblockable. Default: anyone can be blocked.
* **Deleting an account removes its direct conversations for both
  participants** (the chat tables cascade), so a sender can still erase a
  message older than the 15-minute delete window by deleting their account;
  a message the recipient reported keeps its copy in the report. Keeping the
  recipient's copy with the sender anonymised needs a schema change (the
  `chats` and `chat_messages` foreign keys). Default: documented exception
  (AUDIT #105).
* **Chat blocks can be guessed** by a blocked member who sees the other
  person active (posting in a group they share) while a new conversation
  with them, or adding them to another group, is answered as for an inactive
  account. An existing conversation and a current membership answer as for
  anyone. Default: documented (AUDIT #119).
* **Chat exports** keep blocked members' group messages (an export is a
  complete record); a direct message withheld because of a block is in its
  sender's and administrators' exports only. Reports a member filed are not
  in that member's data export. The administrators' token page does not show
  which token issued a token (the owner's own page does).
* **API tokens and two-step verification.** Turning two-step verification on
  does not revoke the account's existing API tokens, and the administrator
  requirement does not apply to tokens (docs/security.md). Decide whether
  enabling it should offer to revoke them, as BananaChat now does. Default:
  tokens survive.
* **Hosting account recovery by email** can still be paused for a day by
  someone who knows about ten user names with their verified addresses and
  has a few networks. The forms then say recovery is paused, to everyone
  alike, and administrators see it in `/admin/attention`; reminders, which
  need only an address, can no longer cause it alone (AUDIT #96). Default:
  documented; raise `HOSTING_EMAIL_DAILY_LIMIT` if it happens. A possible
  follow-up: let a browser holding a portal device cookie for the account
  reset its password even then.
* **Unconfirmed addresses in the portal.** An attacker who keeps creating
  new accounts can hold someone's not yet confirmed address for a few days a
  month (AUDIT #95). The complete fix is uniqueness among verified addresses
  only (a partial unique index, other holders cleared on confirmation),
  which changes how the portal looks up accounts by address. Default:
  per-account series of address changes; an administrator can clear a
  squatter's address.
* **Remote backups after a server compromise.** Retention counts only
  snapshots the series' recovery key authenticated, so branches pushed with
  a stolen upload token no longer make it delete genuine snapshots. Someone
  who holds the recovery key (a full server compromise) can still push
  authenticated snapshots dated now; after a suspected compromise, start a
  new series with a new key. Default: documented (AUDIT #97).
* **Old pre-upgrade copies in hosted wikis.** Copies that 1.4 to 1.6 upgrades
  wrote in a hosted wiki's own `backups/` folder count against its storage,
  and its owner cannot delete them from the wiki (AUDIT #99). Default: left
  in place (they are the owner's data); a maintenance step or administrator
  action could list or remove them.

* **Hosting without the runtime agent.** A portal with no
  `BW_RUNTIME_AGENT_SOCKET` (a release the 1.4 updater installed, until its
  units converge) can never use hard quotas; under `auto` it starts wikis
  without them, as 1.6 did, instead of refusing (which would roll back every
  first update by the 1.4 updater). Default: kept and documented
  (RUNTIME_AGENT.md); set `required` to refuse.
* **The portal footer names the software.** `HOSTING_SERVICE_NAME` renames
  the platform in page titles, the top bar, emails, the maintenance page,
  wikis' status pages and authenticator apps, but the footer keeps
  "BananaWiki Hosting <version>" next to the source-code link, as the
  software's identity. Decide whether operators' own name should replace it
  there (the `hosting.footer` string in each language). Default: the
  software's name.
* **Cloudflare's client addresses are believed on every installation.**
  The generated Caddy configuration takes `CF-Connecting-IP` from
  connections out of Cloudflare's published ranges, also on a server that
  is not behind Cloudflare, and anyone with a Cloudflare account can open
  such connections and write the header (docs/deployment.md, "Not behind
  Cloudflare?"). Nothing is granted to an address, but the limits per
  address are best effort. Turning it off needs a new `proxy` option that
  also reaches the runtime agent's per-wiki routes; tying it to
  `--cloudflare` would break visitors' addresses for platforms proxied by
  Cloudflare without that flag. Default: believed everywhere, documented.
* **The mascot is on by default** (a built-in plugin, pull request #21):
  servers updating from 1.6 show it to every signed-in account.
  Administrators can hide it for everyone (**Admin → Appearance**) or switch
  it off (**Admin → Plugins**). The Italian and German toasts keep the
  English phrase "Deal with it." (the contributor's choice). Default: as
  merged.
* **The mascot is a 44-pixel target on touch screens** (pull request #25),
  the size platform guidance asks for; the 32-pixel sprite alone already
  meets WCAG 2.2 AA (24 pixels). On a 375-pixel phone the site name then
  starts 16 pixels later than with a mouse, so a long name such as "Banana
  Labs Wiki" is cut on touch phones (it fits with a mouse, and beside the
  logo). The alternative keeps 32 pixels in the layout and lets an invisible
  hit area reach further. Default: as merged (44 pixels).
* **The banana icon on dark backgrounds.** The icon's outline (`#1a1408`)
  all but disappears on near-black backgrounds (the website's header, the
  dark top bars of the wiki, the portal and BananaChat, dark browser tabs):
  the yellow body stays clearly visible, but the stem reads as a separate
  dot. A light one-pixel halo for the static icons alone would set them
  apart from the moving mascot in the same top bar. Default: no halo, the
  same drawing everywhere.
* **No 24-pixel frame in the Windows icon.** `packaging/desktop/bananawiki.ico`
  holds 16, 32, 48, 64, 128 and 256 pixels, whole multiples of the 16-pixel
  art; the earlier icon also had 24. Windows at 100% scaling uses 24 pixels
  in the taskbar and scales the 32-pixel frame down to it, which softens the
  pixel art. A 24-pixel frame could only hold the art at its own size with a
  4-pixel margin: sharp, but smaller. The launcher's window gets 16, 32, 48
  and 256 pixels, so window managers no longer scale the 256-pixel image
  themselves. Default: no 24-pixel frame.
* **German is on in every wiki after the update** (pull request #22):
  existing wikis offer it in their language menus, and signed-out visitors
  whose browser prefers German get it; administrators switch it off under
  **Admin → Languages**. The German texts address the reader informally
  (*du*), the contributor's choice. Read aloud keeps its own language list
  (English and Italian unless changed), so German pages are read with those
  voices until an administrator ticks German. Default: as merged.
* **Updates quarantine plugins only when every failing wiki holds some**
  (pull request #23): one failing wiki without plugins points at the release,
  so nothing is quarantined and the failing wikis are isolated or the update
  rolled back. Quarantining the wikis that hold plugins and isolating the
  rest would keep more wikis serving. Default: as merged.
* **The portal's own proxy cuts a wiki's response after 300 seconds.** When
  a wiki has no route of its own, the portal proxies it and ends every
  response after 300 seconds in total, also a large download on a slow link
  that keeps making progress (the Gunicorn write timeout of pull request
  #23 only cuts clients that stop reading). A rule on progress instead of
  total time needs a design decision. Default: 300 seconds.
* **Archive budgets without hard quotas.** Platform backups, downloads and
  plugin snapshots hold each wiki to its plan plus a tenth and 64 MiB, or,
  under hard quotas, to the limit the runtime agent adopted it at above its
  plan. Without hard quotas nothing bounds what a wiki holds, so a wiki
  whose plan was lowered below its data keeps the plan as its budget: its
  database or files can then be left out of platform backups and its
  download refused. Default: raise such a plan back (UPGRADING.md).

## Not done in 1.7 (by choice)

* **Bare URLs are not turned into links** in Markdown; `<https://…>` and
  `[text](url)` links work. Autolinking would be a new Markdown rule (with
  exclusions for code and existing links).
* **Dates are ISO** (`2026-10-09`) everywhere, also in prose such as "Member
  since"; a localised long-date format would be an app-wide change.
* **Native file and date controls** show the browser's language, not the
  page's.
* **Desktop app build** (`.github/workflows/desktop-app.yml`,
  `packaging/desktop/build.py`) still installs version ranges; a lock for its
  Windows and macOS targets is not made.
* **Portal password rule.** Hosting portal accounts still need only length
  plus letters and digits; the wiki's policy (no user name, email or common
  password) could be adopted there, and then for the hosted wiki's
  administrator password too.
* **Type checker.** None is configured. mypy reported 274 errors in the third
  round; one was a real defect and is fixed (AUDIT #24). Making it pass means
  annotating about 100 files and adding it to CI.
* **Docker base images** are pinned by tag (`python:3.12-slim-trixie`), not
  by digest; hosting updates pull the base image again and rebuild the
  tenant image every time.
* **Other archive readers.** The plugin manager's package check and the
  desktop launcher's backup restore refuse only encrypted members, not the
  other members `core/zipcheck.py` refuses; both read archives from trusted
  people (the owner or a superuser, the local user). They could adopt it.
* **Desktop write timeout.** The desktop launcher's Gunicorn fallback (used
  only without waitress, which is always bundled) runs without the write
  timeout; waitress has its own inactivity timeout, and the launcher listens
  on the local machine unless sharing on the network is turned on.
