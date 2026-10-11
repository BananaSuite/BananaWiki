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
| Maintenance (`bananawiki-maintenance.service`, `python -m hosting.maintenance --interval 300`) | the service account | Brings running wikis back after a restart or update, then every 5 minutes: lifts ended suspensions, terminates expired wikis and deletes data after the grace period, suspends wikis far over their storage cap and reopens them once they fit again, renews custom-domain proofs, deletes denied and scheduled accounts, sends the approval digest, prunes sessions, old uploads and moderation-log entries past their retention, publishes the routing table. |
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
  gateway, and an `outbound` bridge (port and onion modes) reaches the host,
  the LAN and cloud metadata endpoints such as `169.254.169.254`. Docker
  does not stop either: install the [host firewall](#host-firewall) before
  starting tenants.
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

When the operator enables third-party plugins, a wiki's owner and superusers can
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
files for backups and exports (see [one UID for every wiki](#one-uid-for-every-wiki)
for what that means and how to limit it). The wiki's storage estimate remains useful for
plan reporting. Where the wiki storage supports them, the root runtime agent
separately assigns and verifies hard XFS project byte and inode limits before
the first seed, import, copy, restore write or tenant launch; under them,
disabled enforcement, changed project identities and insufficient host
capacity refuse the operation. XFS is not required. Whether the agent uses
them is `BW_HOSTING_TENANT_QUOTAS`: `auto`, the default for every platform
(new ones too), uses them where the storage enforces them and otherwise runs
the wikis without them, as 1.6 did, with a calm notice, or with a loud
warning when hard quotas were enforced on this platform before and are no
longer available (the wikis still start); `required`, an explicit opt-in,
starts wikis only under them; `off` never uses them. Without hard quotas the agent
holds each wiki to its plan through the wiki's own upload check (a plan of
zero, as apex and administrators' wikis have, gets
`HOSTING_AGENT_MAX_STORAGE_BYTES`), refuses a new wiki while the volume's
free space is below `HOSTING_AGENT_STORAGE_RESERVE_BYTES` plus its plan, and
the portal suspends a wiki above 110% of its plan; nothing stops a wiki from
filling the disk between two of the portal's checks (every 5 minutes). The
agent still compares each started container's `/data` with the tenant
directory before the wiki runs, once its own units are installed. A portal
without the agent (a release the 1.4 updater installed, until its units
converge) can never enforce hard quotas: under `auto` it starts the wikis
without them, under `required` it refuses. See
[hard storage quotas](operations.md#hard-storage-quotas-hosting). What
follows applies when hard quotas are enforced.

Use a dedicated XFS filesystem with project accounting and enforcement enabled
(`prjquota`). Mount the installation parent, with `data/` and `data/instances/`
as ordinary directories within it; mounting `instances/` itself prevents the
managed restore workflow from renaming data directories. Keep the instances
parent in project zero with inheritance off. Do not share this filesystem or
its project-ID range with another quota manager. The agent keeps its private,
root-owned allocation registry in `/var/lib/<service>-quotas`, outside tenant
data and backups. Preserve that registry across service restarts and updates.

The operator-owned ceilings default to 10 GiB and 100,000 inodes per tenant.
A smaller wiki plan becomes its byte limit; a plan of zero, or one larger than
the ceiling, uses the finite host ceiling. A wiki that already holds more than
its limits when it is put under quota (1.6 suspended wikis only above 110% of
their plan and never limited administrators' or apex wikis; a plan can also be
lowered below what a wiki holds) still starts: its hard limits become what it
holds plus a small headroom (64 MiB, 1,024 files), it cannot grow past them,
and they follow its usage back down to its plan as data is removed. The
portal records a `storage.over_plan` event on the wiki and emails its owner.
Raise its plan, or have the owner remove files. A stopped wiki is put under
quota by examining every file in it, at most twice `HOSTING_AGENT_MAX_INODES`
files (at least 200,000): raise that setting before updating a platform with
larger wikis. Configure `HOSTING_AGENT_MAX_STORAGE_BYTES`,
`HOSTING_AGENT_MAX_INODES`, `HOSTING_AGENT_PROJECT_ID_START` and
`HOSTING_AGENT_STORAGE_RESERVE_BYTES` in the installation environment. Byte
limits must be multiples of 512; the default reserve is 256 MiB. Admission
checks both total assigned finite budgets against usable volume size and
outstanding unused tenant reservations against current free space, with
conservative inode headroom. The total-budget check remains conservative when
live tenants write or delete files during admission. Only admission checks
capacity: a new wiki, or a higher limit for an existing one. A wiki from before
1.7, put under quota at its first start, is not admitted then: its data is
already on the volume. 1.6 never checked plans against the volume, so such
wikis' limits may add up to more than it holds: they are recorded as not
admitted, a later start admits them once the volume can hold every limit,
and meanwhile snapshots leave each of them only its adoption headroom (64
MiB and 1,024 files) and print a warning with the shortfall. Lower plans or
free space to end it. A wiki already
admitted is verified (identity, inheritance, exact limits) and started
whatever else uses the volume meanwhile, so an update snapshot, a backup
package or a Drive export staged on the same filesystem never stops admitted
wikis or their tasks. Such a writer is refused instead when it would use space
the wikis were promised: an update or backup snapshot staged on the wikis'
volume must leave every admitted wiki's unused limit plus
`HOSTING_AGENT_STORAGE_RESERVE_BYTES` free, or it stops nothing and reports
how much space it needs. This does not reserve every possible filesystem metadata byte or
budget independent operator writes: provide appropriate physical headroom and
separate portal/backup capacity (or put `backups/` on another filesystem), and
monitor the dedicated volume.

The generated agent unit permits writes to the managed `data/instances/`
path. A custom `INSTANCES_DIR` needs a reviewed `ReadWritePaths` override
(and, for hard quotas, the same XFS prerequisites). The agent never accepts a quota device,
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

### Host firewall

Docker isolates tenant bridges from each other, but nothing it does stops a
wiki from connecting to a service on the host (anything listening on the
bridge gateway or `0.0.0.0`: the portal's port, SSH, a database) or, with
outbound networking, to the LAN and to cloud metadata endpoints. The wiki
has its own SSRF guards; the firewall is what holds when tenant code runs
(plugins) or a guard is bypassed. The release ships a default nftables
ruleset, `deploy/nftables/bananawiki-tenants.nft`. Load it before starting
tenants, with this host's DNS resolvers filled in:

```sh
sudo bananawiki firewall | sudo nft -f -
```

(`bananawiki firewall` prints the file with the private resolvers this host
uses in `dns_resolvers_v4`. Do not load or include the shipped file itself:
its `dns_resolvers_v4` is empty, so on a host with a private resolver, such
as a VPC resolver (`10.0.0.2` or `169.254.169.253` on AWS, `169.254.169.254`
on Google Cloud) or a LAN one, wikis would get no DNS. Print it again after
changing the host's resolvers.)

It keeps to its own table (`inet bananawiki_tenants`; loading it again
replaces only that table) and matches tenant bridges by name: since 1.7 the
runtime agent names every tenant bridge `bwt<12 hex>` (one created earlier
is replaced when its wiki next starts, and every wiki starts again after the
update to 1.7), so bridges created later are covered too and no other
interface is touched. What it does:

* a wiki never opens a connection to the host itself, except to the ports in
  the `host_tcp_ports` set (80 and 443: Caddy, so a wiki reaches the portal's
  sign-in endpoints by their public names) and `host_udp_ports` (empty);
  replies to connections the host opens (Caddy, health checks) pass;
* a wiki with outbound networking reaches the internet, but not private,
  loopback, link-local, shared (`100.64.0.0/10`), multicast or reserved
  addresses, except the addresses in the `allowed_private_v4` set and DNS
  (port 53) to the resolvers in `dns_resolvers_v4`: Docker's resolver sends
  a wiki's queries from the wiki's network to the resolvers the host uses (a
  VPC or LAN resolver), and no other private address is reachable on port 53
  (internal DNS servers, zone transfers, a service listening there). A
  resolver on one of the host's own addresses (dnsmasq, for example) is
  reached as a host port instead: `bananawiki firewall` then adds 53 to
  `host_udp_ports` and `host_tcp_ports`;
* no wiki talks to another wiki's bridge, and IPv6 from tenant bridges is
  dropped (they are IPv4-only).

A GPU speech server on the LAN goes in `allowed_private_v4`, one on this host
in `host_tcp_ports`: edit the sets in a copy of the printed rules and load
that.
Make it persistent with your distribution's nftables service from a saved
copy of the printed rules, for example
`sudo bananawiki firewall | sudo tee /etc/nftables.d/bananawiki-tenants.nft`
(Debian: `include` that copy from `/etc/nftables.conf` and `systemctl enable
nftables`); save it again after changing the host's resolvers or the sets.
Check after a reboot that the table is present
(`nft list table inet bananawiki_tenants`). It works next to Docker's own
rules, whichever backend Docker uses: an accept here never overrides a drop
in Docker's chains. Then verify from a tenant that a listener on the host
gateway and `169.254.169.254` cannot be reached, and that the wiki still
serves through Caddy.

### One UID for every wiki

Every tenant container runs as the service account, the same UID as the
portal: the portal reads and writes tenant directories (imports, exports,
platform backups) as that user, and the runtime agent requires each tenant
directory to belong to it. What keeps wikis apart is the container sandbox:
separate mount, PID and network namespaces, `--cap-drop ALL`,
`no-new-privileges`, a read-only root, the seccomp profile and, where the
storage supports them, per-wiki XFS quotas. A process that escapes a container through a kernel or container
runtime vulnerability while keeping that UID can read and write every wiki's
files and the portal's own (`hosting.db`, its secret key, the platform
backup key under `data/`). Most kernel escapes give root, which no UID
separation would contain; the shared UID matters for runtime escapes that
land as the container's user.

Docker's user-namespace remapping (`userns-remap`) and rootless Docker are
not supported in 1.7: a remapped container user does not own the tenant
directories the portal and the agent hand to it, and the agent's quota
checks need the host's view of each container. Per-tenant UIDs, with the
portal's file operations on tenant data moved into the agent, are planned.
Until then:

* keep third-party tenant plugins off (`HOSTING_ALLOW_TENANT_PLUGINS`, the
  default), so tenants cannot run code of their own in the first place;
* keep the kernel, Docker and runc patched (unattended upgrades), and reboot
  for kernel fixes;
* do not run other services, keys or data on the hosting server beyond what
  the platform needs;
* host wikis whose owners you do not trust on a separate server (or VM)
  from those that hold sensitive data.

### Hosted databases and the root controller

Backups and updates run as root. They copy each wiki's database
(`instances/<wiki>/bananawiki.db`) with its WAL or journal as plain bytes,
once the wiki is stopped, and never open it: a tenant controls that file, and
root does not parse what a tenant wrote. SQLite replays the WAL when the wiki
opens the restored copy, inside its own sandbox. Only the platform's own
databases (`hosting.db`) are copied with SQLite's backup API and checked.

### Restarts and updates at scale

A backup or an update stops every wiki for a consistent snapshot, and the
portal's maintenance service then starts them again,
`BW_HOSTING_RECOVERY_MAX_WORKERS` at a time (2 by default). The
agent still serializes the part of
each start that verifies the wiki's mount and storage (one global lock) and
reloads Caddy whenever the routes change. Since 1.7 a start no longer
re-checks the platform's storage capacity across every allocation, and
publishing the routes after each start reads the containers' addresses
without re-verifying every other wiki's quota, so a full restart no longer
grows with the square of the number of wikis. Restarting a stopped wiki
still walks its whole tree once to repair XFS project IDs (the boundary
against a tenant changing them), so restart time grows with the number of
files per wiki. On platforms with many wikis, measure a `restart` and keep
the readiness timeout (derived from `INSTANCE_STARTUP_TIMEOUT_SECONDS` and
`BW_HOSTING_RECOVERY_MAX_WORKERS`, see [configuration](configuration.md)) above
it; backups without downtime per wiki are planned.

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
cannot reserve a name. A domain whose proof lapsed more than 7 days ago (the
DNS moved away, the domain changed hands) can be claimed by another wiki with
a TXT token of its own; the old binding is removed only when that wiki
verifies the domain with its token. Until then the old owner keeps the
binding, and the maintenance service renews it as soon as their own records
are back. Caddy asks the portal's `/internal/domains/authorize`
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
mode): [deployment](deployment.md#behind-cloudflare). The generated
configuration believes Cloudflare's client-address header only from
Cloudflare's own edge addresses, also on a platform that is not behind
Cloudflare; since anyone with a Cloudflare account can connect from those
addresses, the limits per address are best effort everywhere (same section).

## Accounts

* **Sign-up modes** (platform settings): open, invite codes, approval by an
  administrator, or closed. The
  first account is the administrator and needs `HOSTING_BOOTSTRAP_TOKEN`.
* Email verification, password and user-name recovery, and notices by email
  through Brevo, Resend or SMTP (`HOSTING_EMAIL_*`), with daily limits (see
  [Abuse and capacity](#abuse-and-capacity)).
* Two-step verification with an authenticator app and one-time recovery codes
  (administrators can be required to use it: until an administrator turns it
  on, the portal only offers to do so, and that account has no administrator
  rights anywhere, over customers' wikis included); sessions listed and
  revocable; "sign out everywhere". Second-step codes from a browser that
  passed both steps for the account before are counted apart from everyone
  else's (10 per 5 minutes and 50 a day per browser), so someone who knows
  the password cannot lock the owner out; all browsers together get at most
  20 codes per 5 minutes and 150 a day. Turning two-step verification on or
  off retires every browser's cookie except the one that did it, so
  browsers that signed in with the password alone earlier get no budget of
  their own.
* Password sign-in reserves its rate-limit budget before checking the hash,
  so concurrent requests cannot bypass it. Limits over 15 minutes are 20 per
  address, 8 per account from one address and 100 per account across addresses.
  A successful sign-in refunds its own attempt and clears the account-and-
  address failures; it preserves the address budget used to guess other accounts.
* Account merges (both sides confirm, or an administrator decides), account
  deletion with a grace period, suspension, flagged email addresses.
* An account that must do something first is held on the page that does
  it, one thing at a time, in this order: a scheduled deletion, a flagged
  address, a missing contact address, an unconfirmed address, approval
  (pending or denied), two-step verification for an administrator who must
  use it, new terms of service. Signing out, deleting the account and
  revoking its API and wiki sign-in tokens always work; a suspended account
  is signed out and sees why.
* Personal access tokens for the portal API.

* When an administrator approves or denies an owner's request (account,
  wiki feature, account merge), the owner sees a notice at the top of the
  portal until they dismiss it, and gets an email when they have an address.

### Abuse and capacity

With open sign-up, anyone can create accounts, and every wiki holds a place
under the platform's wiki limit (platform settings, 50 by default) until it
expires. These defaults keep a burst of throwaway accounts from locking out
everyone else; for a public, internet-facing platform, also consider the
*approval* or *invite* sign-up modes and email verification.

* **Sign-ups per address**: the sign-up form creates at most
  `HOSTING_SIGNUPS_PER_IP_PER_DAY` accounts (10) per client address and day,
  on top of five attempts a minute. Like every per-address limit of the
  portal (sign-in failures, form limits, the global request limit), it counts
  an IPv6 client as its whole /64 network.
* **New accounts**: an account created on the sign-up form that no
  administrator approved is *new* for `HOSTING_NEW_ACCOUNT_DAYS` (7). A new
  account may hold `HOSTING_NEW_ACCOUNT_MAX_INSTANCES` wikis (1), and the
  wikis of all new accounts together may use at most
  `HOSTING_NEW_ACCOUNT_SHARE_PERCENT` (50 %) of the platform's wiki limit.
  The rest of the platform stays available to established and approved
  accounts. Approving an account (approval mode) lifts these limits at once.
* **Wikis per account**: `MAX_INSTANCES_PER_ACCOUNT` counts running, stopped
  and suspended wikis, and applies to transfers as well: a customer at the
  limit cannot accept another wiki.
* **Expiry**: wikis of customers expire after `INSTANCE_DURATION_DAYS`, so
  abandoned ones give their place back (see [Wikis](#wikis)).
* **Email budgets** are counted in the database (`hosting_email_log`), so
  every worker and restart shares them: `HOSTING_EMAIL_DAILY_LIMIT` messages
  per 24 hours in total, in two parts that cannot use each other up: a
  fifth for password resets and user-name reminders, the rest for
  everything else (verification links, notices). Reminders, which anyone
  who knows an address can ask for, may use only half of that fifth;
  resets, which also need the user name, may use all of it. Also 15
  messages per recipient, of which at most 3 password resets and 3
  user-name reminders (counted apart, so reminders cannot hold back a
  reset); `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` (10) for messages an account
  causes itself, such as verification links when it changes its address;
  and 10 recovery requests (the two "forgot" forms together) per client
  network and day (an IPv4 address or an IPv6 /64), and 30 per IPv6 /48,
  counted whether or not an account matches. When a budget is spent the
  message is refused and the user is told to try again later. When the
  recovery part is spent, the "forgot" forms say so to everyone (it says
  nothing about accounts) and administrators see **Account recovery emails
  are paused** among the items that need them (`/admin/attention`, and the
  attention email). Someone who knows enough user names with their verified
  addresses, and has a few networks, can still use up the recovery part
  for a day: raise `HOSTING_EMAIL_DAILY_LIMIT` if that happens.
  Email verification **fails closed**: it stays required even when the
  budget is spent (it is only skipped when no email provider is configured
  at all), so a spent budget holds new sign-ups and address changes at
  **Confirm your email** until the next day. Keep the daily limit well above
  what the platform needs, and watch the portal's log for `not sent:
  daily_limit` warnings.
* **Email addresses**: while verification is required, an address another
  account typed but did not confirm no longer blocks its real owner once a
  verification link's lifetime (`HOSTING_EMAIL_TOKEN_TTL_SECONDS`) has
  passed since it was typed: the first account to claim it afterwards gets
  it, and the other account loses the unconfirmed address. A link sent later
  (when the first one never arrived) keeps the address while that link is
  valid, but never longer than twice the lifetime. Changing the address away
  and back does not restart the count: within 30 days, the first three
  addresses an account takes (signing up counts) do, later ones count from
  the first, whether or not the account confirmed an address in between. A
  confirmed address is never taken away.

## Wikis

Owners create wikis on the dashboard (`/instances/create`) up to
`MAX_INSTANCES_PER_ACCOUNT`, and for each one can:

* open it, see its status, storage and traffic, read its logs;
* stop, restart, terminate, reset its content, reset its administrator's
  password, rename it. Resetting the administrator's password also signs
  that account out everywhere and turns off its two-step verification on the
  wiki (recorded in the wiki's audit log and the portal's moderation
  history): it is the way back in for an owner who lost the authenticator
  app and the recovery codes, who has no shell to run
  `bananawiki reset-two-step` in the tenant;
* switch the easy wiki mode;
* invite collaborators with full access or chosen permissions, and transfer
  ownership (the recipient accepts, and chooses whether the previous owner
  keeps view-only access or none, the default; the previous owner never
  keeps the right to stop, reset, download or delete the wiki, and the
  wiki's administrator password is not changed, so the new owner should
  reset it);
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

A wiki lives `INSTANCE_DURATION_DAYS` (14; 0 = wikis never expire) unless an
administrator extends it or makes it indefinite. Its owner is warned by email
`HOSTING_EXPIRY_WARNING_DAYS` before (3 days and 1 day by default), and told
when it expired. With *Owners may renew their wikis* on (platform settings,
off by default), the owner can renew a wiki in the last 7 days before it
expires: it gets a new full lifetime from that moment, so a wiki lives as
long as someone looks after it and an abandoned one still expires.
Collaborators cannot renew, whatever their permissions. An end date an
administrator brought forward (or set on a wiki that had none) is not
renewable: the owner asks the platform, and the expiry warning says so.
Once an administrator moves that date later again, or removes it, the owner
can renew as before. When it
expires or is terminated, it enters the grace
period configured in the platform settings, during which an administrator can
restore it; afterwards its data is deleted. An administrator can pause that
countdown: the data is then kept until the countdown resumes (the paused time
is added to the grace period), and the owner cannot download it meanwhile.

**Storage protection.** A customer's wiki that uses more than 110% of its
storage limit is suspended by the maintenance service, which emails its
owner. Since the owner cannot free space while it is suspended, the way out
is for an administrator to raise its limit for a while (**Admin → the wiki →
Limits**): a wiki suspended for storage and nothing else reopens by itself,
at once when the raised limit is saved or at the next maintenance pass, as
soon as it fits within its limit (the limit itself, not the 10% margin), and
its owner is told. A suspension an administrator made, or that of the
owner's account, is never lifted automatically, and nothing reopens while
the owner's account is suspended. An administrator who reactivates a wiki
that is still over its limit is warned that the next pass will suspend it
again.

Platform sign-in: every wiki is an OAuth client of the portal, so owners and
collaborators sign in to their wikis with their portal account
(`/platform-oauth/login` on the wiki). New wiki accounts created this way
follow the wiki's own sign-up policy. When the portal user name already
exists on the wiki, the portal account is linked to it only after its wiki
password and, when that account uses two-step verification, its code (an
authenticator or recovery code, counted like a sign-in's); the wiki then
signs it in without asking for the code again. Signing in through the portal
later asks for the code as well.

## Administration

`/admin` on the portal (platform administrators):

* accounts: approve, deny, suspend, delete, impersonate, promote, flag email;
* wikis: every lifecycle action above plus suspend (the expiry clock stops),
  extend, set storage limits and upload policy, move between owners,
  duplicate, restart, bulk actions, import an archive (chunked upload),
  quarantine a wiki's plugins and restore plugin snapshots;
* feature requests, invite codes, banners, merge requests, moderation history
  (kept for `HOSTING_EVENT_RETENTION_DAYS`, 10 years by default, then deleted
  by the maintenance service);
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
  lies in the wiki's own folder, so it goes in only at the size its sandbox
  reported and within the wiki's byte budget below: a wiki that extends it
  to a huge sparse file cannot make the backup run out of space. Its SHA-256
  is checked while it is saved: a copy whose content changed stays in the
  archive, and the wiki is reported;
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
  the archive. For a wiki the runtime agent adopted above its plan, the
  budget starts from the hard limit it was adopted at (the portal keeps it
  as `over-plan.json` in the wiki's platform state), which its quota holds
  it to. Its own export and its plugin snapshots use the same budget.
  Administrators', apex and unlimited wikis have no budget.

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
Plugin snapshots (saved by an administrator, or kept when plugins are
quarantined or before a snapshot is restored) and the database of a
duplicated wiki are checked the same way: a copy the wiki changed, or one
over the byte budget (for a duplicate, the new wiki's), is refused and
nothing of it is kept; a quarantine still goes on without its copy.
Imports and platform restores accept only stored and deflated
ZIP members (bzip2 and LZMA data cannot be decompressed with a bounded
amount of memory) that are neither encrypted nor patched, as a wiki's own
whole-site import does; any other archive is refused before a member is read.

Tenants are part of every managed update: the updater stops them, and the
maintenance service starts every wiki whose status is `running` again with
the new image; the update is rolled back if any of them that was serving
before does not become healthy, unless the updater can keep it. With
`HOSTING_ALLOW_TENANT_PLUGINS=1`, a wiki whose own plugins keep it from
serving under the new release does not roll the update back: the updater
quarantines its plugins as the **Quarantine plugins** button does
(`bananawiki hosting-admin instance quarantine-plugins WIKI` runs the same),
starts it again and keeps the update if it then serves, reporting it as
`quarantined_tenants` (also in `status`, `last_update`); lift the quarantine
once its plugins are fixed. A few wikis that still do not serve (at most one
in ten, never all), including one whose plugins could not be quarantined
(its data fails the quarantine too, for example a full storage quota), are
then stopped and reported as `isolated_tenants` while the others keep the
new release, unless `BANANA_UPDATE_UNREADY_WIKIS=rollback`. Such a wiki's
quarantine marker is usually in force already (it is set first), with its
plugins still switched on in its database: its isolation says so, and
lifting the quarantine turns them on again. A wiki that was
already failing does not block updates, and one that does not come back
from a backup or a recovery is reported instead of keeping the platform in
maintenance (see [operations](operations.md#managed-servers)).

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

The portal ships a help centre (`/help`, in English, Italian and German like
the rest of the portal, in `bananawiki/hosting/content/`). Every link of the
portal to the terms of service and the privacy policy (the sign-up form, the
new-terms page, the wiki forms, the help centre) goes through `/terms` and
`/privacy` (`/compliance` is the terms), which redirect to your pages:

* **Your own addresses:** set `HOSTING_TERMS_URL` and `HOSTING_PRIVACY_URL`
  (absolute `https://` addresses, or `http://` for an onion address; both
  or neither; they may be the same page), plus a pair for each other
  interface language whose versions you publish: `HOSTING_TERMS_URL_IT` and
  `HOSTING_PRIVACY_URL_IT` for visitors whose portal language is Italian,
  `HOSTING_TERMS_URL_DE` and `HOSTING_PRIVACY_URL_DE` for German (any other
  visitor gets the address above).
* **The static site (default):** without those settings, `/terms/` and
  `/privacy/` on the base domain, or `/terms/index.it.html` and
  `/privacy/index.it.html` in Italian; German visitors get the English
  pages. The site is served from `/opt/bananawiki/site/` and never touched
  by updates: put your own legal texts there, in English and Italian.
  Without a base domain the links lead to the help centre.

The name the platform goes by in its pages, its emails and authenticator apps
is `HOSTING_SERVICE_NAME` (default `BananaWiki Hosting`); the footer keeps
naming the software and its version next to the source link.

The sign-up form records the version of the terms each account accepted:
`HOSTING_TERMS_VERSION`, the version date printed on the terms and privacy
pages you publish. Unset, it is the version this release ships with
(`TERMS_VERSION` in `bananawiki/hosting/accounts.py`); the portal never
guesses which pages, or which effective date, you publish. Set it together
with `HOSTING_TERMS_URL`: with your own addresses and no version, the portal
logs a warning at start, because accounts would be recorded as accepting the
release's default version rather than yours. Neither setting asks accounts to
accept anything again; only `HOSTING_TERMS_REQUIRED_VERSION` does (below). To
publish new terms:

1. Publish the new pages with their version and effective dates, and
   set `HOSTING_TERMS_VERSION` to the new version date in `config/app.env`
   (restart the portal): from then on new accounts accept the new version.
2. Tell account holders as the terms require (at least 30 days before the
   effective date, by email or with a portal banner).
3. To have existing accounts accept the new terms explicitly, set
   `HOSTING_TERMS_REQUIRED_VERSION` to that version date in `config/app.env`
   on the effective date and restart the portal. Every account that accepted
   an older version, or none (accounts an administrator created, or older
   ones), is then sent to **New terms of service** (`/account/terms`) before
   anything else in the portal, and its API tokens and its sign-ins to
   hosted wikis are refused until it accepts; accepting records the version
   and an `account.terms_accepted` entry in the moderation history. Someone
   who does not accept can still download their data, revoke their tokens,
   delete their account or sign out. Left unset, nobody is asked, and the
   terms' own change clause applies. A required version later than
   `HOSTING_TERMS_VERSION` could never be accepted: the portal (and its
   maintenance service and command line) refuses to start with it and says
   so, rather than quietly asking for an older version.

The contact address shown to customers is `HOSTING_CONTACT_EMAIL`.
