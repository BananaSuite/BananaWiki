# Production validation — 3 October 2026

The reviewed source is ready for the documented production profiles: a single
managed wiki and Linux x86_64 hosting with Docker, IPv4 tenant bridges, the pinned
quota-safe seccomp policy, a dedicated XFS volume with project accounting and
enforcement, and the documented host INPUT allowlist.

**Automatic creation by ordinary public accounts now has a built-in pre-seed
storage gate.** The root runtime agent assigns finite byte and inode quotas
before any seed, import, copy, restore or launch. Unsupported filesystems,
disabled enforcement, quota drift, uncertain container state and insufficient
capacity refuse admission. New reservations remain pending until provisioning
finishes; recovery and manual start refuse incomplete or failed provisioning.

The final review also repaired renamed live task handling and host-service
filesystem setter permissions. Actual mounted inode checks prevent a renamed
folder from hiding a writer; pinned descriptors prevent replacement during
repair. Inert container guards wait for actual mount verification before
tenant-controlled startup. Hosting portal and maintenance units cannot access
host process handles or invoke filesystem quota setters.

Real disposable systemd, Docker and XFS acceptance covers ordinary HTTP creation,
pre-seed quota assignment, byte/inode exhaustion with ample physical space,
negative admission, pending provisioning, renamed-task cleanup, mount
substitution refusal and the exact final image. Managed install, update,
rollback and backup restoration also retain their earlier systemd evidence.
Images retain disclosed scanner findings and the component/consumer assessment
below. No particular operator's production host was deployed or certified:
persistent firewall rules, dedicated storage/headroom, HTTPS/DNS, credentials,
recovery and optional integrations require that host's documented acceptance.

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
and keyboard interactions. All seven wiki landing screenshots were recaptured
on 3 October from disposable sample data at 1280px wide, using full document
height for long pages. The landing's existing visual style
was retained. Its nine HTML pages passed link/asset checks and 36 browser
checks with disclosures expanded and screenshots loaded.

## Application and packaging checks

* The complete final Python 3.14 suite passed **2,392 cases**, with
  **11 explicit root/tool skips**, against unchanged runtime,
  tests and build inputs. A separate disposable root container covers ownership
  cases; verified custom Caddy covers Cloudflare-module cases. Deployment
  FFmpeg cases retain exact-binary native acceptance below. The deliberate
  Pillow oversized-image warning remains expected. The final release ledger
  records every run, skip and source snapshot rather than equating skips with
  passes.
* Both freshly built distributions passed **327 cases each** outside
  the checkout. All **835 runtime files**
  matched source in the installed wheel and extracted source distribution,
  including both SDKs, translations, migrations, assets, built-in guide pages,
  starter-kit files and the pinned seccomp policies/license. README-derived
  package metadata and every source-mapped artifact byte were checked.
* Python **3.11.17**, the declared minimum, passed **289 cases** in the final focused runtime,
  quota, lifecycle, guard and admission selection in a fresh container using
  the 49 exact runtime pins. Its root-only peer check is explicitly skipped.
  The detailed minimum-run report records the selection and exact dependency versions.
* Ruff, dependency compatibility, diff whitespace, shared backup/lifecycle,
  JavaScript syntax and ShellCheck evidence passed. Earlier dependency audits
  found no known Python runtime advisories after upgrading inherited build
  tools. Managed installation and CI require setuptools at least 83; see the
  [publisher advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c).
* Both factories and the installed wheel serve the canonical stylesheet.
  Source/hash, headers, ETag/304, HEAD, cache lifetime, missing paths and plugin
  blueprints are covered. Bilingual installed onboarding and starter-kit
  download run from actual packaged files.
* Both rebuilt images pass startup and HTTP probes as **UID 4242**, with a
  read-only root, all capabilities dropped, no network, **384 MiB memory,
  0.5 CPU and 96 processes**. Secret files remain mode 0600; security headers,
  shared-asset caching, malformed JSON, API errors, current source hashes and
  absent runtime installers were checked. Runtime tests also cover seeding,
  snapshots, live user listing, mount boundaries, task deadlines, timeout
  cleanup and subsequent recovery.
  Both final images match all **839 runtime/NOTICE/LICENSE source files** and
  use the exact quota-safe seccomp profile plus both IPv6-disable sysctls.
  Ordinary ioctl operations remain available; quota-changing calls return EPERM,
  and IPv6 addresses, including link-local addresses, are absent.
  An earlier accepted tenant image passed **185 export, asset, API, onboarding, plugin-kit,
  encoder and sandbox checks**, with one root-only peer-identity skip. The
  earlier single-wiki image passed all **11 encoder
  hardening cases**, including real speech-speed conversion.
* CI now checks both dependency installation paths, runs the ownership tests
  as root and rejects high/critical image vulnerabilities with available fixes.

Verified account fixes include atomic password-attempt reservations, credential
and authority checks inside sensitive write transactions, and revalidation of
active impersonation after role or superuser changes. A password reset cannot
be undone by a request verified against the old password or create a new
session from it. Hosting OAuth rejects blocked accounts when redeeming codes,
verifying tokens or linking accounts; password changes revoke access tokens
and unused authorization codes.

A fresh review found overlapping portal creation requests could exceed the
account and platform instance-count limits. Admission now rechecks current
settings and reserves the instance row in the same cross-worker SQLite write
transaction. Slow filesystem usage sampling remains outside that lock; it is
an admission estimate, while host quotas provide hard storage enforcement.
The committed row reserves capacity during provisioning and after a worker
interruption. Successful failure cleanup releases the reservation. If cleanup
fails, the name, port and capacity remain reserved: a verified stop marks the
row stopped, while an unverified stop retains its running state and logs that
the container may remain active. The operator must inspect and retry termination
before treating those resources as free. Real HTTP/SQLite regression checks
cover overlapping account/platform requests, current policy changes, in-flight
provisioning, successful cleanup, both cleanup/stop failure outcomes and a staged
operator-owned creation/transfer workflow. Their synthetic runtime does not
claim that real filesystem quotas or firewall rules were provisioned.

Request and storage fixes include body limits before JSON/CSRF parsing,
safe rejection of malformed Unicode tokens, serialized upload quotas, durable
chat usage limits and transactional ownership/membership checks. Concurrent
legacy-blob downloads use independent staging files. A file deleted during
download preparation returns 404. Imports reject file/directory path collisions
before staging; archive copies and storage walks respect size and time limits.

Both applications reject lone Unicode surrogates, non-finite JSON numbers and
more than 64 nested objects/arrays before database writes or recursive consumers.
The same checks cover canvas/board, language/theme, plugin and migration uploads,
hosting archive manifests and federation replies. Valid Unicode and ordinary
nested documents retain their contents.

Keyed API writes now commit their database changes, audit entry and replay record
together. Server errors, failed response recording and interrupted workers roll
back the write; bulk deletion keeps attachment files until commit. Oversized
answers keep a status-only record so a retry cannot repeat a successful write.
Replays apply the owner's current role and resource access after privacy,
category or ownership changes, and preserve page/canvas ETags. Routing, server,
storage and operator-maintenance failures preserve the API JSON error contract
with a request ID and generic details; storage failures do not need another
database read to render their answer.

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

The latest source speech check passed **57 TTS cases**, with four real-FFmpeg
cases additionally exercised in both rebuilt images. Four new local/GPU engine
checks prove telemetry is disabled before native imports by default while an
explicit operator setting is preserved. Real PCM WAV-to-MP3 generation and
MP3 tempo conversions cover 0.5x, 1x, 1.25x and 2x.
Unrelated input formats are refused before starting FFmpeg. Inputs select only
local MP3/WAV demuxers and PCM/MP3 decoders; network protocols, video and subtitles
are excluded, metadata is stripped and conversion uses one thread per stage.
Diagnostics are bounded to 64 KiB; flooding and timed-out processes are reaped,
and a subsequent conversion succeeds.
Earlier accepted images also passed **49 real conversions each**, covering six PCM
formats and MP3 at seven speeds from 0.5x to 2x, both MP3 decoders and the GPU
service's stereo WAV pipe. Independent decoding verified the resulting sound,
duration and retained pitch. Those images also ran real English, Italian
and cached English Piper synthesis, with independently decoded, non-silent MP3
speech. These checks run through actual container exec under the exact quota-safe
profile, IPv6 disabled, 384 MiB/0.5 CPU/96-process limits and telemetry disabled;
no OOM kill occurred.
The GPU service protocol and CPU fallback were checked; physical CUDA
acceleration was not available in this assessment environment.

## Image advisories

The images use Debian 13 and apply available distribution updates during the
build. They replace the broad distribution FFmpeg stack with **FFmpeg 9.0.2**,
built from the signed upstream archive with a pinned SHA256 and a minimal
speech-focused configuration. Actual unused Debian utilities
and media dependencies are removed through apt/dpkg; retained package metadata
and before/after inventories remain available. Native Python/Piper libraries,
fonts, GNU timeout and the default HTTPS trust store remain verified. Runtime
Python installers are removed. Matching FFmpeg and LAME source archives,
licenses, configuration, signing evidence, executable hashes and a supplemented
software inventory ship in `/usr/local/share/bananawiki/media`.

The managed updater builds the tenant image of every release on a freshly
pulled base image and repeats the distribution upgrade and the Python package
installation of the final stage instead of reusing cached layers (the FFmpeg
and ACL stages are rebuilt only when the pull brought a new base image; with
the registry unreachable, or when the build fails on the new base image, it
falls back to the previous base image and reports `image_warnings`). Between releases the running image does not change
(`status` shows `tenant_image_built`); single-wiki images get later fixes
only when rebuilt with `--pull --no-cache`.

The final images are `bananawiki-review-20261003:tenant-release`
(`sha256:c96f91b06c2fca6c0794bd7dafaf12c2ec7c711186178ce2aa34c702fd0d4be7`)
and `bananawiki-review-20261003:wiki-release`
(`sha256:30f73626fab44e28406a9c09c0aacf2c7c9827a40409c29ede4f9a12ed3449c6`).
The earlier conversion/Piper checks above are retained native evidence, not
reruns against these tags. Both final images retain the identical FFmpeg
executable SHA256 `d06b7813e9b8a1d7fbe08e9ff31c3ac44e9bb375929b2629d86466a172ba2e50`
and verified unchanged native package versions; their fresh startup, source,
sandbox and scanner checks are recorded separately.
The exact-image scans on **3 October** report **seven distinct HIGH OS
advisories across nine package findings**, **zero CRITICAL findings**, and
**zero HIGH/CRITICAL Python findings**
in each image. The fresh pre-change tenant scan had 47 distinct advisories and
213 package findings. The residual findings cover Python-required ncurses,
UUID and retained ACL/systemd libraries; they remain recorded. The raw Trivy
trixie records contain **zero nonempty FixedVersion fields**; that describes
the scanner's distribution database, not the absence of upstream or sid fixes.
CI's
`--ignore-unfixed` gate checks only findings with available fixes; the full
scans retain every finding, with package metadata intact. The enriched
107-component inventory reproduces all 97 findings across all severities.

The applicability review traces six of those advisories to mount/nsenter,
infocmp or systemd-homed code that is absent from the shipped images. Their
source-package findings remain visible on retained UUID, ncurses and systemd
libraries. The seventh concerns ACL pathname APIs. The images now install
authenticated Debian sid **libacl1 2.4.0-1** and namespace-compatible
**tar 1.35+dfsg-6**, keeping trixie libc/coreutils. The
[Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-54369)
marks ACL 2.4.0-1 fixed. Signed upstream source, authenticated package indexes,
hashes, licenses and matching ACL/tar/coreutils source ship under
`/usr/local/share/bananawiki/acl`.

The new trusted-descriptor/no-follow/empty-path APIs pass actual symlink and
invalid-flag tests, including an ancestor replaced after opening its directory.
Legacy pathname APIs deliberately retain their symlink-following compatibility
contract, as the [upstream maintainer explains](https://www.openwall.com/lists/oss-security/2026/06/29/1).
Retained manual cp/install/mv/tar consumers still import legacy pathname calls;
no advertised automatic application path invokes them. Actual copying/archive
roundtrips verify modes, ACLs, links and sparse files, and a separate test proves
that legacy following remains. This upgrade and supported-path assessment do
not make privileged manual operations on attacker-controlled paths safe.
Trivy still labels the installed ACL version because its trixie record lacks a
fixed version; the raw finding is preserved. Keep the documented runtime
restrictions and separately review new consumers or privileged derived images.

Trivy's generic CycloneDX input does **not** provide vulnerability coverage for
the source-built FFmpeg component. The independent review checks the signed
release against [upstream security information](https://ffmpeg.org/security.html),
the NVD version query, and all 16 FFmpeg HIGH/CRITICAL component paths reported
by the baseline scan. Every affected baseline component is disabled in the
actual compiled profile. The source/build inventory is explicitly supplemented;
an empty generic-component scanner result is not called a clean audit. Continue
monitoring upstream releases and advisories alongside distribution packages.

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

## Deployment acceptance

A disposable Debian 13/Python 3.12 systemd host exercised the real Manager/System
Wiki lifecycle: service-account creation, wheels-only setup and source sealing,
generated units, enable/start and HTTP readiness. A consistent backup retained
pages and an upload. Update, rollback to the previous release and restoration
into a fresh installation root restored the original database and upload. An
intentionally broken candidate wrote a page, deleted the upload and failed to
boot; automatic rollback restored the prior revision and both data items,
restarted the service and removed transaction/maintenance markers. Source
updates used a synthetic local repository over key-verified loopback SSH.

The same isolated host ran the actual generated root agent, unprivileged portal
and maintenance units. The portal had no Docker-group membership. A normal
HTTP login with signed form timing and CSRF created a tenant; seed ran in a
stopped sandbox, and user listing and a database snapshot ran through the real
agent socket and container exec. The tenant used the final image, exact seccomp
policy and IPv6-disable settings. This hosting-unit acceptance reused the
independently built image; it is distinct from another full hosting-mode
Manager install/image-build lifecycle.

Nested Docker packet tests verified tenant-to-tenant isolation and demonstrated
the bridge-gateway host-listener exposure. A dedicated-host INPUT policy
matching `br+` allowed established replies and one explicit integration while
denying an unlisted host listener. Host-to-tenant HTTP still worked, including
after the tenant bridge was deleted and recreated. Docker's gateway-removal
flags broke the current host proxy path and are not substituted for INPUT
policy. DOCKER-USER/FORWARD rules alone do not cover host-bound traffic.

A separate daemon with IPv6 default network options created IPv6-enabled bridges
by default; explicit IPv6-off creation overrode that setting. The source also
checks and refuses actual enabled or uninspectable networks. Its container
sysctls disable IPv6, including loopback and link-local addressing. An
intentionally enabled fixture demonstrated that IPv4 INPUT rules leave both
ULA and link-local host listeners reachable; an IPv6 INPUT policy with neighbor
discovery allowances denied the unlisted listeners and preserved the allowlist.
Such a derived IPv6 tenant runtime is outside the supported profile and needs
its own IPv6 validation. Public Caddy IPv6 clients are unaffected.

Real XFS project accounting/enforcement applied separate byte and inode caps.
A tenant exhausted an 8 MiB project after 6,750,208 probe bytes; another exhausted
its 128-inode cap after 119 probe files. Tenant HTTP stayed healthy and reserved
portal storage stayed writable. Before repair, an unprivileged owner escaped
the project by calling FSSETXATTR. The pinned default allowlist now excludes
that call and both 32/64-bit SETFLAGS layouts. All 12 native upper-bit/sign
aliases and an actual static ELF32 process received EPERM; the project ID stayed
unchanged. Missing/tampered policies and unverified host architectures fail
closed. The installed policy preserves Docker's other default syscall rules,
with a test proving that every remaining 32-bit ioctl command is covered once.
Running tasks inherit the verified container policy; outdated settings are
refused and replaced during start/recovery even when image/application settings
match.

Each hosting deployment must provide dedicated XFS project accounting and
enforcement, trusted finite quota ceilings, portal/backup capacity and the INPUT
allowlist before opening access. The runtime provisions and verifies each
tenant's limits before its first task, including ordinary public creation.
Its aggregate admission budget and free-space checks retain a positive
configured reserve; project quotas do not create a hierarchical filesystem-wide
reserve or budget independent operator writes. Follow the volume/headroom and
monitoring requirements in [hosting isolation](hosting.md#isolation).

Keep management listeners on loopback, verify effective rules after reboot, and
rehearse restoration with that deployment's real data and retention settings.
Configure the chosen public HTTPS/DNS method and credentials, then verify public
issuance, proxy trust and intended integrations. Actual external DNS credentials
and physical CUDA hardware were not supplied here. See
[hosting isolation](hosting.md#isolation) and [deployment](deployment.md).
