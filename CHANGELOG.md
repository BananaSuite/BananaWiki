# Changelog

## 1.7.0 (unreleased)

BananaWiki 1.7 upgrades a 1.6 installation in place: the wiki database moves
to schema 11 after an automatic copy, and the hosting database to version 7
(managed updates write a rollback package first). Read [Upgrading from 1.6 to
1.7](UPGRADING.md#upgrading-from-16-to-17) first: managed servers now follow
signed release tags and install hash-pinned dependencies, restores onto a new
server ask for `--trust-source`, and third-party plugins are off by default on
new installations. What the fourth and fifth audit rounds found and what this
release does about each finding is in [AUDIT.md](AUDIT.md). Dhareck
contributed the mascot (pull request #21, polished in #25), the German
interface (#22 and #24) and a third hardening round (#23): plugin
quarantine during updates, database copies and exports held to what their
snapshot reported, stalled downloads, whole-site import compression and
account merges; AUDIT.md lists where they meet this release's own fixes.

### New

* **Two-step verification** for every wiki account (**Settings → Two-step
  verification**): after the password, or the hosting portal's sign-in, the
  wiki asks for a 6-digit code from an authenticator app or one of ten
  single-use recovery codes (shown once, stored hashed). "Back to sign-in" on
  the code page ends the pending sign-in (`POST /login/two-step/cancel`), as
  the portal's does. Turning it on or off signs out the account's other
  sessions. Wrong codes are limited per account
  and per known browser, so someone holding the password cannot use up the
  owner's attempts; after 5 wrong codes in a day the owner gets a security
  notice (at most one a day, emailed like decision notices). Owners and
  superusers who use it can require it of every administrator (**Admin →
  Settings → Security**, off by default) and reset it for an account that lost
  its app and codes (**Admin → Users**, or `bananawiki reset-two-step NAME`,
  also on managed servers). The hosting portal's "Reset admin password" turns
  it off for the account it resets. Turning it on or off, resets, new recovery
  codes and sign-ins with a recovery code are in the audit log.
* **Chat safety.** Members can block other members (from a conversation, a
  group's member list or **Settings → Blocked people**). In a conversation
  they already have, what the blocked member sends is accepted, shown to them
  as sent, and withheld from the member who blocked them: never delivered,
  also after an unblock, and not unread (administrators see it marked "Not
  delivered"). The blocked member cannot start a conversation with them or
  add them to a group (answered as for a suspended account, while an existing
  conversation opens as usual and a current member is reported as one) and
  is not offered their name; the member who blocked cannot message them
  either until they unblock. In shared groups the blocked member's messages
  are hidden from the one who blocked them (system messages stay) and do not
  count as unread. Blocks are private: nobody is notified, they are not
  audited, and they appear only in the blocker's settings and data export;
  administrators' moderation views hide nothing. Members can report messages to the
  administrators (**Admin → Chats → Reports**); a report keeps a copy of the
  text. Clearing a direct conversation hides it only for you: the other
  participant keeps their copy, messages both have cleared are erased, and
  administrators can still clear it for both when moderating.
* **Password policy.** New passwords (sign-up, settings, the forced change,
  administrators creating accounts or setting passwords, the REST API, the
  command line) may not contain the user name or the part of the account's
  email address before the @, or be one of the 10,000 most common passwords
  (bundled list, compared without case). Existing passwords keep working: the
  policy applies only when a password is set. REST API error codes:
  `password_common`, `password_personal`.
* **Managed updates follow release tags.** New installations deploy the
  highest signed `vX.Y.Z` tag instead of the latest commit on `main`
  (`source set --track tags|branch`); existing servers keep following their
  branch until switched. `updates enable` requires `--require-signatures`
  (with release tags, an annotated tag signed for that version by an allowed
  key; a server that follows a branch needs signed commits) or an explicit
  `--allow-unsigned`; `status` warns while automatic updates deploy unsigned
  code.
* **Hash-pinned dependencies.** Releases, the Docker images and CI install
  `requirements.lock` and `requirements-build.lock`: every package at one
  version, accepted only with its SHA-256, wheels only. `requirements.txt`
  keeps the ranges, and `scripts/lock_requirements.py` regenerates and
  verifies the locks for CPython 3.11-3.14 on Linux x86_64 and aarch64.
* **Remote backups**: `backups configure --append-only` (uploads never delete
  snapshots, so the stored token needs no delete right) and
  `backups prune [--token-file FILE]`, which applies the retention with a
  token given for that command only.
* **Hosting: host firewall.** A default nftables ruleset
  (`deploy/nftables/bananawiki-tenants.nft`) keeps wikis away from host
  services, the LAN and cloud metadata endpoints; tenant bridges are named
  `bwt…` so it can match them. Load it with
  `sudo bananawiki firewall | sudo nft -f -`, which fills in the private DNS
  resolvers this host uses (and persist a saved copy of that output).
* **Hosting: expiry warnings and renewals.** `INSTANCE_DURATION_DAYS=0`
  disables expiry. Owners are emailed 3 days and 1 day before a wiki expires
  (`HOSTING_EXPIRY_WARNING_DAYS`) and again when it expires. A new platform
  setting lets owners renew a wiki in its last 7 days.
* **Hosting: accepting new terms.** With `HOSTING_TERMS_REQUIRED_VERSION`
  set, every account that accepted an older version of the terms, or none, is
  asked on `/account/terms` to accept the current terms before anything else
  in the portal; it can still download its data, delete its account or sign
  out.
* **Hosting: the operator's own name and legal pages.**
  `HOSTING_SERVICE_NAME` (default `BananaWiki Hosting`) is the name the
  platform goes by in page titles, the top bar, emails, the maintenance page,
  wikis' status pages and the label authenticator apps show for new
  enrolments (enrolled authenticators keep working); the footer still names
  the software and its version. `HOSTING_TERMS_URL` and
  `HOSTING_PRIVACY_URL` (with an optional pair for each other interface
  language: `HOSTING_TERMS_URL_IT` and `HOSTING_PRIVACY_URL_IT`,
  `HOSTING_TERMS_URL_DE` and `HOSTING_PRIVACY_URL_DE`) send `/terms`,
  `/privacy` and `/compliance`, and
  so every legal link of the portal, to the operator's own `https://` pages
  (`http://` for an onion address) instead of the static site's; set both or neither. With them and without
  `HOSTING_TERMS_VERSION` the portal logs a warning, since accounts would be
  recorded as accepting the release's default version. Unset, nothing
  changes, and neither setting asks anyone to accept the terms again.
* **API tokens page**: tokens issued through the API show the token that
  issued them and how many tokens each one issued; the revoke confirmation and
  message say how many issued tokens are revoked with it.
* **Personal data export**: two-step verification appears as
  `data/auth__two_step.json` (whether it is on, since when, how many recovery
  codes are left; never the key or the recovery code digests), and the
  account's own chat blocks as `data/chat__blocks.json`.
* **Kanban: done columns.** A column can be marked as done (the check-mark
  button among the column tools, **Done column**). Its tickets keep their due
  dates but are never shown as overdue or due soon: not on the board, not in
  the due-date filters and not on **My tickets**, which lists them under "In
  done columns". Off for every column until set; also in the REST API
  (`PUT /kanban/columns/<id>` with `done`, columns report `done`), exports
  and imports, and board history (schema 11).
* **Mascot.** Signed-in people see a pixel-art banana in place of the top
  bar logo. It bobs, blinks and hops when clicked, so a click there no longer
  opens the home page (the site name still does). With reduced motion, the
  display preference or the operating system's, it stands still: the shell
  now runs any animation once under the system setting instead of
  restarting it every frame. On touch screens it is a finger-sized target.
  Eleven clicks put sunglasses on it, saved on the account until taken off
  under **Customise**, where the mascot can also be hidden (the logo comes
  back); a failed save says so. Resetting the display preferences keeps both
  choices (the confirmation says that language and mascot choices are
  kept), and `/api/accessibility` cannot set them. Administrators can show,
  hide or dress it for every account at once (**Admin → Appearance**, one
  audit entry per action; the result counts one account in the singular) or
  switch it off in **Admin → Plugins** (built-in, on by default). Changing
  it, for one account or for all, writes only its two choices (every other
  saved display preference stays as stored, for example a language that is
  switched off for now), and the change for all accounts is written in one
  batch. The script reads its click count and where to save from the
  mascot's own button, so page content cannot redirect its request. No
  migration: the two choices are keys of the account's display preferences.
  New template slots: `topbar.brand`, `account.display_sections`,
  `admin.appearance`.
* **New icons.** Every icon is the mascot's banana with its smile (no
  sunglasses), drawn from its 16x16 pixel art with every pixel a sharp
  square: the eight favicon presets of **Admin → Appearance** (same names,
  colours as bright as the mascot's), the logo that visitors see and that
  stands in for a hidden or switched-off mascot, the hosting portal's icon, a
  new 180x180 home-screen icon (`apple-touch-icon.png`, on a light square,
  as iOS expects), and the desktop app's window icon (handed to the system
  at 16, 32, 48 and 256 pixels, so it need not scale the pixel art), Windows
  `.ico` (16 to 256 pixels) and macOS `.icns` (16 to 1024). Logos, and the
  mascot's button icons under **Customise** and in **Admin → Appearance**,
  are shown at 32x32, twice the pixel art, without smoothing, so they stay
  sharp on high-density screens. The bundled icons' addresses carry a
  content version, so browsers drop the earlier drawing. The hosting portal
  now answers `/favicon.ico` with its icon, as the wiki does, and custom
  pages shown in their sandbox link the home-screen icon too. Uploaded
  favicons are unchanged.
  `python scripts/make_icons.py` draws them all, and BananaChat's and the
  website's copies when those trees sit next to this one; the tests run it
  with `--check`.
* **German interface.** The wiki, the hosting portal and its help centre, the
  desktop launcher and the built-in user guide ship in German beside English
  and Italian. German is built in and switched on in every wiki, new or
  upgraded; administrators switch it off under **Admin → Languages**. Setup,
  the first-run wizard and **Admin → Documentation** offer it, the portal
  picks it from `Accept-Language` and the launcher from a German system
  locale. A `de` language file uploaded earlier now overrides the bundled
  German until it is removed. The portal's legal links take German visitors
  to `HOSTING_TERMS_URL_DE` and `HOSTING_PRIVACY_URL_DE` when set, otherwise
  to the default addresses (on the static site, the English pages). Read
  aloud does not follow the interface language: German pages are read in
  German once German is enabled among the read-aloud languages (automatic
  detection of German pages needs `langdetect`).

### Changed

* A bare `Accept-Language: *` gets the wiki's default language (on the
  portal, English) rather than the alphabetically first enabled language,
  and so does a tie between equally preferred languages that include the
  default.
* Hosting can give every wiki finite XFS project byte and inode quotas,
  assigned before seeding, imports, copies, restores and launches. XFS is
  optional: no installation or update requires it. `BW_HOSTING_TENANT_QUOTAS`
  decides: `auto` (the default, new platforms included) uses them where the
  wiki storage enforces them and otherwise holds each wiki to its plan with
  the application's own checks, with a calm notice, or with a loud warning
  (the wikis still start) when hard quotas were enforced on the platform
  before and are no longer available; `required`, an explicit opt-in, starts
  wikis only under them; `off` never uses them. Under quotas, quota drift,
  uncertain container state and insufficient capacity fail closed, and so
  does storage without them under `required`. Launches verify Docker’s actual
  mounted directory while a trusted bootstrap waits behind a private
  network-namespace gate (without quotas too: by inode). Interrupted task
  containers are found by actual mounted inode even after folder renames and
  removed before stopped repair. Renamed live servers refuse admission, and
  hosting services cannot access host process handles or change filesystem
  quota flags.
* **Updates from servers without XFS quotas or BuildKit** (a production
  incident: an update from a release of 2 October to a development build
  that required XFS stopped the wikis of an ext4 server, rolled back after
  6 minutes, and was repeated every hour):
  * without hard quotas (`auto` on ext4, or `off`) wikis run as under 1.6,
    held to their plan by their own upload check and the portal's storage
    check, and a new wiki needs its plan plus a reserve free; a wiki without
    a plan (apex, administrators') is held to
    `HOSTING_AGENT_MAX_STORAGE_BYTES`. The agent's log, `status`
    (`tenant_storage`, as the agent sees it, in `notes`; in `warnings`, with
    `lost`, when quotas were lost) and a banner on the portal's
    administration page (information, or a warning when lost) say so;
    `status` also warns when tenant plugins run without quotas.
    `config get|set tenant-quotas` changes the setting after checking the
    storage. A new platform installs on any storage: `install` says which
    limits apply and how to get hard quotas, and `install --tenant-quotas`
    chooses the setting (refused with `--restore`);
  * an update runs the new release's own `banana preflight` before anything
    stops (one JSON line, protocol 1): its storage and quota policy, its
    settings (tenant plugins need subdomain mode), a kernel with IPv6. Its
    warnings and notes (storage without hard quotas is a note) are printed
    and kept in the result. A refusal stops nothing and records the commit
    as failed; Docker not answering, or no answer in time, records nothing;
  * Docker's BuildKit component is checked before a release is prepared
    (`docker buildx version`; the message names `docker-buildx` and
    `docker-buildx-plugin`), a build must leave the tenant image in Docker,
    and the Dockerfiles no longer name a frontend (`# syntax=`), which made
    every build look it up on Docker Hub;
  * `config/failed-revision.json` records the stage (`preparation`,
    `preflight`, `rollback`), the reason and the time, `status` shows them,
    and a record of a commit the installed release already contains (left by
    an older updater) is removed;
  * installing a release's units that then fail is recorded
    (`config/failed-convergence.json`, `status`): automatic updates no
    longer repeat it every interval;
  * a host without Caddy (another reverse proxy) no longer logs a routing
    warning at every wiki start and stop.
* New hosting reservations remain pending until provisioning completes.
  Recovery and start refuse unfinished or failed wikis, and interrupted
  provisioning can be cancelled safely without releasing a live reservation.
* Hosting requires Linux x86_64: on other architectures the portal and
  maintenance service refuse to start and the runtime agent launches no wiki.
* REST API: JSON bodies and uploaded JSON files with non-finite numbers,
  unpaired surrogates or more than 64 nesting levels are refused;
  `Idempotency-Key` replays re-check current access and answer 409
  `idempotency_replay_unavailable` when a returned resource became
  unreadable or the answer exceeded 2 MiB; keyed writes commit with their
  replay record; server, storage and routing errors carry a `request_id`.
* Page editor actions and insertion dialogs share small template macros.
  Formatting and insertion tools use a clear separator and consistent spacing.
* Hosting textareas share label, help-text and value rendering; transfer
  requests use one partial on both dashboard and wiki management pages.
  Administrative utility lists use the shared panel-list component.
* New page is a direct sidebar action, with New category alongside All pages.
  Article contents expand before the text on phones. Editor Save and Cancel
  actions appear in the page header and footer without covering form fields.
* Customise keeps everyday preferences visible and puts detailed colours,
  highlighting, layout and background settings in expandable sections. The
  hosting dashboard uses compact summaries and wiki rows; occasional setup
  and bulk controls open on request, and failed create forms retain choices.
* The wiki and hosting portal now serve one shared design-system stylesheet,
  with the existing static URL and caching behavior. Component styles are
  consolidated, and settings use explicit form sections with a consistent
  control and typography scale. Page edit information sits below the title.
* Polished heading hierarchy, article spacing, menus and navigation icons.
  Canvas tools now use consistent line icons; Kanban keyboard help opens on
  demand. Plugin status, People links and portal controls are easier to scan.
* Refined the wiki and hosting portal with a compact navigation shell, quieter
  tables and lists, and divider-based settings sections. The Markdown editor
  has a joined writing/preview surface and keeps its save actions in reach.
* Page actions now share one menu, creation starts from the sidebar's New page button,
  and uncommon editor commands have named menus. Administration links are
  grouped by task; the feature list can be searched. Hosted wiki links distinguish
  opening the wiki from managing it, with maintenance controls in disclosures.
* Restored Customise, Settings and Sign out shortcuts in the topbar. Phones
  keep a direct Customise icon and the account menu for the other actions.
* Hosting account settings now have a section index. On the administrator
  dashboard, account and invite creation forms open from their section headings.
* Ordinary wiki and portal pages now share a 1120px content frame, with an
  800px article reading measure and wider editor, board and canvas workspaces.
  Forms, buttons and disclosures share their styling, labels and focus behavior;
  settings and permission groups use dividers instead of nested cards.
* Package installs use the same security dependency floors as server installs.
  The speech extra installs from wheels and uses built-in language detection
  unless an operator separately installs `langdetect`.
* Container images use Debian 13, apply available system security updates
  during the build, and omit Python package installers from the runtime image.
* `page.view_all` now controls reading and `category.view_all` category
  listings (navigation, lists, search, `GET /api/v1/categories`). Schema 6
  grants both once to every saved permission set and custom role, so nothing
  changes on upgrade; sets saved afterwards are enforced as saved.
* Deleting a category with its pages (web, API, bulk) is all or nothing and
  follows each page's rules: hidden, forbidden, protected, checked-out or
  scheduled pages refuse it before anything changes, and deletion slowdown
  applies. The API's `page_action=delete` also needs the `pages` write scope
  and answers 202, 403 or 409.
* Renaming or deleting an account no longer edits pages or drafts.
  `/users/<former name>` leads to the account, former names stay reserved for
  it, and members rename themselves at most 3 times a day.
* A suspended administrator lifts their own suspension only when it was not
  imposed by an owner or superuser (schema 6 records it; older suspensions
  count as imposed). Lock-mode merges demote, lock and clear the overrides of
  the source; merges recheck everything in their transaction and cannot remove
  the last active administrator.
* Contribution approvals apply the version the reviewer saw: a proposal or
  page changed meanwhile is refused instead of overwritten.
* Hosting: "Pause deletion countdown" really pauses the purge and adds the
  paused time to the retention; 1.4 wikis whose download was blocked stay
  paused until an administrator resumes them. Deleted accounts become
  `~deleted-<id>`.
* Hosting updates rebuild the tenant image on a freshly pulled base with
  current Debian and Python packages (`BW_REFRESH`), falling back to the
  cached base with `image_warnings` when the pull fails, or when the build
  fails on the new base (its ACL stage pins two exact Debian unstable
  versions); only a build that also fails on the previous base, such as in a
  Debian mirror or PyPI outage, records the commit as failed until
  `update --retry-failed`. An unhealthy wiki is reported in
  `unready_tenants` instead of keeping the platform in maintenance or rolling
  back the update; `recover --abandon` drops an operation that cannot finish.
* API token expiries are at most 10 years ahead. Profile pictures are limited
  to 4 megapixels and profile/background uploads to 10 per 10 minutes.

* The wiki session cookie is `__Host-<name>` over HTTPS; existing sessions move
  to it on their first HTTPS request. Desktop and plain HTTP keep the old name.
* Deleting an account hands its kanban tickets and comments on other boards to
  the board owner and its boards to an administrator, through a new
  `user.delete` interceptor, instead of deleting them.
* Schema 7: a read-aloud job whose worker dies three times is marked failed.
  `BW_TTS_MAX_JOB_SECONDS` bounds a job; on POSIX Piper runs in a child process
  per job, in pieces of at most 500 characters, optionally limited by
  `BW_TTS_PIPER_MEMORY_MB`.
* Administrators can release a former user name reserved for an account.
* Hosting schema 5: collaborators and merge records survive the deletion of the
  account that created them, and merges keep platform sign-in to the merged
  account's wikis. Hosting dates are bounded to the years 1900-9998.
* `HOSTING_ALLOW_TENANT_PLUGINS=1` is refused in port and onion mode, where the
  portal and the wikis share cookies.
* Hosting platform backups continue past a wiki that cannot be copied in full
  and report it (`backup_manifest.json`, `platform-backup.json`, the settings
  card); each wiki is bounded by its storage limit plus a tenth and 64 MiB,
  and hard links are stored once. Drive keeps the last complete backup. A
  wiki's own export (the download during the grace period and the
  administrator download) keeps to the same bound, stores hard links once
  too, and leaves out files no import accepts, listing them in its manifest.
  For a wiki the runtime agent adopted above its plan (below), that bound
  starts from the hard limit it was adopted at, so its database stays in
  platform backups and its owner can still download it.
* Managed servers: packages scale with the number of files
  (`BANANA_PACKAGE_MAX_FILES`); the database is migrated once before the
  workers start; `stop`, `converge` and every operation check Docker, and the
  storage where `BW_HOSTING_TENANT_QUOTAS=required` needs quotas, before
  anything stops; `start` after `stop` reports wikis that did not come back.
* Hosting: one wiki no longer rolls back a platform update. When the platform
  is healthy and only a few wikis that served before fail under the new
  release (at most one in ten), they are stopped and reported
  (`isolated_tenants`, exit status 2) and the rest keep the release. With
  `HOSTING_ALLOW_TENANT_PLUGINS=1`, when every failing wiki holds plugins of
  its own, the update first quarantines those plugins, as the portal's
  button does (new `hosting-admin instance quarantine-plugins`), and is kept
  when the wikis then serve (`quarantined_tenants`); the wikis that still
  fail, or whose plugins could not be quarantined (data the release cannot
  migrate fails the quarantine too), are isolated as above, plugins that
  were quarantined staying so. The quarantine marker is set before that
  step: the update's warning and the isolation say when it is in force with
  the plugins still switched on in the wiki's database.
  `BANANA_UPDATE_UNREADY_WIKIS=rollback` rolls back instead of isolating, as
  1.6 did (the quarantine still comes first).
* Hosting: storage capacity is checked only when a wiki is admitted or its
  limit raised. Snapshots, packages or exports on the same volume no longer
  stop admitted wikis or their tasks, and no longer roll updates back. Update
  and backup snapshots must leave the wikis' reservations free, and stop
  nothing if they cannot. Publishing routes no longer re-verifies every
  wiki's storage on each start.
* Hosting: a wiki suspended only by the automatic storage protection reopens
  by itself once it fits within its storage limit again: at once when an
  administrator raises the limit, otherwise at the next maintenance pass. Its
  owner is emailed when it is suspended and when it reopens. An administrator
  who reactivates a wiki that is still over its limit is warned that
  maintenance will suspend it again.
* Hosting: the maintenance service deletes moderation-log entries older than
  `HOSTING_EVENT_RETENTION_DAYS` (default 3650, the 10 years the privacy
  policy promises; 0 keeps them all).
* Hosting: `BW_HOSTING_RECOVERY_MAX_WORKERS` (default 2) also sets how many
  wikis the maintenance service restarts at once after an update or a backup
  (1.6 always used 4), so the updater's readiness wait matches what the portal
  does.
* Hosting: `/terms`, `/privacy` and `/compliance` take visitors whose portal
  language is Italian to the site's Italian pages.
* Bulk delete (`/admin/bulk`) requires typing a confirmation word (DELETE, or
  the interface language's own, such as ELIMINA in Italian), checked on the
  server, and every bulk deletion is audited.
* Version 1.7.0; CI also tests Python 3.14. The contrib GPU speech server is
  released with BananaWiki and reports version 1.7.0.
* README: a Maintainers section (started by Luca Zani; maintained by Luca
  Zani and contributors); the project's origin is told on the website's
  about page.
* Chat: a sender can delete a direct message for both participants only
  within 15 minutes of sending it. After that it stays in the recipient's
  copy; senders can still clear the conversation from their own view.
  Deleting an account still removes its direct conversations for both
  participants (a reported message keeps its copy in the report). Group
  messages are unchanged.
* Kanban: a ticket holds at most 300 comments (adding one more answers 409
  "This ticket has reached the limit of 300 comments.", API code
  `too_many_comments`). Listing a ticket's comments does a bounded amount of
  work per request: newest first, the comments share one code-highlighting
  allowance, their renderings are kept per process so reopening a ticket
  renders nothing new, and a listing renders at most about 4 MB of HTML (a
  short comment can expand a hundredfold with reference links). Older
  comments, and those beyond 300 on tickets from earlier versions, are listed
  as plain text.
* Excerpts, search snippets and other plain text no longer highlight code.
  Page lists are faster, and a page-builder page's page list no longer uses
  up the code highlighting of the page itself.
* The daily automatic sign-out, which any administrator may set, ends only
  the sessions of users and editors. Administrators, owners and superusers
  stay signed in, as with a plain administrator's mass sign-out.
* Hosting: `HOSTING_TERMS_VERSION` sets the version date of the terms the
  operator publishes; sign-ups and `/account/terms` record it (default: the
  release's `TERMS_VERSION`). The portal now refuses to start if
  `HOSTING_TERMS_REQUIRED_VERSION` is later than that version, instead of
  lowering it with a log warning. It also refuses either value if it is not
  a real `YYYY-MM-DD` date.
* Hosting: only a wiki's owner (or an administrator) can renew it; a
  collaborator allowed to start and stop it no longer can. An end date an
  administrator brought forward, or set on a wiki that had none, cannot be
  renewed away, and the expiry warning tells the owner to ask the platform.
* Hosting: password-reset and user-name reminder emails may use only their
  fifth of `HOSTING_EMAIL_DAILY_LIMIT` (reminders, which need only an
  address, at most half of it), and never the part kept for verification
  links and notices. Each address receives at most 3 resets and 3 reminders
  a day (counted apart, so reminders cannot hold back a reset), and each
  client network (an IPv4 address or an IPv6 /64) may make 10 requests a day
  across the two "forgot" forms, an IPv6 /48 30. When the recovery part is
  spent, the forms say so to everyone alike (it reveals nothing about
  accounts) instead of answering that a message is on its way, and
  administrators see **Account recovery emails are paused** in
  `/admin/attention`.
* Database schema 8-11 keeps its own frozen DDL in the migration files, so a
  later change to a feature cannot change what those versions create.
* REST API version 2.3.0 (`/status`, `/openapi.json`): 1.7 adds fields
  (kanban columns' `done`, a token's `parent_id`) and error codes.
* Italian: "wiki" is feminine (*la wiki*) everywhere, in the wiki, the hosting
  portal with its help and emails, the desktop launcher and the built-in
  docs, as in README.it and on the site. The portal uses the wiki's names
  (*Editor visuale delle pagine*, *Tour guidato*, *Lettura ad alta voce*,
  *verifica in due passaggi*, *limite di spazio*, *schede* on Kanban boards),
  and the wiki says *Registro delle chiamate API* and *codice a 6 cifre*. In
  English the portal says "Two-step verification" (was "Two-step sign-in"),
  and a plugin locked by easy mode says "easy mode" (*modalità semplice*).
* Hosting: "Sign out other devices" keeps you signed in on this browser and
  is offered only when other devices are signed in. Accounts waiting for
  approval, a confirmed address or new terms can use what their Account page
  offers (change the password, two-step verification, signed-in devices);
  options their state would refuse are no longer shown.

### Interface and accessibility

A browser pass crawled every page of the wiki (247) and of the hosting portal
(109) at phone, tablet and desktop widths, in both themes, in English and
Italian, with axe-core and a keyboard walk, and fixed what it found:

* Contrast (WCAG 2.1 AA in both themes): status badges, count pills,
  announcements, difficulty tags, code comments and the dark theme's danger
  colour, now one shared value in the wiki and the portal. Links inside
  sentences are underlined.
* Keyboard and screen readers: on phones and tablets the closed side menu is
  hidden from the keyboard and screen readers, opens below the top bar even
  with banners shown, and closes on a tap outside. Board, kanban embed,
  code preview and table scroll areas can be reached by keyboard; chat
  transcripts keep list semantics in their live region; task-list boxes are
  announced. Portal forms mark the field at fault, invalid fields get a red
  border (portal and wiki), breadcrumbs have a name, error pages are titled
  in words, and the two-step code field accepts recovery codes on phones.
* Tables: a table with row actions becomes one card per row whenever its
  columns do not fit, not only on phones (API tokens, accounts, webhooks,
  chat groups and members, the audit log, the user list, the portal's
  account and activity-log tables), so no action is hidden past the edge.
  Other tables scroll with edge shadows, keep whole words in their last
  column and never break an ISO date at a hyphen.
* Layout: tab strips wrap; form fields stay aligned when a label wraps;
  sticky Save bars never cover a focused field; the top bar shows the whole
  wiki name and portal brand on phones; banners keep their close button on
  the first line; signed-out visitors of a private wiki no longer see an
  empty sidebar. Canvas on a phone shows the whole width when it stays
  legible and centres a mind map on its hub; read-only previews shrink to
  their content.
* Wording: Kanban history and activity, the portal's activity log and the
  reasons and backup notes it writes, and the maintenance page during an
  update are in the reader's language (Italian times and sizes with Italian
  units and the decimal comma). Singular and plural forms, empty states and
  Italian terms were checked page by page. Read aloud names each language in
  its own name and the reader's ("Deutsch (tedesco)"), in the reader's
  alphabetical order. Search results show page text without Markdown,
  table-of-contents markers or shortcodes. The API guide sets parameter and
  header names as code.
* Hosting portal: expired wikis show an Expired badge, are no longer counted
  as running and offer no Open, Stop, Restart or (unless the platform allows
  it) Terminate; deleted wikis offer "Download your data"; storage over the
  limit is red; a lapsed custom domain says so; administrator wikis say
  "Never expires"; the dashboard shows pending outgoing transfers. The
  analytics chart has one bar per day of the period and its table lists the
  days with traffic. DNS records break only after dots and each value has a
  Copy button. The denied-account and scheduled-deletion delays are entered
  in days, hours and minutes and show the stored value. The admin wiki page's
  storage checkbox says "No storage limit". A denied account is told so on
  its Account page; the bounced-address page names the address, asks for the
  current password before replacing it and shows the support contact.
* The portal's help centre was rewritten to match the portal's and the
  wiki's actual labels, in English and Italian and then in German; `?lang=`
  keeps the chosen language on links between articles. Its list of features
  a new wiki starts with now includes the mascot.
* Long German compound words in the admin menu, page headings, the portal's
  feature list and the read-aloud system table are hyphenated on German pages
  instead of breaking at any letter; English and Italian words still wrap
  whole.
* The portal's moderation log names every event in words: quarantined
  plugins, a wiki over its storage limit, linked wiki accounts, rotated
  sign-in credentials and platform backup downloads and restores showed
  their raw code.
* Open chat reports count in **Needs your attention** (avatar badge, menu,
  page and emails).

### Fixed

* Speech conversion accepts only local MP3/PCM WAV inputs and the required audio
  decoders, discards metadata and limits decoder/filter/encoder threads.
  Oversized diagnostics and execution time stop and reap the encoder, preserving
  ordinary MP3 generation and downloads at a different speed.
* A value in `config/app.env` with an unbalanced quote (such as an
  apostrophe outside quotes) makes the platform's commands name the file, the
  line and the setting instead of failing with "No closing quotation".
* Managed backups retain the allowed SSH commit signers for signed updates.
  Failed restores put back repository URLs, credentials and signer trust
  together with the previous data and release.
* An invalid Ctrl+S keeps the editor's unsaved-work warning. Changes to titles,
  categories, summaries and feature fields also count as unsaved work.
* Delayed preview and search replies cannot replace newer results or reopen
  dismissed searches. Search errors retain navigation to the full results.
  Repeated More clicks do not load duplicate rows, and reorder saves run in order.
* The topbar theme switch saves the newly selected theme to display preferences;
  rapid toggles save in order and the choice survives a reload.
* A display preference holding a number too large for an integer (such as
  `1e999`, in a saved account, a public visitor's cookie or the display form)
  is dropped instead of making every page answer with a server error.
* Keyboard users can tab out of the Markdown editor and switch its mobile
  writing/preview tabs with arrow keys. Visually hidden file inputs no longer
  widen the editor, and stale sidebar searches cannot reappear after clearing.
* Malformed URLs return validation errors instead of server errors; page
  reservation API failures return their intended HTTP status.
* Malformed bot-check tokens and API form nonces no longer cause server
  errors. OAuth validates PKCE character sets and preserves valid Unicode
  callback paths during authorization-code redemption.
* Invalid Unicode CSRF tokens are rejected as bad requests. Concurrent first
  downloads of legacy attachments restore complete files independently;
  deleting a file during download preparation returns 404 instead of 500.
* Runtime routing retries a failed Caddy reload, including after an agent
  restart. Tenant tasks have bounded execution, clean up timed-out containers,
  and serialize with container start and stop operations.
* Stopped tenants keep their network reservation until Caddy acknowledges the
  route removal, so a failed reload cannot send an old hostname to another
  tenant after address reuse. Retired network cleanup survives agent restarts.
* Archive and snapshot reads respect the observed file size and manifest
  limits. Failed exports preserve existing backups, and rejected platform
  restores leave running tenants untouched.
* Container images normalize public application file permissions so restrictive
  source checkout permissions do not prevent unprivileged startup.
* Cancelling a portal restore stops its upload. Upload forms reject invalid
  progress replies, prevent duplicate submissions and recover after failure.
* Opening a hosted wiki no longer lands on the hosting portal. When Caddy
  has no route for a wiki's address (a hand-written Caddyfile, a server still
  on the 1.4 proxy setup, a container that has just restarted), the portal
  proxies the wiki itself or shows a status page on the wiki's address
  (no wiki here, paused, suspended, expired, starting). It never shows portal
  pages on a wiki host.
* Behind Cloudflare proxied records: no redirect loop in Flexible mode, the
  real visitor address instead of Cloudflare's (rate limits and sign-in
  limits were shared by everyone), no empty "Bad gateway" while a wiki
  starts (retries, then the portal's status page, answered as 503), `www`
  redirects to the base domain, and proxied custom domains verify. New
  `bananawiki proxy` options: `--tls acme|cloudflare-dns|origin-cert`,
  `--cloudflare`, `--cloudflare-token-file`, `--origin-cert/--origin-key`.
  See "Behind Cloudflare" in docs/deployment.md.
* Hosting portal and wiki interface: many layout regressions from the
  rewrite (light theme in the portal, misaligned cards, broken phone top
  bar, unstyled inputs, code highlighting, menus running off screen), and a
  plainer look closer to 1.4.
* Platform sign-in and account linking work in Chromium and Edge: the consent
  and linking pages allow the validated redirect origin in `form-action`.
* Renaming a page updates links inside builder documents too, without
  overwriting concurrent saves, and always redirects to the new address.
* Hosting: deleting an account checks every wiki before acting on any; admin
  bulk actions report per-item failures instead of answering 500; maintenance
  backs off rows that keep failing; "Extend" no longer fails on far expiries.
* Desktop: the launcher binds its port exclusively on Windows and verifies its
  own /health answer, so another program on the port is reported.
* Hosting: merging accounts no longer leaves the target as a collaborator of
  its own wikis, which kept it access after a later transfer.
* Far dates (an API token expiring in 9999, for example) no longer make the API
  administration and token pages fail; they are shown in UTC.
* Hosting: a failed or cancelled create, duplicate or import leaves alone a
  data folder that already existed under its name, and any folder when the
  portal never handed it to the runtime agent (the one exception, a portal
  worker killed outright while the agent works on the creation, is described
  in docs/hosting.md); terminating moves the data before releasing the name.
  Recovery, purges, expiry and account deletion continue past a failing wiki
  or account. A user name cannot block another account's deletion.
* Hosting: containers are listed after the maintenance service stops and a
  missing container no longer fails recovery; if Docker does not answer when
  an operation begins, nothing is changed and the update is not marked failed.
* "Remember me" sessions ended after 7 idle days instead of 30.
* Signing out one session from **Admin → Sessions** works however many
  sessions are active.
* "Restore original password" can no longer roll back a password that was
  changed after the temporary one was set.
* A sign-up approved while the expiry job ran could be deleted. A
  temporary-role revert could undo a later promotion made while the feature
  was paused, or leave a custom role of the higher base role behind.
* Bulk Markdown import in update mode overwrote pages protected or reserved
  by someone else. A demotion that ends a page protection is now recorded.
* Changing only a page's title (or only its builder document) no longer
  writes back content or a title saved by someone else in the meantime.
* Uploads keep file names in any script (Cyrillic, CJK, Greek and so on,
  normalised to NFC, without path, control or direction-override characters)
  instead of refusing or mangling them, and downloads send an RFC 5987
  `filename*`. Canvas outline and kanban JSON exports of non-Latin titles no
  longer fail behind Gunicorn.
* Backups: a user upload named like a database (`*.db`, `*.sqlite`,
  `*.sqlite3`, `*-wal`) no longer fails every backup, update rollback package
  and remote backup. Only the wiki's and the portal's databases are
  integrity-checked, so a hosted wiki's damaged database cannot fail other
  wikis' backups. Uploads named `x.db-wal` are no longer left out of backups.
* Upgrade from 1.4: the takeover repairs of the wiki and the hosting databases
  remove chains of orphaned rows of any length (for example a chat with a
  deleted user that still has messages) instead of rolling the whole upgrade
  back on every start.
* Hosting: an address another account left unverified past the verification
  link's lifetime no longer blocks its real owner while verification is
  required, and changing it away and back does not hold it longer. Within 30
  days, only the first three addresses an account takes (signing up counts)
  are held from the moment they are typed; later ones count from the first,
  whether or not the account confirmed an address of its own in between. A
  verification link sent later keeps the address while the link is valid,
  for at most twice the link's lifetime.
* Hosting: claiming a custom domain whose proof lapsed more than 7 days ago
  no longer removes the other wiki's binding. The binding moves only when the
  claimant verifies the domain with its own TXT token, and the previous owner
  keeps it if their proof comes back first.
* Hosting: an account in several blocking states at once no longer bounces
  between two pages with "too many redirects" (for example an unconfirmed
  address plus new terms to accept, or an administrator without the required
  two-step verification plus a missing address). The account is held on the
  page for the first state, in a fixed order (scheduled deletion, flagged
  address, missing address, unconfirmed address, approval, administrator
  two-step, new terms), and moves on as each one is resolved. Signing out,
  deleting the account and revoking API and wiki sign-in tokens work in every
  state.
* Re-hashing a password at sign-in (a stored hash made with another
  `BW_PASSWORD_HASH_METHOD` or cost) no longer revokes the account's API
  tokens or makes its other browsers unknown, also when an API request reads
  the token before the re-hash is saved and the account after it. Two
  sign-ins re-hashing the same password at once no longer refuse the right
  password.
* Importing a site exported from a wiki with another secret key no longer
  locks out the accounts that use two-step verification, often the importing
  owner. Set-ups this wiki cannot read are turned off, with a notice naming
  the accounts and a `site.import_two_step_off` audit entry. Importing an
  export of the same wiki no longer duplicates audit log entries.
* Restore, rollback and `backups restore` on a server that requires
  signatures accept a release whose tag is signed (the documented release
  procedure), not only signed commits. A refused restore says how to proceed.
* Remote backup retention (`backups prune` and the pruning after each upload)
  counts only snapshots that the series' recovery key authenticated, and
  never deletes while fewer than `--keep` of them would remain. Branches
  named with dates in the future, or names that are no dates, are neither
  counted nor deleted and no longer break later uploads.
* `--allow-unauthenticated` no longer accepts a snapshot that cannot predate
  authentication: one dated in the future, or at or after the series' first
  authenticated snapshot, which every authenticated snapshot records (so
  retention deleting that snapshot changes nothing). The refusal no longer
  claims that an untagged snapshot is an old one.
* Updates and restores refuse a 1.7 or later release that lacks
  `requirements.lock` or `requirements-build.lock`, or has only one of them,
  instead of silently installing unpinned versions; `--allow-unpinned`
  (on `update`, `restore`, `rollback` and `backups restore`) installs it
  anyway. Releases from before 1.7 still install their
  `requirements.txt` ranges, with a warning and `dependencies_unpinned` in
  the result.
* Hosting: an update that isolates a wiki stops it through the runtime agent
  and checks that it stopped before reporting it; otherwise the update is
  rolled back. The portal never starts an isolated wiki by itself: not its
  recovery after a restart of the maintenance service or the host (also for
  a wiki isolated while its start was in progress), and not a changed policy
  that restarts the running wikis. Starting or restarting it from the portal
  lifts the isolation.
* Hosting, upgrade: where hard quotas apply, wikis that 1.6 left above their
  storage plan, the 10 GiB ceiling or 100,000 files start under 1.7 instead
  of staying down with `quota_unavailable`. They are adopted at what they hold plus 64 MiB and
  1,024 files, cannot grow past that, and the limit follows their usage back
  down to the plan. The portal records a `storage.over_plan` event and emails
  the owner once, including when the maintenance recovery starts the wiki.
  Plans above `HOSTING_AGENT_MAX_STORAGE_BYTES` are held to the ceiling
  instead of refused.
* Hosting, upgrade: wikis from 1.6 are adopted without checking their plans
  against the volume (their data is already on it). When those plans add up
  to more than the volume holds, update, backup and rollback snapshots no
  longer all fail with "would use space reserved for the hosted wikis'
  storage quotas": they leave each such wiki its adoption headroom and print
  a warning with the shortfall, and the wiki's next start after space was
  freed (or its plan lowered) admits it.
* Hosting, upgrade: hosted wikis no longer write a pre-upgrade copy of their
  database inside their quota'd directory, since the platform's update
  package already covers them. A wiki with less free room than its database
  still starts. A standalone wiki that cannot write its copy (full disk)
  refuses to upgrade with a message that says where and how much space it
  needs.
* Hosting: the two-step page's "Back to sign in" now ends the half-finished
  sign-in (it was a plain link, and the pending second step stayed valid for
  a few minutes).
* Hosting: creating a wiki refuses an administrator login typed in without
  ticking "Choose the administrator login myself"; without JavaScript it was
  silently replaced by a generated one.
* Hosting: denied accounts wait one day before they are deleted, as
  documented. Hosting schema 7 turns a stored delay of 0, the 1.4 column
  default that the 1.6 approval form also saved whatever was shown, into one
  day; "never" and other values are kept.
* Headings in pages, kanban boards, tickets and comments, canvases and
  announcements carry an anchor (made from their text, or written for them),
  and a heading named like a control around it could stand in for that
  control. Now saving a ticket keeps the assignees, priority, due date and
  labels its side panel shows, comments and checklist items send what was
  typed, tickets open, the board's search, filters, panels and its Edit,
  Sharing and Delete dialogs work, and so do the page's Rename, Move and
  Change address dialogs, the editor's Link, Table and Video dialogs (and
  Ctrl+K), the canvas's node and connection dialogs and page search, the
  sidebar's live search and reordering, and the canvas history preview. The
  interface finds its own elements by their tag, a data attribute or inside
  its own container, and the editor's **Add files** and **Load Markdown
  file** buttons, the ticket's **Attach a file** button, the edit summary and
  the Move dialog's category hold their control instead of naming it.
  Headings called Main, Sidebar or Attachments get `_1` in their anchor
  (`#main_1`), so the skip link, the menu button and the return to the
  attachments panel after an upload reach the page's own parts, and the
  contents list reaches the heading. Headings in the announcement bar have no
  anchor.

### Security

* Sign-in and password-protected account actions recheck the current credentials
  and revocation state in their transaction. Administrator mutations and active
  impersonation recheck current account authority, including hierarchy changes.
* Hosting OAuth verification and account links reject suspended, pending and
  denied accounts. Password changes revoke OAuth tokens and unused codes.
* Markdown rendering bounds delimiter work, nesting and heading counts before
  parsing. Pathological content remains readable as escaped source, including
  malformed fences, repeated entities, escapes and generated line breaks.
* Code fences accept only bounded display options. Authors cannot pass arbitrary
  lexer or formatter arguments to Pygments. Automatic language detection uses
  a short sample; code over 65,536 characters remains literal code without
  syntax-highlighting amplification. Ordinary article formatting is preserved.
* Tenant ZIP imports reject file/directory path collisions before writing staged
  files. Storage walks honor their deadline within large directories.
* Runtime-agent connections, task queues and subprocess output are bounded.
  Inspection and execution share one deadline; timed-out or flooding tasks are
  reaped, and stopped-tenant tasks retain an in-container deadline.
* JSON limits apply before parsing and CSRF processing, including streams
  without a content length. Sanitized images must still fit the upload budget;
  final file publication serializes the storage quota check across workers.
* Wiki and portal password verification reserve rate-limit allowance atomically.
  Owner deletion, owner promotion and chat membership changes recheck current
  authority inside the write transaction.
* Direct-message and group uploads share an atomic daily allowance. Schema 5
  adds a durable usage ledger so deleting messages or clearing conversations
  cannot reset that allowance.
* Outbound HTTP deadlines cover DNS, connection attempts, TLS and response
  reads while preserving address pinning and certificate hostname validation.
* Hosted third-party Python plugins are disabled by default. Operators may
  explicitly enable trusted tenant code with `HOSTING_ALLOW_TENANT_PLUGINS=1`;
  files and settings are preserved, and quarantine always disables that code.
* Portal session cookie is `__Host-bwh_session` over HTTPS, so a wiki cannot
  plant a portal session (users sign in once more).
* Administrators are changed only by owners and superusers everywhere: the
  REST API, the owner toggle, temporary accounts, self-reactivation, token
  revocation and the data export now follow that rule.
* Sign-in limits no longer let anyone lock an account out; a successful
  portal sign-in no longer resets the per-address limit.
* API idempotency never stores new tokens or webhook secrets, and keys are
  bound to one token.
* Hosted wikis: per-wiki GPU speech tokens instead of the platform token;
  webhooks and federation cannot reach private networks.
* Personal data export is an explicit allowlist (no hidden suspension
  reasons, impersonation logs or content the account can no longer read).
* Root controller: a tenant can no longer block updates through its
  maintenance marker or make backup packages unrestorable; packages are
  verified before use; tenant files are captured by descriptor. Remote
  backups are authenticated; older snapshots need `--allow-unauthenticated`.
* Single requests have bounded cost: read-aloud text normalisation is linear
  and runs outside write transactions; unterminated `[[video`, `[[kanban` and
  `[[canvas` shortcodes, repeated `[TOC]` markers and misaligned code fences no
  longer bypass the Markdown limits; page-list excerpts read a bounded start
  of each page; every diff shares a work budget; images are checked against
  per-use pixel limits before decoding.
* Custom pages no longer reach anonymous visitors (or suspended
  administrators) on hosts that forbid public wikis.
* The REST API can no longer delete protected, hidden or slowed-down pages
  through a category deletion.
* Renaming an account can no longer change pages it cannot read, and a
  suspended administrator can no longer lift a suspension imposed by an owner
  or superuser.
* Hosted wikis keep receiving operating-system and Python security updates:
  every update reruns the tenant image's package upgrade and Python install
  instead of reusing cached layers (the FFmpeg and ACL build stages are
  rebuilt when the base image changes).
* Canvas page nodes no longer store or reveal titles and slugs of pages their
  author cannot read, in reads or in the personal data export.
* A hosted wiki can no longer plant another wiki's session cookie over HTTPS
  (`__Host-` prefix); tenant plugins are refused where the portal and the wikis
  share a host.
* The desktop launcher's port cannot be shared or taken over on Windows.
* Tenant imports, platform restores and whole-site imports accept only stored
  or deflated ZIP members, so bzip2 and LZMA members can no longer bypass the
  decompression limits; whole-site imports also refuse encrypted members, and
  check both before reading anything. One wiki can no longer make every
  platform backup fail.
* Single requests and imports have bounded cost: canvas renders are cached,
  kanban and canvas imports check sizes before parsing, exports use temporary
  files, histories are pruned by size and kanban bulk actions refuse oversized
  input up front.
* Restores (`restore`, `rollback`, `install --restore`, `backups restore`) no
  longer deploy a package's bundled source unchecked. They reuse a prepared
  release, or build the revision from this server's update source
  (signature-checked when required), and otherwise ask the operator to
  confirm the shown revision with `--trust-source REVISION`. Accepting an
  unauthenticated remote snapshot with `--allow-unauthenticated` prints a
  warning.
* Backups and updates copy each hosted wiki's database as bytes; the root
  controller never opens a tenant's SQLite file or its WAL. Platform backups,
  wiki exports, plugin snapshots and wiki duplicates check each copy against
  the size and SHA-256 its snapshot reported and against the wiki's byte
  budget, so a wiki's plugins can no longer grow it into a huge sparse file
  between the snapshot and the host reading it.
* Wiki sign-in gives a browser that signed in to an account before its own
  failure budget, through a signed device cookie retired by password changes,
  so strangers can no longer lock the owner out by using up the account's
  shared budget (the portal fix of the third round, now on the wiki too).
* Every per-address limit of the wiki and the portal counts an IPv6 client as
  its whole /64: sign-in, sign-up, platform sign-in, the global request
  limits, the anonymous page, canvas, read-aloud and chat limits, group joins,
  portal forms and daily portal sign-ups.
* Unknown user names spend the same time as the configured password hash
  method. Passwords stored with an older method or cost are re-hashed at the
  next sign-in.
* Delegated invite codes can only grant a plain member account or what the
  inviter holds (permissions, readable and writable categories). Sign-up
  checks this again against the creator's current rights, so invites from a
  since-demoted administrator no longer create administrators.
* The account hierarchy also covers profile moderation, mass sign-out,
  temporary-role reverts and the sessions and audit pages: a plain
  administrator no longer edits other administrators' profiles, signs out
  owners, superusers or other administrators, or sees their sign-in
  addresses.
* Whole-site export and import are restricted to owners and superusers.
  Imports drop triggers and views this release does not create, and keep the
  live audit log.
* The audit log records many more administrative actions: superuser grants,
  permission overrides, custom roles, approvals, unsuspensions,
  impersonation, session revocations and mass sign-outs, attribution and
  role-history clean-up, invite codes, bulk deletions, feature and plugin
  switches and installs, and administrators opening, exporting, clearing or
  taking over private conversations and groups they are not in, downloading
  their attachments or resolving reports. Only owners and superusers can
  switch the log off or change its retention; switching it off and pruning
  are both recorded.
* Third-party plugins are off by default for new installations
  (`BW_ALLOW_EXTERNAL_PLUGINS=1` allows them), and only the owner and
  superusers can install, update or enable one. Administrators can still
  switch plugins off and remove them. An upgraded installation whose plugins
  folder already holds plugins keeps them allowed while the variable is
  unset, and logs a warning at every start. The built-in admin guide
  ("Owners and trust") and the user guide say so, in every language.
* REST API: changing your own password through `PUT /users/<id>` needs
  `current_password`. Tokens issued through `POST /tokens` are revoked with
  the token that issued them and cannot carry the `tokens` or `userbot`
  scope. The `/userbot/*` endpoints answer only while automation mode is on,
  so an administrator's lock can no longer be bypassed with an ordinary
  token. No API call creates owners or superusers.
* The outbound HTTP filter always refuses cloud metadata addresses (AWS and
  Google IPv6, Alibaba, Azure WireServer) and Teredo, and judges 6to4
  addresses by the IPv4 address inside, even where private networks are
  allowed.
* Rendering has a bounded cost: one page highlights at most 64 KB of code
  (later blocks stay plain code, and the blocks of a page-builder page share
  the allowance); rendered pages, code snippets and PDF exports are cached by
  content hash; the live preview and code highlighting endpoints are
  editor-only, at 60 requests a minute.
* Style attributes in pages accept only bounded, non-negative lengths (no
  negative margins, viewport units, `calc()` or `var()`). Mentions, embeds
  and task lists are never rewritten inside attribute values. nh3 0.3.7 or
  later is required.
* Linking a hosting-portal account to an existing wiki account
  (`/platform-oauth/merge`) needs the wiki account's two-step code after its
  password when it uses two-step verification; before, the password alone
  created the link.
* Hosting: new self-service accounts (sign-up form, not approved by an
  administrator) may hold one wiki during their first 7 days, their wikis
  together may use at most half of the platform's wiki limit, and the sign-up
  form creates at most 10 accounts per address per day, so throwaway accounts
  can no longer fill the platform (`HOSTING_NEW_ACCOUNT_DAYS`,
  `HOSTING_NEW_ACCOUNT_MAX_INSTANCES`, `HOSTING_NEW_ACCOUNT_SHARE_PERCENT`,
  `HOSTING_SIGNUPS_PER_IP_PER_DAY`).
* Hosting: email budgets are counted in the database and shared by every
  worker. A fifth of the daily budget is reserved for password resets and
  username reminders, and each account may cause at most
  `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` messages (default 10) a day. Email
  verification stays required when the budget is spent instead of switching
  itself off.
* Hosting: accepting an ownership transfer no longer keeps the previous owner
  as a full-access collaborator; the recipient chooses no access (the
  default) or view-only access. Transfers respect the recipient's wiki limit,
  and suspended wikis count toward that limit.
* Hosting: an administrator who must use two-step verification but has not
  turned it on can only open the two-step page until they do, and has no
  administrator rights over customers' wikis. Second-step attempts from a
  known browser are counted separately over 5 minutes too, so someone who
  knows the password cannot lock the owner out.
* Read aloud: a GPU token set in the environment
  (`BW_TTS_REMOTE_GPU_AUTH_TOKEN`) is only ever sent to the URL set in the
  environment, never to an address saved on the admin page. The contrib GPU
  speech server drops slow or idle clients after `TTS_SOCKET_TIMEOUT` (30 s)
  and accepts at most `TTS_MAX_CONNECTIONS` (64) open connections.
* Chat: blocks stay private. A blocked member is no longer told by a refusal
  in a conversation they already have (what they send is withheld instead),
  the person who blocked them is no longer suggested in the name fields, and
  starting a conversation with them or adding them to a group gets the same
  answer as for a suspended account.
* Chat: moderation views and conversation exports hide the IP addresses of
  accounts the administrator may not manage (owners, superusers and, for
  plain administrators, other administrators), as the sessions pages and the
  audit log already did.
* Chat: when a site administrator who is not a member reads a private group
  through its message feed (the page polling, or paging back through
  history), the read is written to the audit log as `chat.group_read`, at
  most once per 10 minutes per group. Opening the page is still always
  recorded.
* PDF export: kept PDFs follow the uploaded images they embed. A deleted or
  replaced image is never served again from a PDF made earlier.
* Two-step code limits can no longer be multiplied with device cookies
  collected while two-step verification was off. Device cookies are signed
  with a per-account device epoch, which a new password and turning two-step
  verification on, off or resetting it retire; the browser making the change
  keeps a fresh cookie. An account also accepts at most 100 wrong codes a day
  from all browsers together, and after 10 wrong codes in a day from one
  browser each further code waits 30 seconds, then twice as long each time,
  up to an hour.
* A plain administrator could switch the audit log off, unrecorded, by
  replaying the onboarding wizard. The owner-only rule and the recording now
  live in `registry.set_enabled` (new `feature.switch` and `feature.switched`
  interceptors), whichever page switches a feature. The wizard no longer
  offers the audit log to plain administrators, and every feature it switches
  is recorded (`plugin.enabled` / `plugin.disabled`).
* Wrong `current_password` values on `PUT /api/v1/users/<own id>` count
  against the account page's password limit (10 in 15 minutes, shared with
  it); then 429 `too_many_attempts`.
* Hosting: while an account must accept new terms
  (`HOSTING_TERMS_REQUIRED_VERSION`), its API tokens and its sign-ins to
  hosted wikis are refused as well, not only its portal pages.
* Hosting: portal two-step codes have a ceiling per account across all
  browsers again: 20 per 5 minutes and 150 a day, on top of each browser's
  own 10 per 5 minutes and 50 a day. Turning two-step verification on or off
  retires every device cookie except the browser that made the change, so
  cookies collected before enrolment give no extra guesses.
* With signatures required, a server that follows release tags deploys only
  an annotated tag SSH-signed by an allowed key for its own name. A signed
  commit under a lightweight or unsigned tag (which anyone who can push tags
  could add) no longer counts as a release. The highest release that
  verifies is deployed; higher tags that do not verify are skipped and
  reported (`skipped_releases`, and a warning), so one stray tag can no
  longer hold back a security release. Only the 20 highest tags are tried:
  let only maintainers create `v*` tags on the update source.
* A restore onto an installed server keeps that server's update source
  settings, repository credentials and allowed signers, which decided what
  code may run. It no longer takes them from the package, so a package can no
  longer point later updates at another repository or turn signature checks
  off. The result's `source_warning` says when the package's settings
  differ. A new server still takes the package's.
* Hosting: the host firewall allows DNS only to the private resolvers this
  host uses, not to port 53 on every private address.
  `sudo bananawiki firewall | sudo nft -f -` loads the ruleset with them
  filled in. A resolver on the host itself is reached as a host port.
* A client that stops reading a download no longer holds a server thread for as
  long as it keeps the connection open: the wiki's and the portal's Gunicorn
  workers disconnect it once a write to it has waited `BW_WRITE_TIMEOUT` /
  `HOSTING_WRITE_TIMEOUT` seconds (default 300, `0` disables). Generated
  responses are written in 64 KiB pieces and, on Linux, sent in full
  segments.
* Hosting: form and runtime values the portal logs (bulk administrator
  actions, a wiki move that gets stuck) have their line breaks replaced, so
  they cannot add forged lines to the text log. So do the file and folder
  names of a wiki that the portal and the root controller log (folders
  nested too deep, files an export or backup leaves out, files a snapshot
  cannot stage); `core.logformat.one_line` is the one helper, which the
  standard-library-only controller imports too.

## 1.6.0

BananaWiki 1.6 is a rewrite of the whole code base. It upgrades a 1.4
installation in place (same database, files, secret key, environment
variables and URLs); see [UPGRADING.md](UPGRADING.md). Everything the 1.4
audits found and what 1.6 does about each item is in
[docs/1.4-review.md](docs/1.4-review.md).

### Architecture

* One Python package, `bananawiki` (`core`, `wiki`, `hosting`, `ops`,
  `desktop`, `sdk`), with `pyproject.toml` and a version. The root keeps only
  the entry points existing servers run (`banana`, `wsgi.py`,
  `gunicorn.conf.py`, `hosting/`, `scripts/tts_worker.py`).
* An application factory with a documented request pipeline; views are
  private by default. Every feature is a package declaring a `Feature` (routes,
  navigation, jobs, events, template slots, interceptors, translations) with a
  single on/off switch that routes, menus, jobs and permissions all respect.
* One database session per request, explicit transactions, versioned
  migrations from the 1.4 baseline (schema 3 → 4), automatic database copy
  before a schema upgrade.
* One scheduler for background jobs with leases in the database; no timers at
  import time. `bananawiki jobs run` for cron.
* Events are emitted by the services after commit, so every code path fires
  them.
* A typed configuration object read once from `BW_*` variables; invalid values
  stop the start with a clear message.
* No inline scripts or event handlers; static JavaScript modules under a
  nonce-based Content Security Policy.
* Third-party plugins use the same `Feature` API as built-in features and load
  at start-up; 1.4 plugins run through an adapter.
* The `banana` lifecycle controller is BananaWiki's own (standard library
  only), reads the 1.4 configuration files and packages unchanged, and
  converges the systemd units of servers updated from 1.4.
* A new test suite organised by module, including a test that boots 1.6 on a
  real 1.4 instance (`tests/test_upgrade_from_1x.py`).

### Security fixes

* Sign-up approval is enforced; pending and denied accounts reach only their
  status page (1.4 let them in).
* Whole-site import can no longer overwrite `config.py`, the secret key or the
  live database file (code execution for administrators, and a tenant escape
  on hosting); imports are staged, verified and applied with SQLite's backup
  API, with an automatic copy of the previous site.
* The guided tour's role preview no longer grants administrator views.
* One session system: cookies without a server-side session are refused, so
  every session can be revoked (1.4 cookies issued to suspended users survived
  suspension, password resets and mass logout).
* Sign-in limits per address and per account name, shared by all workers; a
  successful sign-in no longer resets other accounts' counters.
* Every permission of the catalogue is enforced; category actions need write
  access to the categories involved.
* Administrators can no longer take over other administrators (only owners
  and superusers change administrators).
* Translations are escaped; no `|safe` on user input or translated strings with
  parameters.
* Markdown sanitised with nh3 against a strict allow-list (no arbitrary
  classes or ids, forced `noopener`).
* `X-Forwarded-Prefix` is never trusted.
* Upload deletion is for administrators only; images are re-encoded to drop
  EXIF/GPS metadata; downloads are sandboxed.
* The page builder requires edit rights on the page and uses the normal page
  service.
* Group moderation checks the chat permission and switch.
* Platform sign-in follows the wiki's sign-up policy, links accounts by the
  portal's stable id, uses PKCE and never stores access tokens.
* Custom pages with HTML/CSS/JS run in a sandboxed document.
* Deleted chat messages are erased, and their attachments and 1.4 database
  copies removed.
* Bot protection tokens are single-use; the setup token is accepted only in
  the setup form.
* The hosting portal no longer has Docker access: a small root runtime agent
  with a validated protocol runs tenant containers; tenant secrets are passed
  in a private env-file, never on a command line; tenant databases are migrated
  only inside their container; the on-demand TLS endpoint is hidden from
  public listeners; the portal no longer contacts IP-echo services.
* Federation pairing keys and the GPU speech token are encrypted at rest;
  retired plaintext secrets (Telegram bot token) are erased by the migration.
* An outbound HTTP client that pins DNS answers and refuses internal
  addresses is used for federation, platform sign-in and the GPU server.
* `setup_wizard.py` (an unauthenticated root web UI) and the debugger on
  non-loopback addresses are gone.

### Performance

* One SQLite connection per request instead of 20–30; permissions resolved once
  per request; request statistics buffered instead of one write per request.
* Full-text search (FTS5) with visibility applied in SQL.
* Attachments stored on disk only and streamed; ZIP downloads built on disk.
* Chats fetch only new messages; kanban and canvas sync incrementally; board
  and canvas history coalesced and capped; the leaderboard reads a small
  statistics table instead of every revision.
* Indexes on the foreign keys used on hot paths.
* Updates snapshot the data online, so downtime no longer grows with the data
  size; old packages, releases and tenant images are pruned.

### New

* `bananawiki` command: `serve`, `setup-token`, `migrate`, `db check|backup|prune-retired`,
  `create-admin`, `reset-password`, `jobs`, `config check`, `export`, `import`.
* `Dockerfile` and `compose.yaml` for a single wiki behind Caddy.
* Edit-conflict detection for pages; `expected_revision` in the API.
* REST API: history, search, single category, paging, OpenAPI description.
* Bulk delete (`/admin/bulk`) through the owning services.
* BananaWiki Desktop replaces the Easy Deployment App (sharing off by default,
  backups while running, restores that keep the replaced data).
* The GPU speech server is a threaded standard-library server with
  concurrency limits.
* **Needs your attention**: every approval queue (sign-ups, quota requests,
  proposed edits, pending deletions, account merges, plugin restarts; on the
  portal: accounts, wiki feature requests, account merges) is counted per
  person, shown as a red dot with the number on the account menu (the *Admin*
  link on the portal), listed in the menu, on the admin dashboard and on
  `/attention` (`/admin/attention`), and summarised in a banner after signing
  in. People whose request was decided get a notice.
* **Notification emails**: immediately, as a digest at most every N minutes,
  or as a daily summary, with per-person opt-out and unsubscribe links, in
  each recipient's language, sent by a background job with the throttling
  state in the database; optional emails about decisions. The wiki can send
  email for the first time (SMTP with STARTTLS or SSL/TLS, Brevo or Resend;
  `BW_MAIL_*`, `BW_SMTP_*`, `BW_BASE_URL` or **Admin → Notifications**, secrets
  encrypted); the delivery code is shared with the portal
  (`bananawiki.core.mail`). The portal's approval emails cover every queue,
  every administrator and a daily mode.
* **Canvas**: templates for new canvases (flowchart, mind map, retrospective,
  SWOT); snap to grid, align and distribute, groups that move together,
  locked elements, parallelogram and hexagon shapes, straight and
  right-angled connections, an overview map, PNG/SVG image download made in
  the browser, and a text outline (`/canvas/<slug>/outline`, Markdown with
  `?format=md`) for screen readers. Dragging only redraws the affected
  connections; very large changes are saved in several batches instead of
  leaving the rest unsaved. Alignment guides with snapping while dragging;
  connections can attach to a chosen side (the flowchart's loop-back uses
  it); locks are enforced by the server (web, REST API, whole-document
  saves); PNG export includes CORS-enabled external images and draws a
  labelled placeholder for the others; `canvas.created|updated|deleted`
  registry events.
* **Kanban**: a filter bar (text, person or *Me*, label, priority, due date;
  kept in the address, `/` to search) with keyboard moves that skip hidden
  cards; ticket checklists with progress on the card; optional
  work-in-progress limits per column; due-soon highlighting; **My tickets**
  (`/kanban/mine`, `/api/kanban/my-tickets`) across all boards you can open,
  grouped by due date, with the same filters applied server-side there and on
  the board state endpoint; swimlanes by assignee, priority or label (drag
  between rows to reassign or reprioritise); archive and restore tickets
  (one, in bulk or a whole column) and boards (hidden from the list,
  read-only until restored); draggable checklist items. History shows WIP
  limits, checklists, archived tickets and a list of changes per version;
  history, revert, export and import include limits, checklists and archive
  state; `kanban.board|ticket|comment.*` registry events; live updates resume
  at once after a lost connection.
* **Page builder**: new blocks (banner, cards, gallery, questions, quote,
  table, code, Markdown text, up to four columns, page lists and canvas/Kanban
  embeds resolved with each reader's permissions), per-block alignment,
  background and spacing, starter layouts, undo/redo, duplicate and collapse,
  keyboard reordering, desktop/tablet/phone preview, accessibility checks
  (required alternative text, heading order), heading anchors for the contents
  list, and a clear warning when a draft conflicts with a newer page version.
  Opening a Markdown page in the builder keeps its formatting. Documents move
  to version 2; version 1 documents are upgraded when read. The embed block
  has a searchable picker of the canvases and boards you can open, and the
  editor preview shows them live; blocks can be copied between pages through
  the clipboard (checked by the server); administrators save runs of blocks
  as reusable **sections**; image uploads show progress and clear errors.
  **Custom pages** can be built with the builder too (new "Visual builder
  page" type, same safe renderer; other custom page types are unchanged).
* **REST API**: kanban (boards, columns with WIP limits, tickets, moves,
  checklists, comments, ticket attachments, "my tickets", archiving of
  tickets, columns and boards, board filters), canvas (list, read,
  create, change, whole-document saves, atomic operations that respect
  element locks, history) and page attachment endpoints with new
  `kanban`/`canvas` token scopes and the web interface's access rules;
  outgoing **webhooks** for page, category, account, kanban and canvas events
  (ids and names only, no names for private boards and canvases; HMAC-SHA256
  signed, sent right after the change and retried with backoff by a
  background job, delivery log, SSRF-safe, secrets encrypted; a webhook that
  keeps failing is switched off and administrators are told; Admin → REST API
  or `/api/v1/admin/webhooks`);
  `Idempotency-Key` for POST; `ETag`/`If-Match` (412) for pages and canvases;
  `X-RateLimit-*` headers; `next_offset` paging everywhere; a complete
  OpenAPI description checked against the routes; `WikiClient` and
  `verify_webhook` in the Python SDK (`bananawiki.sdk.client`).

### Changed behaviour and removals

See [UPGRADING.md](UPGRADING.md#behaviour-changes). Removed: Obsidian sync,
Banana Mode, joke audio conversion, the BananaChat installation modes,
`setup_wizard.py`, the `legacy_redirect` daemon, the portal's Arabic mirror
mode, demo wiki spawning and the hosting admin CLI.

### Highlights of the 1.4 audit

The review that preceded 1.6 found, among others: approval never enforced;
code execution through site import; administrator views through the tour
preview; irrevocable sessions of suspended users; brute-force limits reset by
any successful sign-in; decorative permissions; 20–30 database connections
and a write per request; a 500 MB request cap; attachments stored twice;
search scanning all content; category IDOR; administrator takeover of
administrators; unescaped translations; permissive Markdown sanitising;
background jobs of disabled features; features broken by the CSP; a
destructive kanban revert; a hosting portal equivalent to root on the host.
Details and status per item: [docs/1.4-review.md](docs/1.4-review.md).
