# Changelog

## Unreleased

### Added

* Mascot: a pixel-art banana in place of the top bar logo for signed-in
  people. It bobs, blinks and hops when clicked; eleven clicks in a row put
  sunglasses on it, saved on the account until taken off under Customize,
  where it can also be hidden (the logo comes back). Administrators can show,
  hide or dress it for every account from Appearance, or switch the feature
  off under plugins. New template slots: `topbar.brand`,
  `account.display_sections`, `admin.appearance`.
* German interface: the wiki, the hosting portal and its help centre, the
  desktop launcher and the built-in user guide ship in German beside English
  and Italian. German is built in and switched on in every wiki, new or
  upgraded; administrators switch it off under Admin → Languages. Setup, the
  first-run wizard and Admin → Documentation offer it, the portal picks it
  from `Accept-Language` and the launcher from a German system locale. A `de`
  language file uploaded earlier now overrides the bundled German until it is
  removed. Read aloud does not follow the interface language: German pages
  are read in German once German is enabled among the read-aloud languages
  (automatic detection of German pages needs `langdetect`).

### Changed

* A bare `Accept-Language: *` gets the wiki's default language (on the
  portal, English) rather than the alphabetically first enabled language,
  and so does a tie between equally preferred languages that include the
  default.
* Hosting assigns finite XFS project byte and inode quotas before seeding,
  imports, copies, restores and launches; quota drift, unsupported storage,
  uncertain container state and insufficient capacity fail closed. Launches
  verify Docker’s actual mounted directory while a trusted bootstrap waits
  behind a private network-namespace gate. Interrupted task containers are
  found by actual mounted inode even after folder renames and removed before
  stopped repair. Renamed live servers refuse admission, and hosting services
  cannot access host process handles or change filesystem quota flags.
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
* Customize keeps everyday preferences visible and puts detailed colours,
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
* Restored Customize, Settings and Sign out shortcuts in the topbar. Phones
  keep a direct Customize icon and the account menu for the other actions.
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
  card); each wiki is bounded by its storage limit and hard links are stored
  once. Drive keeps the last complete backup. A wiki's own export leaves out
  files no import accepts and lists them in its manifest.
* Managed servers: packages scale with the number of files
  (`BANANA_PACKAGE_MAX_FILES`); the database is migrated once before the
  workers start; `stop`, `converge` and every operation check Docker and
  project-quota storage before anything stops; `start` after `stop` reports
  wikis that did not come back.
* The wiki's and the portal's Gunicorn workers disconnect a client once a
  write to it has waited `BW_WRITE_TIMEOUT` / `HOSTING_WRITE_TIMEOUT` seconds
  (default 300, `0` disables). Generated responses are written in 64 KiB
  pieces and, on Linux, sent in full segments.
* Managed hosting servers with `HOSTING_ALLOW_TENANT_PLUGINS=1`: an update that
  fails only because wikis with their own plugins do not come back quarantines
  those plugins (new `hosting-admin instance quarantine-plugins`) and is kept
  when the wikis then serve.
* A hosted wiki's export (the download during the grace period and the
  administrator download) keeps to the wiki's storage limit plus a tenth and
  64 MiB, and stores each hard-linked file once.

### Fixed

* Speech conversion accepts only local MP3/PCM WAV inputs and the required audio
  decoders, discards metadata and limits decoder/filter/encoder threads.
  Oversized diagnostics and execution time stop and reap the encoder, preserving
  ordinary MP3 generation and downloads at a different speed.
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
* Tenant imports and platform restores accept only stored or deflated ZIP
  members, so bzip2 and LZMA members can no longer bypass the decompression
  limits. One wiki can no longer make every platform backup fail.
* Single requests and imports have bounded cost: canvas renders are cached,
  kanban and canvas imports check sizes before parsing, exports use temporary
  files, histories are pruned by size and kanban bulk actions refuse oversized
  input up front.
* Hosting: a wiki's plugins can no longer grow its database copy into a huge
  sparse file between the snapshot and the host reading it. Platform backups,
  wiki exports, plugin snapshots and wiki duplicates check the copy against the
  size and SHA-256 its snapshot reported and against the wiki's byte budget.
* Whole-site imports refuse encrypted members and members compressed other than
  stored or deflated before reading anything.
* A client that stops reading a download no longer holds a server thread for as
  long as it keeps the connection open.

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
