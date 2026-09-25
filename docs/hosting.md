# Hosting platform

The hosting portal provisions separate BananaWiki instances. It has its own accounts, database, administration pages, and lifecycle controls. Each wiki keeps its own users, content, session key, and data directory.

Follow [deployment](deployment.md) to build the tenant Docker image, configure the portal and Caddy, and create the first administrator with `HOSTING_BOOTSTRAP_TOKEN`. The recommended Internet deployment uses HTTPS subdomains. Port mode is intended for local testing or a private network behind an appropriate proxy.

## Accounts and moderation

Administrators choose whether registration is open, invite-only, or closed, and whether new accounts need approval. When approval is required, pending users cannot create or manage instances. Approvals and declines can include a reason; the portal records the decision and actor in moderation history. Declined accounts can be reconsidered.

When approval is required, admins can set a notification email under Admin settings → Email and account policy → Pending-approval notifications. Digest mode (default), which is safe for provider quotas, sends one email per interval listing everyone still waiting. The interval is configurable from 1 to 72 hours, and the maintenance service sends the digests. Immediate mode sends one email per signup instead, but still counts against the platform's email rate limits. Leaving the address empty disables these notices. Email delivery itself must be configured (Brevo, Resend, or SMTP); without it, no notices are sent.

Account and instance suspensions are separate controls. A suspension can be indefinite or timed. Administrators can choose whether the owner sees its reason and end time. Suspending an account blocks its access; use the option to suspend its instances when their public access must also stop. Unsuspending an instance credits its hosting expiry for the suspended time before restarting it. The moderation history preserves the previous decisions.

The hosting service may apply storage, upload, account, and duration limits. Those are service settings, independent of the AGPL source license. The software license allows both personal and commercial use.

## Domains

With `BASE_DOMAIN=example.org` and `INSTANCE_URL_SUFFIX=hosting`, an instance named `notes` uses `notes-hosting.example.org`. `PORTAL_DOMAIN` selects the portal address. Unknown hostnames never fall back to the portal.

A platform administrator must enable custom domains for each instance. The owner then adds a hostname and verifies its DNS ownership. Only verified, active claims route to a running, permitted instance. Revoking permission or suspending the instance disables custom-domain routing. Follow the [DNS and TLS guide](custom-domains.md) before enabling this feature.

## Data and lifecycle

Stopping an instance preserves its data. Restarting keeps the same wiki and credentials. Termination retains data for the configured recovery window; a hard deletion or expired retention removes it. Read the action and retention details shown in the portal before applying a destructive operation.

Platform exports include the portal database, tenant data, and required secrets. They are encrypted. Download the encryption key separately and store it outside the server. Restore onto a fresh platform with no instances, then restart it. For wiki-level migration, use each wiki's export and import tools.

## Plugins and tenant code

With the Docker runtime, the admins of a hosted wiki can install Python plugins. A plugin runs as the wiki itself inside that wiki's container, so installing one, or importing a full-site backup, gives complete control of the wiki: its content, users, settings and files. Give the admin role in a wiki only to people trusted with everything. Owner status protects an account against being demoted or deleted through the normal interface, not against an admin who installs code.

The platform's boundary is the container. Plugin code cannot reach the portal, other wikis or the host's files. The portal's operations on a wiki's database and files (starts, quarantine, snapshots, password resets, exports, duplicates and resets) treat everything in the wiki's data folder as untrusted: they do not follow links there or open special files, and exports and duplicates leave those out.

Platform administrators have these recovery tools on each instance's admin page:

- **Quarantine custom plugins** stops the wiki, saves a copy of its database for investigation, disables every plugin whose code sits in the wiki's external plugins folder, and restarts the wiki with external plugins switched off. The platform records the quarantine outside the wiki's data, so restarts, the wiki's own settings and a wiki reset cannot undo it. It stays until an administrator uses **Lift plugin quarantine**.
- **Save plugin safety snapshot** stores a copy of the wiki's database on the platform. Take one while the wiki is in a known-good state, before its admins enable custom plugins.
- **Restore plugin safety snapshot** rolls the database back to the newest of those copies, or to the one you pick, after checking it against the checksum recorded when it was saved. It saves the current database first, and it quarantines custom plugins, since their code is still on disk. The copies a wiki makes in its own `plugin_safety_snapshots` folder before enabling a plugin are never used here, because plugin code can rewrite them. The platform keeps the five newest copies of each kind.

These actions are recorded in the instance's event history. Resetting a wiki password from the portal also revokes the API tokens of the accounts it resets. Instance pages load at most 200 of a wiki's users at a time.

When a shared GPU text-to-speech server is configured, the portal hands its address and token to each wiki the TTS policy allows through that wiki's environment. Earlier versions copied them into every wiki's database, where a wiki admin could read the token; each wiki's copy is cleared the next time it starts, so rotate the token after upgrading. Plugin code in an allowed wiki can still read its own environment, so allow only wikis you trust with the token, and rotate it if one misuses it.

## Operator references

- [Quick start](../hosting/docs/getting-started.md)
- [Environment settings](../hosting/docs/configuration.md)
- [Operator CLI](../hosting/docs/admin-cli.md)
- [Runtime architecture](../hosting/docs/architecture.md)
- [Migration](../MIGRATION.md)

The portal serves its own help centre at `/help`, in English and Italian, from `hosting/content/help.*.json`. It follows the visitor's interface language and needs no configuration. `site/` is an editable placeholder for the apex website, and there is no website editor in the portal.
