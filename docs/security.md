# Security

How BananaWiki protects accounts and content, what it trusts, and what you
should configure. To report a vulnerability see [SECURITY.md](../SECURITY.md).

## Trust model

* **Owners and superusers are fully trusted.** Only they can install or
  enable a third-party plugin (Python code that runs with the wiki's own
  rights), export the whole site (every password hash and private message)
  or import an archive (which replaces every account). Each gives complete
  control of the wiki; the pages say so where the decision is made, ask for
  the password again, and log it.
* **Administrators** run the wiki: accounts, roles, settings and content.
  They are kept below owners and superusers everywhere: only owners and
  superusers change administrators (their role, password, profile,
  sessions), only an owner changes an owner, only a superuser changes a
  superuser or impersonates an administrator, and the addresses of
  accounts an administrator may not change (sign-ins, audit entries, the
  chat messages they sent) are hidden from them. They cannot
  switch the audit log off or shorten how long it keeps entries. This
  protects against a careless or compromised administrator account; give the
  role only to people you trust with the wiki's content and members.
* **Everyone else** gets exactly what their role, permissions and category
  access allow, in the web interface and in the REST API alike.
* **Hosting operators** trust neither tenant wikis nor their administrators;
  hosted third-party plugins are disabled unless the operator sets
  `HOSTING_ALLOW_TENANT_PLUGINS=1`. Enabling plugins allows arbitrary Python
  inside the tenant container and requires tested host resource controls;
  see [hosting](hosting.md#isolation). Port and onion mode do not isolate
  cookies between the portal and the wikis; use subdomain mode when wiki
  administrators are not trusted ([addresses](hosting.md#addresses)).

## Accounts and sessions

* Passwords: 8 to 1024 characters, stored as Werkzeug scrypt hashes (PBKDF2
  where scrypt is unavailable, or with `BW_PASSWORD_HASH_METHOD=pbkdf2`).
  A new password (sign-up, settings, the forced change, administrators
  creating accounts or setting passwords, the REST API, the command line)
  may not contain the user name or the part of the account's email address
  before the `@`, and may not be one of the 10,000 most common passwords
  (bundled in `bananawiki/core/data/common-passwords.txt`, compared without
  case). Existing passwords keep working: the policy is checked only when a
  password is set.
  Sign-in compares against a dummy hash for unknown names so timing does not
  reveal which accounts exist.
* Every sign-in creates a row in `user_sessions` holding the SHA-256 of a
  random token; the signed cookie carries the token. A request is signed in
  only when the cookie's token matches an active, unexpired, unrevoked row of
  the same account. Revoking the row ends the session at once.
* Sessions end after 7 days, or 30 with **remember me**; without remember me
  the cookie is a browser-session cookie. The cookie is `HttpOnly`,
  `SameSite=Lax`, and `Secure` on HTTPS (or as `BW_SECURE_COOKIES` says).
* A `Secure` session cookie is named `__Host-<name>` (`__Host-bw_session`,
  `__Host-bw_session_<slug>` on hosted wikis), with `Path=/` and no
  `Domain`. Browsers refuse such a cookie from any other host, so a wiki on a
  sibling subdomain cannot plant a session (its own account, or a half-done
  portal sign-in) for another wiki. Plain HTTP keeps the plain name (desktop
  app, local use) and has no such protection.
* After the upgrade to this naming, a session under the plain name is moved
  to the prefixed name on its first HTTPS request, so nobody is signed out,
  and the plain cookie is expired; signing out clears both. Only a cookie
  signed before the wiki first started with this naming moves (the time is
  kept in `.host_cookie_since` in the instance folder): the wiki never issues
  the plain name over HTTPS after that, so a newer one can only have been
  planted, and it is ignored. A request with two plain-name cookies is never
  moved either.
* Sessions are revoked when the password changes (other sessions), when an
  administrator resets the password or suspends the account, on "sign out
  everywhere" (**Settings → Sessions**), on **Admin → Sessions** (one session
  or everyone), and by the optional daily automatic sign-out.
* **Single session** (`session_limit_enabled`): a new sign-in ends the
  previous session; the signed-out browser is told why.
* **Sign-in limits:** at most 20 failed attempts per address, 8 per account
  name from one address, and 100 per account name from all addresses
  together, in 15 minutes, shared by all workers; an IPv6 client counts as
  its whole /64 network. A browser that has signed in to the account before
  carries a device cookie (`<session cookie name>_device`, `__Host-` on
  HTTPS, signed with the secret key and the account's device epoch, which a
  new password and every change to two-step verification retire) and gets
  its own budget of 8 instead of the shared 100: strangers cannot lock the
  owner out. A successful sign-in clears only its own account-and-address
  counter. Passwords stored with an older hash method or cost are re-hashed
  with the configured one at the next sign-in; the password stays the same,
  so the account's API tokens and device cookies keep working.
* **Two-step verification** (**Settings → Two-step verification**, optional
  for every account): after the password, or after the hosting portal's
  sign-in, the wiki asks for a 6-digit code from an authenticator app (TOTP,
  30-second steps, each code accepted once) or one of ten single-use
  recovery codes. Turning it on takes the password and a code and signs out
  the account's other sessions; turning it off or making new recovery codes
  takes the password and a code. The pending sign-in lasts five minutes,
  lapses when the password changes and ends with **Back to sign-in** on the
  code page (a form, so the password step is not left passed in the
  browser). Codes are limited to 10 wrong attempts in 5 minutes and 50 a
  day: per account for browsers that never signed in to it, and separately
  for each browser with its device cookie, so someone who knows the password
  cannot use up the owner's attempts. Turning two-step verification on, off
  or resetting it retires the account's device cookies (the browser making
  the change gets a new one), and while it is on a device cookie is issued
  only after both steps, so cookies collected while it was off cannot
  multiply the limits. After 10 wrong
  codes in a day from the same browsers, each further code waits 30
  seconds, then twice as long each time, up to an hour. The account
  accepts at most 100 wrong codes a day from all browsers together, more
  than the browsers without a device cookie can reach on their own. After 5 wrong codes at sign-in in
  a day (all browsers together) the owner gets a security notice, at most
  one a day, also by email when the wiki sends decision emails and they have
  not turned them off: someone else may know their password. Owners and superusers who use it
  can require it of administrators (**Admin → Settings → Security**):
  administrators without it are sent to set it up before anything else.
  Owners and superusers can reset it for an account they may manage
  (**Admin → Users**; every session of the account ends);
  `bananawiki reset-two-step NAME` resets any account from the server. On a
  hosted wiki, the portal's **Reset admin password** turns it off for the
  account it resets (and records that in the wiki's audit log). API
  tokens and impersonation do not involve it.
* **Sign-up:** invite codes, open sign-up (optionally until a date), and
  administrator approval, which is enforced everywhere: pending and denied
  accounts can only see their status page. Bot protection adds a honeypot
  and a signed, single-use form token that must not be sent faster than
  `BW_MIN_FORM_SECONDS`.
* **Forced steps:** "change password at next sign-in" and the first-run
  wizard block every other page until done.
* **Impersonation** (**Admin → Users → Impersonate**) keeps the
  administrator's own session, is logged, and needs a superuser for
  administrators and owners. While impersonating, lasting credentials of the
  account (API tokens, the userbot key) cannot be created.
* **Setup token:** the first account needs the token from
  `bananawiki setup-token` (or `BW_SETUP_TOKEN`), accepted only in the posted
  form, never in the URL.

## Access control

* Views are **private by default**: without a session every page redirects
  to sign-in, except the sign-in, sign-up and setup pages, health checks,
  `/source`, published custom pages, and, while **public mode** is on, the
  read-only views (pages, categories, search, announcements, public boards and
  canvases, read-aloud audio). Public mode can have an end date and can be
  forbidden by a host (`BW_FORBID_PUBLIC_MODE`), which also keeps published
  custom pages for signed-in members.
* Every action checks a permission from the [catalogue](permissions.md) and
  the object itself: this page, this category, this board. Objects you may
  not read answer 404, so their existence is not revealed.
* A permission that belongs to a switched-off feature is never granted.
* Maintenance mode lets only administrators in.

## Requests

* **CSRF:** every POST, PUT, PATCH and DELETE needs the session's CSRF token
  (form field `csrf_token` or header `X-CSRF-Token`). Only the REST API with a
  bearer token is exempt, and it refuses requests carrying an `Origin` from
  another site.
* **Rate limits:** 300 requests per minute per client address (IPv6: per
  /64) and worker,
  plus per-action limits (editing, uploads, search, chats, the API …), per
  account or, for visitors and group joins, per address (IPv6: per /64).
  Sign-in and sign-up limits are stored in the database, so they hold across
  all workers.
* **Body limits:** JSON bodies 2 MiB, form fields 2 MiB in memory; file
  uploads stream to disk and are checked against their own limits (see
  [configuration](configuration.md#limits)).
* **Proxies:** with `BW_PROXY_MODE=1` one hop of `X-Forwarded-For`, `-Proto`
  and `-Host` is trusted; `X-Forwarded-Prefix` never.
* **Slow clients:** the shipped Caddy configuration allows 10 s for request
  headers and 5 minutes for a request body. A response is abandoned once a
  write to the client has waited `BW_WRITE_TIMEOUT` seconds (300), so clients
  that stop reading downloads cannot tie up the worker threads for longer;
  readers that kept taking in 256 KiB to 4 MiB per period, depending on their
  buffers, were not cut off in tests (see
  [deployment](deployment.md#reverse-proxy)). A client that reads slowly on
  purpose still holds a thread for the whole download: a proxy's minimum
  rate (Caddy's `write_idle <duration> <min_rate>`) covers that.

## Response headers

Every HTML response carries:

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-…';
  style-src-elem 'self' 'nonce-…'; style-src-attr 'unsafe-inline';
  img-src 'self' data: blob: https:; media-src 'self' blob: data:;
  font-src 'self' data:; connect-src 'self';
  frame-src 'self' https://www.youtube-nocookie.com https://www.youtube.com https://player.vimeo.com;
  frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self';
  manifest-src 'self'; worker-src 'self' blob:
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Cross-Origin-Opener-Policy: same-origin
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()
X-Frame-Options: SAMEORIGIN
```

plus `Strict-Transport-Security` on HTTPS in production and
`Cache-Control: private, no-store` for signed-in visitors. The templates
contain no inline event handlers; the few inline script blocks
carry the per-request nonce. `style-src-attr 'unsafe-inline'` is needed for
the sanitised spacing styles page content may use.

Chromium-based browsers also apply `form-action` to the redirect that follows
a form submission. Two pages answer their form with a redirect to another
site, so their own response adds that origin (never a path) to `form-action`:
the wiki's `/settings/link-platform-account` adds the portal's (from
`BW_PLATFORM_OAUTH_AUTHORIZE_URL`), and the portal's OAuth consent page adds
the wiki's, taken from a redirect URI already checked against the wiki's
address and verified domain. Every other page keeps `form-action 'self'`.

`img-src https:` lets pages show images from other sites. A reader's browser
then contacts that site, which learns the reader's address. If that matters,
ask editors to upload images instead of linking them.

## Content

* Markdown is rendered with Python-Markdown and sanitised with
  [nh3](https://github.com/messense/nh3) against a strict allow-list: known
  tags only, known classes only, `id` only on headings, `style` limited to a
  few spacing properties with bounded, non-negative values (no negative
  margins, viewport units, `calc()` or `var()` that could lay content over
  the wiki's own interface), links get `rel="noopener noreferrer"`. Video,
  canvas and kanban embeds, mentions and task lists are generated after
  sanitising from validated parameters, and never inside an attribute value
  (nh3 0.3.7 or later is required).
* Rendering is bounded: one page highlights at most 64 KB of code (later
  blocks stay plain code; the blocks of a page-builder page share that
  allowance, as do the comments of one kanban ticket, which holds at most
  300, and one listing of them renders at most about 4 MB of HTML, newest
  first, older ones as plain text; excerpts and plain text highlight
  nothing), and rendered pages, snippets and PDF exports are cached by the
  hash of their source (a PDF also by the state of the uploaded images it
  embeds), so reading a page again costs nothing. The live
  preview and code highlighting endpoints are for editors only, at most 60
  requests a minute.
* Page-builder documents are validated against an allow-list of block types
  and escaped when rendered.
* Custom pages with author HTML, CSS or JavaScript are served from a
  sandboxed document (`Content-Security-Policy: sandbox`, opaque origin: no
  cookies, no same-origin requests) inside a wiki-made wrapper.
* Federated copies from other wikis are shown as plain text.

## Uploaded files

* Stored under random names in the configured folders; the original name is
  kept only in the database, in any script (normalised to NFC, without
  folders, control or direction-override characters), and offered back with
  an RFC 5987 `filename*`. Uploads are written to a temporary file and
  renamed into place.
* Size limits per kind, the administrator's allow/deny lists of extensions,
  the host's blacklist and the storage quota are checked before anything is
  kept. Images are verified, and still images are re-encoded, which removes
  EXIF and GPS metadata.
* An image's size in pixels is read from its header and checked before it is
  decoded: 40 megapixels at most, 4 for avatars and
  `BW_BACKGROUND_IMAGE_MAX_PIXELS` for display backgrounds. Each worker
  process decodes one picture at a time, and an account can change its avatar
  or background ten times in ten minutes.
* Downloads send `X-Content-Type-Options: nosniff` and
  `Content-Security-Policy: default-src 'none'; sandbox`. Only images, audio,
  video, PDF and plain text are shown inline; everything else is a download
  (`application/octet-stream`).
* Attachments are checked against the page, chat or board they belong to on
  every download.

## Speech conversion

The speech encoder accepts local MP3 and PCM WAV inputs. It explicitly selects
the audio demuxer and allows only PCM/MP3 decoders and the local-file protocol;
video, subtitles, playlists and network protocols are not used. Encoding and
tempo changes each use one decoder, filter and encoder thread. Embedded metadata
is discarded. Diagnostics are capped at 64 KiB, execution is limited to five
minutes, and failed or timed-out encoders are stopped and reaped.

These application restrictions reduce exposure to unnecessary multimedia
parsers. They do not remove libraries installed in the container or resolve
their distribution advisories; see [production validation](production-readiness.md#image-advisories).

## Secrets at rest

* API tokens are stored as HMAC-SHA256 digests keyed from the secret key;
  the token is shown once.
* Session tokens, portal tokens and hosting API tokens are stored as SHA-256
  digests.
* The GPU speech server token and federation pairing keys are encrypted
  (Fernet, key derived from the secret key).
* Two-step verification keys are encrypted (Fernet, key derived from the
  secret key) and recovery codes stored as HMAC-SHA256 digests; codes are
  shown once.
* Retired 1.4 secrets (the Telegram bot token, feedback bot settings) are
  erased by the 1.6 upgrade.

Losing or changing the secret key signs everyone out and makes API tokens,
encrypted settings and two-step verification unusable (reset it with
`bananawiki reset-two-step NAME`). Back it up with the database.

## Outbound connections

Everything that calls another server on behalf of configuration or remote
data (federation, platform sign-in, the GPU speech server) goes through one
HTTP client that resolves the name once, refuses link-local (cloud metadata),
IPv6 site-local, multicast and reserved addresses, refuses private and loopback addresses
unless that connection is explicitly allowed, connects to the checked
address, does not follow redirects, and caps time and response size. Under
managed hosting no webhook or federation peer can be allowed to reach private
or loopback addresses: that network belongs to the host.

## Plugins

Third-party plugins run inside the wiki process with its privileges: they
can read every table, the secret key and the environment. They are therefore
off unless the server operator sets `BW_ALLOW_EXTERNAL_PLUGINS=1` (an
installation upgraded with plugins already installed keeps them while the
variable is unset, and logs a warning), and only the **owner** and
**superusers** may install or enable one: an administrator, or an API token,
cannot turn their access into code on the server. Administrators can still
switch plugins off and delete them. The plugin manager asks for the acting
person's password, copies the database before enabling a plugin, and logs
every step. A plugin only loads after a restart.
`BW_ALLOW_EXTERNAL_PLUGINS=0` turns them off entirely. See
[plugins](plugins/README.md).

## Audit log

**Admin → Audit log** (feature `audit`) records sign-ins, account creation,
renames, released former user names, deletions, role and custom-role
changes, superuser grants, permission overrides, approvals, suspensions and
their lifting, impersonation, session revocations and mass sign-outs,
attribution and role-history clean-up, invite codes (never the code itself),
password changes and resets, two-step verification turned on or off, reset,
new recovery codes and sign-ins with a recovery code, page and category
deletions and restores, bulk
deletions, protections ended by a demotion, attachment uploads and
deletions, site settings, appearance and language changes, features and
plugins switched, installed or removed, documentation, bulk Markdown and
whole-site exports and imports (and refused attempts), server restarts, and
administrators opening, exporting, clearing or taking over private
conversations and groups they are not in (reading a group's messages as
JSON is recorded once per ten minutes of reading; downloading their
attachments too) or resolving reported chat messages, with the acting account
and address.

Only owners and superusers can switch the audit log off (the switch is
recorded as its last entry), whichever page switches it: the plugin manager
or the onboarding wizard, which leaves the log out of the features it offers
a plain administrator. Every feature switch is recorded. Owners and
superusers also change the retention (0 keeps entries forever; the
`audit.prune` job applies it and records how many entries it deleted). A
site import keeps the live log and appends it to the archive's, without the
entries the archive already holds.
The plugin manager's own steps also go to the application log
(`bananawiki.plugins`), and the REST API keeps its own log of every call
(**Admin → REST API**).

## Checklist for production

For public hosting, also follow the [release assessment](production-readiness.md),
including unfixed tenant-image advisories and the required host firewall,
storage quotas, reserve, restore and update/rollback checks.

* Serve over HTTPS only; set `BW_PROXY_MODE=1` only when the port is reachable
  through your proxy alone.
* Keep `BW_ENV=production` (the default).
* Back up the database, the upload folders and the secret key; test a restore.
* Keep few administrators; make yourself owner and superuser (the first
  account already is).
* Turn on two-step verification for your account, then require it of
  administrators (**Admin → Settings → Security**).
* Leave `BW_ALLOW_EXTERNAL_PLUGINS` unset or `0` unless you need plugins;
  review every plugin before enabling it.
* Switch off features you do not use (**Admin → Plugins**).
* Keep the server and BananaWiki updated (`sudo bananawiki update`).
* Set `BW_SOURCE_URL` to the source you run if you changed the code (AGPL).
