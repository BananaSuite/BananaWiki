# Changelog

## 1.7.2 — 2026-10-20

### Changes

* Image options in the page editor: clicking an image in the preview, or pressing **Image** with the cursor on one, opens a dialog for its alternative text, width, spacing, alignment and a crop. The dialog rewrites only that image in the Markdown, as an `<img>` tag the allow-list already keeps. A crop is uploaded as a new image through the usual upload, so the original is never changed. On screens up to 600 px wide, left- and right-aligned images sit above the text.

### Fixes

* Canvas: the edit dialog of an element resized by hand, to a width or height such as 264, can be saved again. Its size fields now take any whole pixel.

### Hosting

* A new wiki's certificate no longer fails for its first visitor. Caddy orders it during the first TLS handshake, which used to be cut after 10 seconds; the limit is now 60 seconds, and the portal makes that first handshake itself right after a wiki or a custom domain is set up, retrying with growing pauses.
* Hosting platforms never use the TLS-ALPN challenge, which Cloudflare's proxy cannot pass and Caddy picks at random. Certificates now come from Let's Encrypt alone (no ZeroSSL fallback), over HTTP-01. Run `sudo bananawiki update` to write the new Caddyfile.
* Caddy's certificate check no longer shares the portal's request limit, so scanner traffic cannot make it refuse a wiki's certificate. Refused names are logged.
* `bananawiki proxy` detects a Cloudflare-proxied domain from DNS and, in `acme` mode, recommends `--tls cloudflare-dns` or an origin certificate.
* The portal warns when it wrote new routes but Caddy did not reload.
* The maintenance service starts again running wikis whose container is gone or stuck, on every pass, not only after an update or a reboot.
* A wiki name that would make its address longer than a DNS label (63 characters, with the instance suffix) is refused.
* CI runs the Caddy tests against a pinned, checksum-verified Caddy.

## 1.7.1 — 2026-10-18

### Upgrading

* Wiki databases move to schema 12, which 1.7.0 cannot open, and desktop installations stop loading third-party plugins until allowed. Read [UPGRADING.md](UPGRADING.md) first.

### Security

* A HEAD request runs as GET in the wiki and the hosting portal, so it can no longer submit a form without a CSRF token.
* Servers that require signed updates accept only SSH signatures from the allowed signers file.
* Maintenance run as root does not follow links planted in the data folder when it sets file modes.
* Downloads made by a signed-in account (exports, chat transcripts, kanban, canvas and attachment archives) are marked private and never cached.
* Administrators cannot remove a temporary-role or scheduled-deletion entry from their own account, or from another administrator's unless they are an owner or superuser.
* Administrators change their own password in Settings, which asks for the current one. The admin user page no longer sets it.
* Replaying the onboarding wizard waits until a required two-step setup or forced password change is done.
* An editor cannot reserve a page someone else protects, or protect a page someone else has reserved.
* Page builder page lists are left out of the page's Markdown version, so search, history and exports do not show pages a reader may not open.
* Kanban: reverting a board, transferring it or changing its access through the REST API unassigns people who lost access.
* The API audit log hides the IP addresses of accounts the viewer may not manage.
* A saved GPU speech token is removed when the server's scheme, host or port changes, and a saved SMTP password when the SMTP server name changes.
* The page-not-found page no longer shows the wiki's navigation to suspended, pending or refused accounts, or to non-administrators during maintenance.
* Desktop: third-party plugins stay off unless allowed with `"allow_plugins": true` in `desktop.json` or `serve --allow-plugins`. Restoring a backup does not install or enable plugin code.
* Desktop: restore refuses encrypted, bzip2 and LZMA ZIP members before reading them.
* Minimum dependency versions include their published security fixes: Flask 3.1.3, Werkzeug 3.1.9, Jinja2 3.1.6, waitress 3.0.1, filelock 3.20.3, Markdown 3.8.1, Pygments 2.20.

### Changes

* The mascot shows for every signed-in account while its plugin is on and can no longer be hidden per account. Turning the plugin off in **Admin → Plugins** removes it.
* Hosting portal: the mascot replaces the logo in the top bar; its sunglasses are kept in the browser.
* New default colours, yellow on charcoal as in BananaChat, for the wiki, the hosting portal, emails and PDF exports. Database schema 12 gives them to themes that still have all six old defaults and adds an index for the admin dashboard's recent edits.
* Faster page rendering with custom languages.
* Faster saves and sign-ins on large wikis with automatic badges.
* The leaderboard is cached, and category pages run fewer queries.
* Docker image builds fetch the ACL stage's packages from a fixed snapshot.debian.org date. Build hosts need to reach it.
* Desktop builds install hash-pinned dependencies from `requirements-desktop.lock`.
* `.env.example` lists the hosting portal's domain and tenant limit settings.
* Locked dependencies and CI actions updated. The desktop app is built on macOS 15 and still targets macOS 14.

### Fixes

* A heading whose anchor matched one of the interface's own element ids could break the buttons and dialogs around it in pages, kanban, canvas and announcements. The interface no longer finds its elements by those ids, and headings called Main, Sidebar or Attachments get `_1` in their anchor.
* REST API POST requests with an `Idempotency-Key` work on wikis first set up on 1.6.0.
* `[[video]]` embeds YouTube playlist links and share links with a start time.
* Markdown export and import keep titles ending in `#` (such as "C#") and the full category path.
* A revoked automatic badge stays revoked. Deleting the award lets it be earned again.
* Signing out or losing the session during impersonation closes the impersonation log entry.
* Webhooks, federation peers and the remote speech server can use `http://localhost`, including `::1`, when local-network addresses are allowed.
* Federation: shares that together exceed the 8 MiB sync limit no longer stop all sharing with that wiki.
* The GPU speech server reads long pages in pieces of at most 500 characters.
* Restarting from Admin works in the official Docker image, and is not offered where it cannot work.
* A damaged site export or plugin archive is refused with a message instead of an error page.
* Importing a site from a wiki without SQLite full-text search keeps page editing working.
* `bananawiki import` works into a new or empty instance directory without `SECRET_KEY` and keeps the exported `.secret_key`.
* Error pages, pop-up errors and edit summaries the wiki writes itself appear in the reader's language. In-app JSON endpoints put the machine code in a separate `code` field.
* A new wiki's home page is created in the language chosen during setup.
* Counts use the singular for one item in English, Italian and German.
* Italian always calls the editor role "Editor", the review button "Esamina" and kanban cards "schede".
* Links in announcements are underlined, and labels wrap with the largest text settings on phones.
* Copy buttons work over plain HTTP and say when copying is not possible.
* Desktop: backups save to FAT32 and exFAT drives and network shares, a backup too old for this version is refused before anything is replaced, and the log follows a changed data folder.
* Encrypted Git backups check free staging space before uploading.
* `backups configure` keeps the stored key unless given `--replace-key`.
* Branch names that are not UTF-8 no longer break backup commands.
* Leftover files from killed backup commands are removed.
* `remote-` and `before-restore-` packages are pruned to the newest `--keep-backups`, like other packages.
* Managed servers: rolling back a failed update no longer needs room for a second copy of every wiki's files, and restores move data into place instead of copying it.
* Managed servers that still follow the retired default repository get the exact `source set --repo` command when an update fails.

### Hosting

* Wikis suspended with a timed account suspension reopen when it ends. Reactivating an account restarts only the wikis suspended with it.
* Changing or disabling the instance URL suffix is refused while two wikis would share an address or a wiki would lose its own. With the suffix disabled, apex wikis are reachable again.
* Importing or duplicating a wiki turns off two-step verification set up under the source wiki's key, and the portal and CLI name those accounts.
* Platform restores prepare storage quotas as a first start would, keep plugin quarantine, and remove the wikis they installed when they fail.
* Duplicates are held to the new wiki's storage limit. A full disk during a duplicate or download shows a message instead of an error page.
* A creation, duplicate or import that fails unexpectedly is cleaned up instead of staying "being created".
* Terminated wikis with names of 43 characters or more can be downloaded, deleted and purged.
* A rename that cannot move the wiki's folder starts the wiki again under its old name.
* Restoring an administrator's wiki without a day count keeps it without expiry, and `account set-admin --no-admin` moves apex wikis to hosting addresses.
* Wiki lists ask the wikis for their health in parallel.
* A collaborator allowed to start and stop a wiki can renew it, as its owner can.

## 1.7.0 — 2026-10-11

Upgrades a 1.6 installation in place: the wiki database moves to schema 11 after an automatic copy, the hosting database to version 7. Read [UPGRADING.md](UPGRADING.md) first.

### Upgrading

* New managed installations follow signed `vX.Y.Z` release tags. Existing servers keep their branch until switched with `source set --track tags`.
* `updates enable` needs `--require-signatures` or `--allow-unsigned`.
* Releases, Docker images and CI install hash-pinned `requirements.lock` and `requirements-build.lock`. A 1.7 release without them is refused unless `--allow-unpinned`.
* Restores onto a new server ask for `--trust-source REVISION`.
* Third-party plugins are off on new installations (`BW_ALLOW_EXTERNAL_PLUGINS=1` allows them). Upgraded installations that already have plugins keep them and log a warning.
* Hosted wikis' own Python plugins are off unless `HOSTING_ALLOW_TENANT_PLUGINS=1`, which is refused in port and onion mode.
* Session cookies use the `__Host-` prefix over HTTPS. Portal users sign in once more.
* Hosting requires Linux x86_64. XFS quotas are optional (`BW_HOSTING_TENANT_QUOTAS`).

### Security

* Sign-in, password-protected actions, administrator changes and impersonation recheck credentials and authority inside their transaction.
* Only owners and superusers change administrators, everywhere: REST API, temporary accounts, token revocation, profile moderation, mass sign-out, sessions and audit pages.
* Delegated invite codes grant at most what the inviter holds, checked again at sign-up.
* Whole-site export and import, and installing or enabling third-party plugins, are limited to owners and superusers.
* Strangers cannot lock an account out: browsers that signed in before get their own failure budget, and IPv6 clients count as their /64.
* Two-step codes are limited per browser and per account. Device cookies are retired when the password or two-step setting changes.
* Unknown user names take as long as a real password check. Older password hashes are upgraded at sign-in.
* Linking a portal account to a wiki account needs the wiki account's two-step code.
* REST API: changing your own password needs `current_password`, issued tokens cannot carry the `tokens` or `userbot` scope, `/userbot/*` answers only in automation mode, and no call creates owners or superusers.
* Markdown, code highlighting, diffs and images have bounded cost per request. Style attributes accept only bounded lengths. nh3 0.3.7 or later is required.
* JSON limits apply before parsing. The REST API refuses non-finite numbers, unpaired surrogates and deep nesting.
* Imports and restores accept only stored or deflated ZIP members and refuse path collisions.
* Outbound HTTP deadlines cover DNS, connection, TLS and reads. Cloud metadata and Teredo addresses are always refused.
* Backups and updates copy hosted wikis' databases as bytes and check each copy against its snapshot. Restores keep the server's own update source and signers. Remote backups are authenticated.
* With signatures required, only annotated tags SSH-signed by an allowed key count as releases. Higher tags that do not verify are skipped and reported.
* Hosted wikis get operating system and Python security updates with every platform update.
* Hosting: per-wiki GPU speech tokens. Webhooks and federation from hosted wikis cannot reach private networks.
* Hosting: limits for new self-service accounts (`HOSTING_NEW_ACCOUNT_*`, `HOSTING_SIGNUPS_PER_IP_PER_DAY`) and email budgets shared by all workers (`HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT`).
* Hosting: accepting a transfer drops the previous owner's full access. Administrators who must use two-step verification have no rights until they set it up.
* Hosting: the host firewall allows DNS only to the host's own resolvers.
* The personal data export is an allowlist. Canvas page nodes and PDF exports do not reveal pages or images the reader may not see.
* Chat moderation views hide the IP addresses of accounts the administrator may not manage. Reading a private group's feed is audited.
* The audit log records many more administrative actions. Only owners and superusers can switch it off or change its retention.
* Clients that stop reading a download are disconnected after `BW_WRITE_TIMEOUT` or `HOSTING_WRITE_TIMEOUT` seconds (default 300).
* Logged form values and file names cannot add forged lines to the log.
* The desktop launcher's port cannot be shared or taken over on Windows.

### New

* Two-step verification for wiki accounts with an authenticator app and ten recovery codes. Owners can require it of administrators and reset it (`bananawiki reset-two-step NAME`).
* Chat: members can block other members and report messages (Admin → Chats → Reports). Clearing a direct conversation hides it only for you.
* Password policy: new passwords may not contain the user name or email name, or be one of the 10,000 most common passwords.
* German interface for the wiki, the hosting portal and its help centre, the desktop launcher and the built-in guide.
* A pixel-art banana mascot in the top bar for signed-in people (built-in plugin, on by default). All icons are drawn from it by `scripts/make_icons.py`.
* Kanban done columns: their tickets are never shown as overdue.
* Remote backups: `backups configure --append-only` and `backups prune`.
* The API tokens page shows which token issued which. The personal data export includes two-step status and chat blocks.
* Hosting: a host firewall ruleset (`deploy/nftables/bananawiki-tenants.nft`, loaded with `bananawiki firewall`).
* Hosting: expiry warning emails, renewal in a wiki's last 7 days, and `INSTANCE_DURATION_DAYS=0` to disable expiry.
* Hosting: `HOSTING_TERMS_REQUIRED_VERSION` asks accounts to accept new terms. `HOSTING_TERMS_VERSION` sets the version recorded.
* Hosting: the operator's own name and legal pages (`HOSTING_SERVICE_NAME`, `HOSTING_TERMS_URL`, `HOSTING_PRIVACY_URL`, with `_IT` and `_DE` variants).
* Hosting: `HOSTING_EVENT_RETENTION_DAYS` (default 3650) prunes the moderation log.

### Changes

* Refreshed interface for the wiki and the hosting portal: one shared stylesheet, compact navigation, page actions in one menu, New page in the sidebar.
* Accessibility: WCAG 2.1 AA contrast in both themes, keyboard access to the phone menu and scroll areas, tables that become cards on narrow screens.
* Category deletion with pages is all or nothing and follows each page's rules. The API's `page_action=delete` needs the `pages` write scope.
* Renaming or deleting an account does not edit pages. Former names stay reserved, and members rename themselves at most 3 times a day.
* Deleting an account hands its kanban boards and tickets over instead of deleting them.
* Contribution approvals refuse a proposal or page changed since the review.
* A direct message can be deleted for both participants only within 15 minutes.
* A kanban ticket holds at most 300 comments.
* Bulk delete needs a typed confirmation word and is audited.
* The daily automatic sign-out ends only the sessions of users and editors.
* Read aloud: Piper runs in its own process per job (`BW_TTS_MAX_JOB_SECONDS`, `BW_TTS_PIPER_MEMORY_MB`), and jobs that keep failing are marked failed.
* API token expiries are at most 10 years ahead. Profile pictures are limited to 4 megapixels.
* `Accept-Language: *` gets the wiki's default language.
* Italian uses "la wiki" and the wiki's own terms everywhere.
* REST API version 2.3.0.
* Container images use Debian 13 and leave Python package installers out of the runtime image.
* CI also tests Python 3.14. The contrib GPU speech server is released with BananaWiki.

### Fixes

* "Remember me" sessions last 30 idle days.
* Upgrades from 1.4 remove chains of orphaned rows instead of rolling back on every start.
* Importing a site from a wiki with another secret key turns off the two-step setups it cannot read instead of locking those accounts out.
* Uploads keep file names in any script, and downloads send an RFC 5987 `filename*`.
* Uploads named like databases no longer fail backups.
* Headings named like interface controls no longer break dialogs, ticket saves or the skip link.
* Renaming a page updates links inside builder documents.
* Changing only a page's title does not overwrite a concurrent save.
* Bulk Markdown import in update mode respects protections and reservations.
* A value with an unbalanced quote in `config/app.env` is reported with its file, line and setting.
* Platform sign-in and account linking work in Chromium and Edge.
* Re-hashing a password at sign-in keeps the account's API tokens.
* Malformed URLs, tokens and nonces get a 4xx answer instead of a server error.
* The editor keeps its unsaved-work warning after an invalid Ctrl+S, and keyboard users can tab out of it.
* The top bar theme switch saves the chosen theme.
* Hosting: accounts in several blocking states no longer bounce between pages.
* Hosting: a lapsed custom domain moves only after the new claimant verifies it.
* Hosting: a failed create, duplicate or import leaves existing data folders alone.
* Hosting: denied accounts wait one day before deletion, as documented.
* Hosting: the two-step page's "Back to sign in" ends the pending sign-in.

### Hosting

* `BW_HOSTING_TENANT_QUOTAS` is `auto` (default), `required` or `off`. Without hard quotas each wiki is held to its plan by the application. See `config get|set tenant-quotas` and `install --tenant-quotas`.
* Updates run the new release's `banana preflight` and check Docker BuildKit before anything stops. Failures are recorded with their stage and reason and are not retried every interval.
* A few wikis failing under a new release no longer roll back the update: their plugins are quarantined or the wikis isolated. `BANANA_UPDATE_UNREADY_WIKIS=rollback` rolls back instead.
* Updates rebuild the tenant image on a freshly pulled base and fall back to the cached base with `image_warnings`.
* Storage capacity is checked when a wiki is admitted or its limit raised. A wiki suspended for storage reopens by itself once it fits.
* Wikis that 1.6 left above their plan start under 1.7 and shrink back to their plan.
* Platform backups continue past a wiki that cannot be copied in full and report it.
* New reservations stay pending until provisioning completes, and interrupted provisioning can be cancelled.
* "Pause deletion countdown" pauses the purge. Deleted accounts become `~deleted-<id>`.
* Only a wiki's owner or an administrator can renew it.
* Password-reset and reminder emails have their own budget and per-address limits.
* Opening a hosted wiki never shows the portal: the portal proxies the wiki or shows a status page.
* Cloudflare proxied records work (`bananawiki proxy --tls acme|cloudflare-dns|origin-cert`, see docs/deployment.md).
* `BW_HOSTING_RECOVERY_MAX_WORKERS` also limits how many wikis restart at once after an update.
* Expired wikis show an Expired badge and the help centre matches the current labels.

## 1.6.0 — 2026-09-30

A rewrite of the whole code base. Upgrades a 1.4 installation in place, with the same database, files, secret key, environment variables and URLs. See [UPGRADING.md](UPGRADING.md).

### Security

* Sign-up approval is enforced. Pending and denied accounts reach only their status page.
* Whole-site import cannot overwrite `config.py`, the secret key or the live database.
* The guided tour's role preview does not grant administrator views.
* Every session is stored on the server and can be revoked.
* Sign-in limits per address and per account name, shared by all workers.
* Every permission of the catalogue is enforced. Category actions need write access to the categories involved.
* Only owners and superusers change administrators.
* Translations are escaped, and Markdown is sanitised with nh3 against a strict allow-list.
* `X-Forwarded-Prefix` is never trusted.
* Upload deletion is for administrators only. Images are re-encoded to drop EXIF and GPS metadata.
* The page builder requires edit rights on the page.
* Platform sign-in follows the wiki's sign-up policy, links accounts by a stable id and uses PKCE.
* Custom pages with HTML, CSS or JavaScript run in a sandboxed document.
* Deleted chat messages are erased with their attachments.
* Bot protection tokens are single-use.
* The hosting portal has no Docker access. A small root runtime agent runs tenant containers, and tenant secrets never appear on a command line.
* Federation keys and the GPU speech token are encrypted at rest.
* Outbound HTTP pins DNS answers and refuses internal addresses.
* `setup_wizard.py` and the debugger on non-loopback addresses are removed.

### New

* `bananawiki` command: `serve`, `setup-token`, `migrate`, `db`, `create-admin`, `reset-password`, `jobs`, `config check`, `export`, `import`.
* `Dockerfile` and `compose.yaml` for a single wiki behind Caddy.
* Edit-conflict detection for pages (`expected_revision` in the API).
* BananaWiki Desktop replaces the Easy Deployment App.
* Needs your attention: every approval queue counted per person, on the account menu, the admin dashboard and `/attention`.
* Notification emails, immediately, as a digest or daily, with unsubscribe links (SMTP, Brevo or Resend).
* Canvas: templates, snap to grid, alignment, groups, locks, new shapes and connections, PNG and SVG download, and a text outline for screen readers.
* Kanban: filters, checklists, work-in-progress limits, My tickets, swimlanes, and archiving of tickets and boards.
* Page builder: new blocks, starter layouts, undo and redo, device previews, accessibility checks and reusable sections. Custom pages can use the builder.
* REST API: kanban, canvas and attachment endpoints, outgoing webhooks, `Idempotency-Key`, `ETag` and `If-Match`, rate-limit headers, an OpenAPI description, and a Python client (`bananawiki.sdk.client`).

### Architecture

* One Python package, `bananawiki` (`core`, `wiki`, `hosting`, `ops`, `desktop`, `sdk`), with `pyproject.toml`. The root keeps only the entry points existing servers run.
* An application factory with a documented request pipeline. Views are private by default.
* Every feature is a package declaring a `Feature` with one on/off switch that routes, menus, jobs and permissions respect.
* One database session per request, explicit transactions, versioned migrations (schema 3 to 4) and an automatic database copy before a schema upgrade.
* One scheduler for background jobs with leases in the database. `bananawiki jobs run` for cron.
* Events are emitted by the services after commit.
* A typed configuration read once from `BW_*` variables. Invalid values stop the start with a clear message.
* No inline scripts. Static JavaScript modules under a nonce-based Content Security Policy.
* Third-party plugins use the same `Feature` API. 1.4 plugins run through an adapter.
* The `banana` lifecycle controller uses only the standard library and reads 1.4 configuration and packages unchanged.
* A new test suite, including one that boots 1.6 on a real 1.4 instance.

### Performance

* One SQLite connection per request instead of 20 to 30. Permissions are resolved once per request.
* Full-text search (FTS5) with visibility applied in SQL.
* Attachments are stored on disk once and streamed.
* Chats fetch only new messages. Kanban and canvas sync incrementally.
* Indexes on the foreign keys used on hot paths.
* Updates snapshot the data online, so downtime does not grow with the data. Old packages, releases and images are pruned.

### Removed

* Obsidian sync, Banana Mode, joke audio conversion, the BananaChat installation modes, `setup_wizard.py`, the `legacy_redirect` daemon, the portal's Arabic mirror mode and demo wiki spawning. Other behaviour changes are listed in [UPGRADING.md](UPGRADING.md).
