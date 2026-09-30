# Changelog

## Unreleased

### Fixed

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

### Security

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
