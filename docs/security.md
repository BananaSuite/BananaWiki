# Security

How BananaWiki protects accounts and content, what it trusts, and what you
should configure. To report a vulnerability see [SECURITY.md](../SECURITY.md).

## Trust model

* **Administrators are fully trusted.** An administrator can install a
  third-party plugin (Python code that runs with the wiki's own rights) or
  import a whole-site archive (which replaces every account). Either gives
  complete control of the wiki. Give the administrator role only to people
  you would trust with everything. The admin pages say this where the
  decision is made, ask for the administrator's password again, and log it.
* **Owners and superusers** are protected from other administrators in the
  normal interface: only owners and superusers change administrators, only an
  owner changes an owner, only a superuser changes a superuser or impersonates
  an administrator. This protects against a careless or compromised
  administrator account, not against plugins or site imports.
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
  together, in 15 minutes, shared by all workers. Failures from one address
  do not lock the account for everyone else. A successful sign-in clears
  only its own account-and-address counter.
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
* **Rate limits:** 300 requests per minute per client address and worker,
  plus per-action limits (editing, uploads, search, chats, the API …).
  Sign-in and sign-up limits are stored in the database, so they hold across
  all workers.
* **Body limits:** JSON bodies 2 MiB, form fields 2 MiB in memory; file
  uploads stream to disk and are checked against their own limits (see
  [configuration](configuration.md#limits)).
* **Proxies:** with `BW_PROXY_MODE=1` one hop of `X-Forwarded-For`, `-Proto`
  and `-Host` is trusted; `X-Forwarded-Prefix` never.

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
  few spacing properties, links get `rel="noopener noreferrer"`. Video,
  canvas and kanban embeds are generated after sanitising from validated
  parameters.
* Page-builder documents are validated against an allow-list of block types
  and escaped when rendered.
* Custom pages with author HTML, CSS or JavaScript are served from a
  sandboxed document (`Content-Security-Policy: sandbox`, opaque origin: no
  cookies, no same-origin requests) inside a wiki-made wrapper.
* Federated copies from other wikis are shown as plain text.

## Uploaded files

* Stored under random names in the configured folders; the original name is
  kept only in the database. Uploads are written to a temporary file and
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
* Retired 1.4 secrets (the Telegram bot token, feedback bot settings) are
  erased by the 1.6 upgrade.

Losing or changing the secret key signs everyone out and makes API tokens
and encrypted settings unusable. Back it up with the database.

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
can read every table, the secret key and the environment. The plugin manager
asks for the administrator's password, copies the database before enabling a
plugin, and logs every step. A plugin only loads after a restart.
`BW_ALLOW_EXTERNAL_PLUGINS=0` turns them off entirely. See
[plugins](plugins/README.md).

## Audit log

**Admin → Audit log** (feature `audit`) records sign-ins, account creation,
renames, deletions, role changes, password changes and resets, suspensions,
page and category deletions and restores, attachment uploads and deletions,
site settings, appearance and language changes, documentation, bulk Markdown
and whole-site exports and imports (and refused attempts), and server
restarts, with the acting account and address. Retention is configurable
(0 keeps entries forever; the `audit.prune` job applies it). Plugin actions
go to the application log (`bananawiki.plugins`), and the REST API keeps its
own log of every call (**Admin → REST API**).

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
* Review plugins before enabling them, or set `BW_ALLOW_EXTERNAL_PLUGINS=0`.
* Switch off features you do not use (**Admin → Plugins**).
* Keep the server and BananaWiki updated (`sudo bananawiki update`).
* Set `BW_SOURCE_URL` to the source you run if you changed the code (AGPL).
