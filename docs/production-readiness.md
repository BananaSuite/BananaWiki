# Production validation — 2 October 2026

The interface refresh and verified application fixes pass the checks below.
This is a source and disposable-container assessment; the changes have not
been deployed. It does **not** certify a live public hosting service as fully
production-ready. Unfixed distribution advisories and deployment checks remain.
The 2 October follow-up adds speech-encoder restrictions and rescans the existing
disposable tenant image. Earlier browser, wheel and container checks below are
the 1 October release evidence unless explicitly dated otherwise.

## Interface

The final clarity pass makes New page a direct sidebar action, keeps everyday
Customize controls before optional disclosures, and puts mobile article
contents before the text. Editor Save/Cancel actions stay in the header and
footer without covering fields. Hosting uses compact wiki records and opens
occasional setup/bulk controls on request. Focused tests and real desktop/phone
browser checks verified these presentation changes separately from the full
application hardening suite below. Landing screenshots were refreshed again.

The follow-up maintainability pass shares editor actions, dialog buttons,
hosting textarea rendering and incoming transfer markup. Focused tests and
browser checks cover labels, help-text associations, submitted values,
keyboard controls, native validation and saving with JavaScript disabled.
The editor groups formatting and insertion tools with consistent spacing.

Ordinary wiki and hosting pages share a 1120px content frame. Article prose
uses an 800px reading measure; editors, boards and canvases have wider
workspaces. Authentication and dialogs remain intentionally narrow.
Shared form controls, buttons, section dividers, disclosures and keyboard
focus behavior replace inconsistent page-specific layouts and bulky cards.
Both apps now serve the same design-system source from `core/static`, with
the existing static URL, content hashes and conditional caching. Settings
use explicit form sections; shared component rules replace stacked overrides.
Editor/browser regression checks cover native validation, metadata-only draft
warnings, out-of-order replies, search dismissal, duplicate loading and
serialized reorder saves.

Browser checks covered desktop and small screens, English and Italian,
both themes, enlarged text, high contrast, labels, duplicate IDs, overflow
and keyboard interactions. All five landing screenshots were recaptured
from disposable sample data at 1280×800. The landing's existing visual style
was retained. Its nine HTML pages passed link/asset checks and 36 browser
checks with disclosures expanded and screenshots loaded.

## Application and packaging checks

* The full 2 October Python regression suite passed **2,116 tests**, with 17
  skips. A follow-up using the installed Caddy binary passed **29 Caddy tests**
  and resolved eight of those skips. All **seven ownership checks passed under
  root** against the current source in a disposable container. Together these
  checks validated **2,131 unique cases**. Two remaining Caddy checks need its
  optional Cloudflare DNS module. The earlier focused hardening checks passed
  all 109 new account-authority, request/storage and Markdown cases, plus 343
  hosting/runtime cases. The sole warning comes from a test deliberately
  checking Pillow's protection against oversized image dimensions.
* Both application factories and an extracted wheel serve the canonical
  stylesheet. Its source/hash, security headers, ETag/304, HEAD, configured
  cache lifetime, missing paths and plugin blueprint behavior were verified.
* Ruff, ShellCheck, JavaScript syntax, dependency compatibility and diff
  whitespace checks passed. The final distribution wheel built successfully
  and includes the new migration, shared templates and feature assets.
* Installed development/server environment: 54 audited packages, no known
  vulnerabilities. Python runtime dependency audit: 47 packages, no known
  vulnerabilities. The previously verified hosting/speech installation path
  and hardened tenant image also had no known Python dependency findings.
* Container checks exercise an arbitrary UID, read-only root filesystem,
  dropped capabilities, secret-file permissions, security headers, startup
  and audio encoding. Runtime checks cover seeding, snapshots, live user
  listing, CPU/memory/process restrictions, mount boundaries, task deadlines,
  timeout cleanup and subsequent recovery.
* CI now checks both dependency installation paths, runs the ownership tests
  as root and rejects high/critical image vulnerabilities with available fixes.

Verified account fixes include atomic password-attempt reservations, credential
and authority checks inside sensitive write transactions, and revalidation of
active impersonation after role or superuser changes. A password reset cannot
be undone by a request verified against the old password or create a new
session from it. Hosting OAuth rejects blocked accounts when redeeming codes,
verifying tokens or linking accounts; password changes revoke access tokens
and unused authorization codes.

Request and storage fixes include body limits before JSON/CSRF parsing,
safe rejection of malformed Unicode tokens, serialized upload quotas, durable
chat usage limits and transactional ownership/membership checks. Concurrent
legacy-blob downloads use independent staging files. A file deleted during
download preparation returns 404. Imports reject file/directory path collisions
before staging; archive copies and storage walks respect size and time limits.

Markdown checks bound repeated delimiter work, nesting and heading counts,
including malformed fences, entities, escapes, line breaks and mixed list/quote
headings. Complex source returns directly as escaped text. Valid ordinary
articles and large literal code retain their contents. Text counts as fenced
code only where the parser itself extracts it, after its own whitespace
normalisation, so fences that merely look paired cannot hide text from these
checks; the parser's search for fences that never close, repeated to the end
of the page for each one (and, after a legacy `hl_lines="…"` value, from
every later line ending in its quote), counts as work too. Fences forward only
supported display options: Boolean line-number/highlighting flags, tab sizes
up to 16, line-number starts up to 1,000,000 and at most 256 highlight lines.
Unknown lexer/formatter options are discarded. Automatic language detection
uses at most 4,096 characters; snippets over 65,536 characters remain literal
code, preventing excessive highlighting output.

The same checks weigh the number of blocks, with code blocks and tables
counting more than paragraphs. Shortcode attributes are read only within
their own paragraph, so an unclosed `[[video` stays text in linear time; a
document embeds at most 200 players and boards, and later shortcodes stay
text; video links over 2,048 characters stay links. Only the first `[TOC]`
marker becomes a table of contents. HTML that would exceed 16 million
characters (reference links repeating a long address) is shown as escaped
source. Builder page lists show at most 48 pages per document; their excerpts
render only the first 4,096 characters of each page, once per request, under
smaller work, block and heading allowances (at most 200 headings). With
Markdown 3.6, the oldest release the requirements allow, a page start that
combines many headings with raw HTML tags or entities renders several times
slower than with current releases (about 0.3 s instead of 0.05 s per excerpt
in the worst case measured), so deploy a current release.

Runtime fixes bound agent connections, task waiting, inspection/execution and
subprocess output. Timed-out or flooding tasks are reaped and subsequent tasks
can recover. Caddy reloads retry across agent restarts; stopped-tenant networks
are released only after the route removal is acknowledged, preventing stale
routes from reaching a different tenant after address reuse. Outbound HTTP
deadlines cover DNS, connection and TLS. Restore preflight protects running
tenants, and cancelling a portal restore prevents its upload.

The 2 October speech checks passed **40 focused TTS tests**, including real PCM
WAV-to-MP3 generation and MP3 tempo conversions at 0.5x, 1x, 1.25x and 2x.
Unrelated input formats are refused before starting FFmpeg. Inputs select only
local MP3/WAV demuxers and PCM/MP3 decoders; network protocols, video and subtitles
are excluded, metadata is stripped and conversion uses one thread per stage.
Diagnostics are bounded to 64 KiB; flooding and timed-out processes are reaped,
and a subsequent conversion succeeds.

## Image advisories

The images use Debian 13 and apply available distribution updates during
the build. Unused Python installers and their vulnerable bundled components
are removed from the runtime image. The managed updater builds the tenant
image of every release on a freshly pulled base image and repeats the
upgrade and package installation instead of reusing cached layers (with the
registry unreachable it falls back to the cached base image and reports
`image_warnings`). Between releases the running image does not change
(`status` shows `tenant_image_built`); single-wiki images get later fixes
only when rebuilt with `--pull --no-cache`.

The hardened tenant image scan reports **zero high/critical Python findings**
and **zero high/critical OS findings with available distribution fixes**.
It still reports **47 distinct high/critical OS advisories without a
distribution fix**, repeated across 213 package findings, including media
libraries. Moving from Debian 12 and applying updates reduced the distinct OS
count from 86 to 47. These findings are not a demonstration of an application
exploit, but they prevent an unconditional security certification. Rebuild and
rescan as distributor fixes become available; review exposure before launch.

A fresh scan on **2 October 2026, 14:38 UTC** of the existing
`bananawiki-production-review:tenant` image confirmed these same 47 distinct
advisories, 213 package findings and zero available distribution fixes. This
scan used an updated vulnerability database and found no high/critical Python
findings. It inspected the previously built image; today's speech changes have
source and executable regression evidence and require a normal image rebuild
when deployed. The encoder restrictions reduce application exposure but do not
remove or relabel the outstanding OS findings.

## Upgrade behavior

Schema 5 adds a 24-hour chat upload usage ledger with an automatic database
backup before migration. It backfills retained recent uploads; uploads deleted
before the upgrade cannot be reconstructed. Deleting messages no longer resets
the daily allowance. The ledger expires and is excluded from account exports
as a short-lived technical rate-limit counter.

Schema 6 records whether an owner or superuser imposed each suspension.
Suspensions recorded before the upgrade count as imposed by one: a suspended
administrator can no longer lift them, an owner or superuser can.

Hosted third-party Python plugins are disabled by default, with their files
and settings preserved. Trusted operators can explicitly opt in using
`HOSTING_ALLOW_TENANT_PLUGINS=1`; quarantine still disables them. Runtime
images omit package installers, so custom dependencies belong in the image
build. See [UPGRADING](../UPGRADING.md).

## Live deployment requirements

Disposable Docker packet tests denied connections to a peer tenant and to a
host listener outside the tenant subnet. A listener on the bridge gateway was
reachable. An internal Docker bridge alone therefore does not isolate host
services. Configure and test host INPUT rules that allow established replies
to Caddy connections and a narrow allowlist of intentionally configured host
endpoints, then reject other new tenant-to-host connections. Rules
must cover managed tenant bridges when they are created or recreated;
DOCKER-USER/FORWARD rules alone do not cover host-bound traffic. Keep management
listeners bound to loopback. Disposable checks confirmed that removing the
bridge address with Docker isolation flags also breaks current Caddy access.

Public hosting additionally needs enforced per-tenant byte and inode quotas,
reserved storage for the portal/backups, verified HTTPS/proxy settings, and a
complete backup restore on the actual host. The managed systemd install,
update and rollback must also be exercised there. Application quotas and
disabled plugins do not replace these host controls. See
[hosting isolation](hosting.md#isolation) and [deployment](deployment.md).
