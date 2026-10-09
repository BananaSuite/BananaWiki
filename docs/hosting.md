# Hosting platform

The hosting platform lets other people create their own wikis on your server:
they sign up on a portal, create a wiki, and get it at
`<name>-<suffix>.<your domain>` (or on a port, or a custom domain). Each wiki
is an ordinary BananaWiki running in its own container. This is the mode the
hosted BananaWiki service runs.

Installing it: [deployment](deployment.md#hosting-platform). Every variable:
[configuration](configuration.md#hosting-portal).

## Components

| Component | Runs as | Does |
|---|---|---|
| Portal (`bananawiki.service`, `gunicorn … hosting.wsgi:app`) | the service account, **no Docker access** | Accounts, wikis, policies, the administrator pages, the REST API, OAuth provider for platform sign-in. Owns `hosting.db`. |
| Maintenance (`bananawiki-maintenance.service`, `python -m hosting.maintenance --interval 300`) | the service account | Brings running wikis back after a restart or update, then every 5 minutes: lifts ended suspensions, terminates expired wikis and deletes data after the grace period, suspends wikis far over their storage cap, renews custom-domain proofs, deletes denied and scheduled accounts, sends the approval digest, prunes sessions and old uploads, publishes the routing table. |
| Runtime agent (`bananawiki-agent.service`) | root, no network, sandboxed | The only component that talks to Docker, over a local socket that only root and the service account may use. Starts, stops and inspects tenant containers with fixed security options, runs database tasks inside a tenant's sandbox, and writes Caddy's per-wiki routes. |
| Tenant containers (`bananawiki-tenant:<commit>`) | the tenant directory's owner, read-only root | One wiki each, with its data bind-mounted at `/data`. |
| Caddy | | HTTPS: the static site on the base domain, the portal on the portal domain, and one site block per running wiki (written by the agent to `/var/lib/<service>-routes/tenants.caddy`), so wiki traffic goes straight to each container. A wiki host without a route (a hand-written Caddyfile without the routes import, a 1.4 proxy configuration that sends every host to the portal, a container that has just started) falls through to the portal, which proxies a running wiki itself and otherwise shows a status page on the wiki's address (no wiki here, paused, suspended, expired, starting); it never shows portal pages on a wiki host. Certificates for wikis and custom domains come through on-demand TLS, or a wildcard certificate for the platform's own hosts (`proxy --tls cloudflare-dns|origin-cert`). Works behind Cloudflare's proxy ([below](#behind-cloudflare)). |

The contract between the portal and the agent is
`bananawiki/ops/RUNTIME_AGENT.md`.

## Isolation

* The portal parses uploads, archives and OAuth requests from the internet,
  so it has no Docker privileges (in 1.4 its account was in the `docker`
  group, which made any portal bug a root compromise).
* The agent accepts only a narrow set of operations and validates every
  argument. The portal chooses which tenant directory runs and within which
  limits; it cannot choose the image, mounts, capabilities, user or network
  mode. Resource requests are capped by `HOSTING_AGENT_MAX_*`.
* Every tenant container runs with `--read-only`, `--cap-drop ALL`,
  `no-new-privileges`, tmpfs `/tmp` and `/run`, one bind mount (its own
  directory at `/data`), memory, CPU, process and open-file limits, rotated
  Docker logs, and its own network: `isolated` (an internal bridge, the
  default in subdomain mode) or `outbound`.
  Managed tenant bridges are IPv4-only: creation explicitly disables IPv6,
  inspected IPv6-enabled bridges are refused, and container network sysctls
  disable IPv6 including link-local addresses. This protects the boundary
  even when a Docker daemon has different default network options. Public
  Caddy endpoints can still serve IPv6 clients; tenant upstreams use IPv4.
  An internal bridge blocks traffic to other tenant bridges and destinations
  outside its subnet, but can still reach services listening on its host
  gateway. Bind host-only services to loopback. Configure host firewall INPUT
  rules to allow established/related replies to host-initiated connections,
  allow only explicitly required host endpoints (for example public HTTPS
  for OAuth or the configured GPU service), then drop other traffic from
  tenant bridges to the host. DOCKER-USER/FORWARD rules alone do not cover
  host-bound traffic. Cover every managed tenant
  bridge before tenant traffic starts, including bridges created or recreated
  later. Test Caddy proxying and the intended integrations, and verify tenants
  cannot connect to a disposable host gateway listener outside that allowlist.
  On a dedicated hosting host, matching Docker's bridge prefix (`-i br+`)
  covers future bridges as well as current ones. For example, an INPUT chain
  can accept `ESTABLISHED,RELATED`, accept explicitly required TCP destination
  ports, then drop the remaining packets from `br+`. Install these rules before
  starting tenants, integrate them with the host's persistent firewall, and
  verify the effective order after a reboot. A derived runtime that enables
  tenant IPv6 also needs a verified ip6tables/nftables IPv6 INPUT policy,
  including link-local traffic; IPv4 rules do not protect it. Do not apply a
  blanket Docker bridge rule on a shared host
  without accounting for its other workloads.
  Docker's `inhibit_ipv4=true` and `gateway_mode_ipv4=isolated` bridge options
  remove the host bridge address, but also break Caddy's TCP connection from
  the host to each wiki; they cannot replace this firewall policy.
* Secrets for a tenant (its OAuth client secret, the shared GPU token, the
  first administrator's password) reach it through a private `--env-file`,
  never on a command line.
* Tenant databases are only opened inside the tenant's sandbox: migrations,
  seeding the first administrator, user management, analytics and plugin
  quarantine run as `tenant.task` operations of the agent (`docker exec` for a
  running wiki, a one-shot container for a stopped one), never in the portal.
* Tenants run with `BW_MANAGED_HOSTING=1`: site import off, the host owns the
  upload limits and the GPU settings, public builder pages off, third-party
  plugins off by default. The operator can set `HOSTING_ALLOW_TENANT_PLUGINS=1`
  to allow them in container isolation; never those in
  `HOSTING_TENANT_PLUGIN_DENYLIST`. Subdomain mode only: the portal refuses
  to start with it in port or onion mode ([addresses](#addresses)).

When the operator enables third-party plugins, a wiki's administrators can
install Python code inside their container. Treat everything the container holds as readable by
them: its environment, its files and its database. That is why a wiki gets
its own GPU speech token (derived from the master token, revocable on the GPU
server with `TTS_REVOKED_TENANTS`) and never a platform-wide secret, and why
the limits that matter are enforced outside the container (memory, CPU,
processes, network isolation, the proxy). Policies the wiki applies to itself
(upload blacklist, public mode, page builder, storage limit, federation) keep
honest administrators within the plan but are not a security boundary against
one who installs a plugin to lift them; use `HOSTING_TENANT_PLUGIN_DENYLIST`
and plugin quarantine for wikis you do not trust.

Upgrading leaves existing third-party plugin files and database settings in
place, but stops loading them unless the operator explicitly sets
`HOSTING_ALLOW_TENANT_PLUGINS=1` and restarts the portal and tenants. Lifting
a wiki's plugin quarantine does not override the operator's choice. Built-in
wiki features are unaffected.

All tenant containers run as the service account's UID so the portal can read
files for backups and exports. The wiki's storage estimate remains useful for
plan reporting. The root runtime agent separately assigns and verifies hard
XFS project byte and inode limits before the first seed, import, copy, restore
write or tenant launch. Unsupported filesystems, disabled enforcement, changed
project identities and insufficient host capacity refuse the operation.

Use a dedicated XFS filesystem with project accounting and enforcement enabled
(`prjquota`). Mount the installation parent, with `data/` and `data/instances/`
as ordinary directories within it; mounting `instances/` itself prevents the
managed restore workflow from renaming data directories. Keep the instances
parent in project zero with inheritance off. Do not share this filesystem or
its project-ID range with another quota manager. The agent keeps its private,
root-owned allocation registry in `/var/lib/<service>-quotas`, outside tenant
data and backups. Preserve that registry across service restarts and updates.

The operator-owned ceilings default to 10 GiB and 100,000 inodes per tenant.
A smaller wiki plan becomes its byte limit; a plan of zero still uses the finite
host ceiling. Configure `HOSTING_AGENT_MAX_STORAGE_BYTES`,
`HOSTING_AGENT_MAX_INODES`, `HOSTING_AGENT_PROJECT_ID_START` and
`HOSTING_AGENT_STORAGE_RESERVE_BYTES` in the installation environment. Byte
limits must be multiples of 512; the default reserve is 256 MiB. Admission
checks both total assigned finite budgets against usable volume size and
outstanding unused tenant reservations against current free space, with
conservative inode headroom. The total-budget check remains conservative when
live tenants write or delete files during admission. This does not reserve every possible filesystem metadata byte or
budget independent operator writes: provide appropriate physical headroom and
separate portal/backup capacity, and monitor the dedicated volume.

The generated agent unit permits writes to the managed `data/instances/`
path. A custom `INSTANCES_DIR` needs a reviewed `ReadWritePaths` override in
addition to the same XFS prerequisites. The agent never accepts a quota device,
project ID, registry path or filesystem descriptor from a portal request.
Generated hosting portal and maintenance services install an argument-filtered
`ioctl` policy before loading the application. Python's descriptor/socket setup
remains available; the service account cannot change project assignments or
inheritance flags through host filesystem setters. The trusted root agent
retains the required quota ioctls.
An existing or restored tree can only be adopted or repaired while its container
is confirmed stopped and any one-shot tasks left by an interrupted agent have
been removed and confirmed absent. This check compares actual mounted inode
identities, including tasks whose folder was renamed. A live server mounted
under another name must be stopped before admission; an operator can stop its
existing Docker container and restore the intended folder name when recovering
such a mismatch. A running tenant must retain its recorded filesystem,
inode identity, project inheritance and exact finite kernel limits. After a
full restore, quota identities are reassigned and verified before execution.

New instance reservations remain in a provisioning state until all seeding or
copying succeeds. Recovery and manual start refuse unfinished or failed
reservations. A worker interrupted during creation cannot expose a half-created
wiki; administrators can cancel it after its provisioning lock is released and
create it again. Failed cleanup retains the reservation until removal succeeds.
Neither a failed creation nor a cancellation deletes a data folder the creation
did not make: when the folder already existed, or the portal never handed the
creation to the runtime agent, it is left untouched for an administrator to
inspect. The agent's refusal of an existing folder is written to
`.provisioning-refused/<wiki id>` under `HOSTING_PLATFORM_STATE_DIR` before
the database records it, so a failed database write does not lose it; these
files are a few bytes each and are kept. One case remains: a portal worker
killed outright (`kill -9`, power loss) after it handed the creation to the
agent and before it recorded the agent's answer. Cancelling that creation, or
its expiry, deletes the folder under its name, because the portal cannot tell
whether the agent made it. Before cancelling a wiki that stays in
provisioning, or letting it expire, check its folder. The folder can also be
lost if both the marker file and the database records of the refusal fail to
be written.

The agent applies a pinned Docker default seccomp allowlist with quota-changing
ioctls excluded. Without this restriction, an ordinary file owner can change
an XFS project ID or clear inheritance despite `--cap-drop ALL`. Both native
and 32-bit ioctl layouts, including upper-bit and sign aliases, are blocked.
This hosting boundary currently requires Linux x86_64; other host architectures
fail closed until verified. Running tenants from an older release must use the
current seccomp and IPv6 policy before administrative tasks can execute in them.
Starting/recovering a tenant recreates outdated sandbox settings even when its
image and application policy match. The profile and its
upstream Apache-2.0 license ship in the package; missing or altered profile files
refuse tenant launches and tasks.
Reserve disk space for the portal and backups, and test that a container cannot write
past that quota. Also test network isolation, resource limits and a complete
backup restore on the deployment itself. Disabling plugins reduces exposure
to arbitrary tenant code; it does not provide a hard disk quota or replace
those deployment checks.

## Addresses

* **Subdomain mode** (`BASE_DOMAIN` set to a domain name): wikis at
  `<name>-<suffix>.<BASE_DOMAIN>` (suffix `hosting` by default, configurable in
  the platform settings or with `INSTANCE_URL_SUFFIX`; empty for
  `<name>.<BASE_DOMAIN>`). These host names are one level below
  `BASE_DOMAIN`, covered by on-demand TLS or a wildcard certificate
  (`bananawiki proxy --tls cloudflare-dns|origin-cert`). They are single-level
  names of the DNS zone, as Cloudflare's free certificate requires, only when
  `BASE_DOMAIN` is the zone itself (`example.com`, not `hosting.example.com`);
  `install` and `proxy` warn otherwise.
* **Port mode**: wikis at `http(s)://<HOSTING_PUBLIC_HOST>:<port>` with ports
  from `INSTANCE_PORT_START` to `INSTANCE_PORT_END`. Needs
  `HOSTING_TENANT_NETWORK=outbound`.
* **Onion mode**: for Tor hidden services; also needs outbound networking.

In port and onion mode the portal and every wiki share one host name, and
browsers ignore the port when they send or store cookies. Cookies are
therefore **not isolated**: every wiki receives the portal's session cookie
and the other wikis' session cookies, and any wiki can set cookies that the
portal and the other wikis accept. HTTPS does not change this: the
`__Host-` prefix binds a cookie to the host name, which the wikis share, so a
wiki can set the portal's `__Host-` cookie too. Each container is published
on its own port and the portal is not in the way, so BananaWiki cannot strip
these cookies: a wiki's code must be trusted as much as the portal's, and
the portal refuses to start when `HOSTING_ALLOW_TENANT_PLUGINS=1` is set in
these modes. Use subdomain mode when the wikis' administrators are not
trusted.

Names that would collide with platform records (`www`, `mail`, `admin`,
`api`, the portal's own label …) are reserved.

## Custom domains

An administrator allows custom domains per wiki. The owner then claims a host
name on the wiki's **Domain** page, publishes a TXT record
`_bananawiki-challenge.<domain>` with the given token and a CNAME to
`HOSTING_CUSTOM_DOMAIN_TARGET` (or A/AAAA records to
`HOSTING_CUSTOM_DOMAIN_IPS`), and presses **Verify**. A verification is valid
for 24 hours and renewed by the maintenance service; when the proof
disappears, routing stops. Unverified claims expire after an hour, so a claim
cannot reserve a name. Caddy asks the portal's `/internal/domains/authorize`
(on loopback; the public listeners answer 404 for `/internal/*`) before it
requests a certificate.

Keep `HOSTING_CUSTOM_DOMAIN_TARGET` a **DNS-only** (grey-cloud) record if your
own zone is on Cloudflare: a customer's CNAME to a name proxied by another
Cloudflare account fails with Cloudflare error 1014. Customers may proxy their
own record through their Cloudflare account: public DNS then shows only
Cloudflare's addresses, so with `HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED` (on by
default) a domain whose addresses all belong to Cloudflare passes the routing
check (the TXT proof still proves ownership). The domain page then tells the
customer to use SSL/TLS mode *Full (strict)* and not to redirect
`/.well-known/acme-challenge/` to HTTPS until the wiki's certificate exists.

## Behind Cloudflare

The portal, the static site and every wiki work with proxied (orange-cloud)
records: visitors' real addresses reach the rate limits, there is no redirect
loop in any SSL/TLS mode, and a wiki that is starting shows its status page
instead of Cloudflare's "Bad gateway". Settings, certificate modes and limits
(100 MB request bodies, about 125 s per request, one subdomain level, no port
mode): [deployment](deployment.md#behind-cloudflare).

## Accounts

* **Sign-up modes** (platform settings): open, invite codes, approval by an
  administrator, or closed. The
  first account is the administrator and needs `HOSTING_BOOTSTRAP_TOKEN`.
* Email verification, password and user-name recovery, and notices by email
  through Brevo, Resend or SMTP (`HOSTING_EMAIL_*`), with daily limits.
* Two-step sign-in with an authenticator app and one-time recovery codes
  (administrators can be required to use it); sessions listed and revocable;
  "sign out everywhere".
* Password sign-in reserves its rate-limit budget before checking the hash,
  so concurrent requests cannot bypass it. Limits over 15 minutes are 20 per
  address, 8 per account from one address and 100 per account across addresses.
  A successful sign-in refunds its own attempt and clears the account-and-
  address failures; it preserves the address budget used to guess other accounts.
* Account merges (both sides confirm, or an administrator decides), account
  deletion with a grace period, suspension, flagged email addresses.
* Personal access tokens for the portal API.

* When an administrator approves or denies an owner's request (account,
  wiki feature, account merge), the owner sees a notice at the top of the
  portal until they dismiss it, and gets an email when they have an address.

## Wikis

Owners create wikis on the dashboard (`/instances/create`) up to
`MAX_INSTANCES_PER_ACCOUNT`, and for each one can:

* open it, see its status, storage and traffic, read its logs;
* stop, restart, terminate, reset its content, reset its administrator's
  password, rename it;
* switch the easy wiki mode;
* invite collaborators with full access or chosen permissions, and transfer
  ownership (the recipient accepts);
* request restricted features (public access, page builder, custom domain),
  which an administrator approves;
* download an export of the wiki.

While public access is restricted (by default, for owners who are not
platform administrators) and not approved, a wiki runs with
`BW_FORBID_PUBLIC_MODE=1`: no public mode, and its custom pages (with their
files, redirects and sandboxed documents) are shown only to signed-in members;
anonymous visitors are sent to sign in. Builder pages, custom ones included,
stay members-only while public builder pages are forbidden
(`BW_FORBID_PUBLIC_BUILDER_PAGES`).

A wiki lives `INSTANCE_DURATION_DAYS` unless an administrator extends it or
makes it indefinite. When it expires or is terminated, it enters the grace
period configured in the platform settings, during which an administrator can
restore it; afterwards its data is deleted. An administrator can pause that
countdown: the data is then kept until the countdown resumes (the paused time
is added to the grace period), and the owner cannot download it meanwhile.

Platform sign-in: every wiki is an OAuth client of the portal, so owners and
collaborators sign in to their wikis with their portal account
(`/platform-oauth/login` on the wiki). New wiki accounts created this way
follow the wiki's own sign-up policy.

## Administration

`/admin` on the portal (platform administrators):

* accounts: approve, deny, suspend, delete, impersonate, promote, flag email;
* wikis: every lifecycle action above plus suspend (the expiry clock stops),
  extend, set storage limits and upload policy, move between owners,
  duplicate, restart, bulk actions, import an archive (chunked upload),
  quarantine a wiki's plugins and restore plugin snapshots;
* feature requests, invite codes, banners, merge requests, moderation history;
* **needs your attention**: the *Admin* link carries a red dot with the number
  of requests waiting (accounts to approve, wiki feature requests, account
  merges both owners confirmed), the first page after an administrator signs
  in shows a banner, and `/admin/attention` plus the top of the dashboard list
  every queue with how long the oldest item has waited;
* **notification emails** (platform settings → *Notifications for
  administrators*): every administrator with an address (each can opt out on
  their account page) and/or one extra address, **immediately** for each new
  request, as a **digest** at most every N minutes, or as a **daily summary**
  (hour in UTC). They are sent by the maintenance service (`attention emails`
  step, every `--interval` seconds, 300 by default), in each administrator's
  language, with the throttling state in `hosting_attention_recipients`. A
  *Send a test email* button (five per hour) checks delivery;
* platform settings: sign-up mode and approval, URL suffix, grace periods,
  default limits, email, the shared GPU speech server, MFA requirements, the
  REST API switch, Google Drive backups;
* **platform export and restore**: an encrypted archive of `hosting.db`, the
  portal key and every tenant directory, and the backup key to decrypt it
  (download it and keep it apart from the backups).

## Backups

* `sudo bananawiki backup` / `backups …` (see [operations](operations.md#backups))
  cover the portal, every tenant and the static site.
* **Google Drive** (platform settings): daily encrypted platform backups at
  a chosen time to a Drive folder, using a Google service-account key file,
  kept for a configurable number of days. After an incomplete backup,
  retention keeps the latest complete one (known by its name, whatever
  Drive's clock says) and everything newer.
* **Platform export** from the administrator pages, encrypted with the key in
  `HOSTING_BACKUP_KEY_PATH`.

A wiki that cannot be saved in full no longer stops a platform backup: the
others are saved, `backup_manifest.json` in the archive names the wikis it
does not hold in full, and the platform settings show them with the time of
the latest complete backup; a restore logs them too. Such a wiki is only
reported, with whatever could be saved of it, when:

* its database snapshot fails or times out (nothing of the wiki is saved);
* its sandbox refuses the database copy (a damaged database, one replaced
  with a link or a special file), or the wiki removes, replaces or changes
  the copy before it is saved: its other files are still saved. The copy
  lies in the wiki's own folder, so it goes in only at the size and with
  the SHA-256 its sandbox reported, and within the wiki's byte budget
  below: a wiki that extends it to a huge sparse file cannot make the
  backup run out of space;
* its `storage` folder or one of its asset folders is a link or a special
  file, its folder cannot be read, or a file shrinks while it is copied;
* it holds files a restore would refuse, which are left out: a name that is
  not UTF-8, with a `:` or a backslash, or a path longer than 3 KiB, a file
  where the wiki needs a folder, a folder named like its database;
* it holds further hard links to a file already saved: each file goes in
  once, under one of its names;
* its files exceed its byte budget, and the rest are left out. The budget is
  the wiki's storage limit (from `hosting.db`; the default limit for a wiki
  without one of its own) plus a tenth of it and 64 MiB, on top of its
  database copy (which gets the same budget of its own), counting each file
  at its apparent size (a sparse file at its full length) with its entry in
  the archive. Administrators', apex and unlimited wikis have no budget.

The whole backup fails, and a scheduled Drive backup is retried an hour
later, only for faults of the platform: the runtime agent or Docker stops
answering after a wiki failed, the runtime could make the database copy of
no wiki at all, or `hosting.db`, the session key or the backup file itself
cannot be written (a full disk included). A wiki that kept itself out does
not count there: a platform whose only wiki does still gets a backup of
`hosting.db`. A Drive backup counts only once it is uploaded.

A wiki's own export (and the download during the grace period) leaves out
the files no import would accept or would take for the wiki's own: names
that are not UTF-8, with a `:` or a backslash, or longer than 3 KiB, a file
clashing with another file or a folder, and files below `assets/`, or below
`storage/` but outside its asset folders (`storage/translations/xx.json`,
`storage/favicons/custom_*`), that an import would put where the wiki's own
go. They are logged and listed in the export's `manifest.json`
(`omitted_files`, the first 50, and `omitted_file_count`). Where a top-level
asset folder and `storage/<folder>` hold the same file, the `storage/` copy
is exported. An export fails when the wiki changes its database copy before
it is saved (a size or SHA-256 other than its sandbox reported). Exports of
every wiki share `HOSTING_EXPORT_TEMP_DIR`, so an export saves each file
once, whatever number of hard links it has (the further links are listed
with the files left out), and keeps to the wiki's byte budget, as in a
platform backup: over it, the export fails and no archive is left.
Imports and platform restores accept only stored and deflated
ZIP members (bzip2 and LZMA data cannot be decompressed with a bounded
amount of memory).

Tenants are part of every managed update: the updater stops them, and the
maintenance service starts every wiki whose status is `running` again with
the new image; the update is rolled back if any of them that was serving
before does not become healthy. With `HOSTING_ALLOW_TENANT_PLUGINS=1`, a wiki
whose own plugins keep it from serving under the new release does not roll
the update back: the updater quarantines its plugins as the **Quarantine
plugins** button does (`bananawiki hosting-admin instance quarantine-plugins
WIKI` runs the same), starts it again and keeps the update if it then
serves, reporting it as `quarantined_tenants`; lift the quarantine once its
plugins are fixed. A wiki that was already failing does not
block updates, and one that does not come back from a backup or a recovery
is reported instead of keeping the platform in maintenance (see
[operations](operations.md#managed-servers)).

## Portal REST API

Off until an administrator switches it on in the platform settings. Tokens
(`bwh_…`, created under **Account → API tokens**, shown once, stored as
SHA-256) carry scopes:

| Scope | Endpoints (under `/api/v1`) |
|---|---|
| `account:read` | `GET /me` |
| `instances:read` | `GET /instances`, `GET /instances/<id>` |
| `instances:manage` | `POST /instances/<id>/pause`, `POST /instances/<id>/resume` |
| `admin:read` | `GET /admin/pending-accounts`, `GET /admin/instances` (administrators) |

`GET /api/v1/status` answers without a token. Each wiki's own API is
separate; see [API](api.md).

## Help centre and legal pages

The portal ships a help centre (`/help`, English and Italian, in
`bananawiki/hosting/content/`). `/terms`, `/privacy` and `/compliance`
redirect to `/terms/` and `/privacy/` on the base domain's static website,
which is served from `/opt/bananawiki/site/` and never touched by updates:
put your own legal texts there. The contact address shown to customers is
`HOSTING_CONTACT_EMAIL`.
