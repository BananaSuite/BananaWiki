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
| Caddy | | HTTPS: the static site on the base domain, the portal on the portal domain, and one site block per running wiki (written by the agent to `/var/lib/<service>-routes/tenants.caddy`), so wiki traffic goes straight to each container. Unknown hosts fall through to the portal's "not available" page. Certificates for wikis and custom domains come through on-demand TLS. |

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
* Secrets for a tenant (its OAuth client secret, the shared GPU token, the
  first administrator's password) reach it through a private `--env-file`,
  never on a command line.
* Tenant databases are only opened inside the tenant's sandbox: migrations,
  seeding the first administrator, user management, analytics and plugin
  quarantine run as `tenant.task` operations of the agent (`docker exec` for a
  running wiki, a one-shot container for a stopped one), never in the portal.
* Tenants run with `BW_MANAGED_HOSTING=1`: site import off, the host owns the
  upload limits and the GPU settings, public builder pages off, third-party
  plugins only in container isolation and never those in
  `HOSTING_TENANT_PLUGIN_DENYLIST`.

Not yet isolated: all tenant containers run as the service account's UID (the
portal reads tenant files for backups and exports), and the storage limit is
enforced by the wiki inside the container (use file-system quotas on the host
if tenants must not be able to fill the disk).

## Addresses

* **Subdomain mode** (`BASE_DOMAIN` set to a domain name): wikis at
  `<name>-<suffix>.<BASE_DOMAIN>` (suffix `hosting` by default, configurable in
  the platform settings or with `INSTANCE_URL_SUFFIX`; empty for
  `<name>.<BASE_DOMAIN>`). The single-level host names are covered by a
  wildcard certificate or on-demand TLS.
* **Port mode**: wikis at `http(s)://<HOSTING_PUBLIC_HOST>:<port>` with ports
  from `INSTANCE_PORT_START` to `INSTANCE_PORT_END`. Needs
  `HOSTING_TENANT_NETWORK=outbound`.
* **Onion mode**: for Tor hidden services; also needs outbound networking.

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

## Accounts

* **Sign-up modes** (platform settings): open, invite codes, approval by an
  administrator, or closed. The
  first account is the administrator and needs `HOSTING_BOOTSTRAP_TOKEN`.
* Email verification, password and user-name recovery, and notices by email
  through Brevo, Resend or SMTP (`HOSTING_EMAIL_*`), with daily limits.
* Two-step sign-in with an authenticator app and one-time recovery codes
  (administrators can be required to use it); sessions listed and revocable;
  "sign out everywhere".
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

A wiki lives `INSTANCE_DURATION_DAYS` unless an administrator extends it or
makes it indefinite. When it expires or is terminated, it enters the grace
period configured in the platform settings, during which an administrator can
restore it; afterwards its data is deleted.

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
  kept for a configurable number of days.
* **Platform export** from the administrator pages, encrypted with the key in
  `HOSTING_BACKUP_KEY_PATH`.

Tenants are part of every managed update: the updater stops them, and the
maintenance service starts every wiki whose status is `running` again with
the new image; the update is rolled back if any of them does not become
healthy.

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
