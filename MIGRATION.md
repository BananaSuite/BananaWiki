# Upgrade an existing BananaWiki installation

BananaWiki now uses one managed `banana` command for installation, updates, backups, migration, restoration, and uninstall. Choose `wiki` or `hosting` at installation; the command remembers the mode. Automatic repository updates are optional and stay off until you enable them. HTTPS token and SSH deploy-key authentication support private forks and repositories.

Follow [server migration](docs/server-migration.md), especially [older installations](docs/server-migration.md#older-installations), before retiring an existing deployment. Export a standalone wiki through its administrator migration interface, or export the entire hosting platform and save its encryption key separately. Restore into a new matching managed installation and verify it before redirecting users. Old shell deployment bundles and application export ZIPs are not interchangeable with new `banana` installation packages.

If the old hosting export button produces no backup, use that release's `./deploy.sh migrate`. The new hosting Disaster Recovery form accepts the hosting portion of its deployment ZIP, including account/instance IDs, tenant files, and available keys. Landing files, operator configuration, standalone wikis, and additional sites still need separate migration. See [legacy deployment bundles](docs/server-migration.md#old-deployment-bundles-and-disabled-export-buttons) for the rehearsal and final-restore sequence.

Telegram backup delivery has been retired. Move scheduled backups to [repository backups](docs/backups.md); a hosting platform that used Google Drive can keep it.

Existing databases migrate at application startup. Pages, accounts, histories, tenant metadata, and hosting-service limits remain. Software registration screens and keys, commercial-build expiry, and commercial-use declarations have been removed. Migrations drop obsolete licensing tables and columns. Instance expiry remains an operator's hosting-service policy.

Keep a custom website separate from source. Managed hosting uses its preserved `site/` directory for the apex website. The customer help centre is built into the hosting portal at `/help`, so there is no external help address to configure any more. The bananawiki.com service website belongs to its separate private repository.

The first-run token protects new installations; completed installations retain their accounts. AGPL-3.0-only permits personal and commercial use, including the hosted service using this codebase. Keep an accessible corresponding-source offer for modified network deployments and preserve third-party notices.

## Eight more plugins are no longer part of the release

`banana_ai`, `bw_oauth_provider`, `file_manager`, `git_override` and
`oauth_login` were experimental and never installed by default. The other
three shipped but did not work as a release other people run would need:

- `feedback` refused every report unless a Telegram bot was configured, and
  when one was, it sent each reporter's IP address and username to Telegram.
- `meetings` connected calls through Google's public STUN server only, with no
  relay, so calls failed for anyone behind a strict firewall and every
  participant's address went to Google.
- `beta_testers` enrolled people into a programme whose settings the rest of
  the wiki never read. The only visible effect was a badge, which the badges
  plugin already does.

`git_override` also showed any signed-in user the history and text of pages
they were not allowed to open.

An installation that had any of them keeps its rows in the `plugins` table.
They are inert: nothing loads and their pages return 404. Delete the rows if
you want the list tidy. Their tables and settings columns stay in the database
untouched, and so does any data in them. A migration export leaves those
tables out, because several hold credentials or personal data: users' own
model API keys, OAuth client secrets and tokens, linked sign-in accounts and
reporters' IP addresses. A personal data export still includes a person's old
feedback reports and beta enrolment if the database has them.

A saved sidebar order that still names one of these apps is fine; unknown
entries are skipped. The paths `/beta-testers` and `/banana-ai` are free to use
for custom pages again.

The GPU text-to-speech token (`tts_gpu_auth_token`) is now encrypted at rest.
An existing plaintext value keeps working and is encrypted the next time the
settings are saved. As with the other encrypted settings, it reads as empty
after moving the site to a server with a different secret key, so enter it
again there. The settings API no longer returns it.

## The REST API now applies the editor's permissions

A bearer token used to be able to do anything its owner's role allowed,
ignoring the finer rules the web editor applies. It now follows the same ones,
which changes what some existing integrations will see:

- A page outside the token owner's readable categories returns `404`, and the
  page list leaves it out.
- Creating, editing, moving or deleting a page outside the owner's writable
  categories returns `403`. Moving a page needs write access to both the old
  and the new category.
- A page that is protected, or checked out by another editor, returns `409`.
- Deleting a page with Deletion Slowdown enabled returns `202` with
  `"pending_deletion": true` and starts the grace period, instead of removing
  the page at once. A script that checked for `200` needs to accept `202`.
- Deleting a page needs the `page.delete` permission, as in the wiki. Editors
  do not have it by default, so an editor's token gets `403` until an admin
  grants it through a custom role or a per-user override.
- Creating a page in a category that does not exist returns `400`.
- `GET /api/v1/pages` works again. It raised an error as soon as the wiki held
  a single page, so no integration can have been relying on it.
- Creating a category needs `category.create`, renaming one needs
  `category.edit` and moving one needs `category.reorder`, as in the wiki.
  Without them the request returns `403`.
- Moving a category with `parent_id` works now; it used to fail with a server
  error every time. A move into the category itself, into one of its own
  subcategories or under a parent that does not exist returns `400`, and so
  does a name longer than 100 characters.

Tokens owned by administrators behave as before, apart from the deletion
grace period. Tokens owned by unrestricted editors also behave as before,
except that deleting a page now needs `page.delete`.

## Six plugins are no longer part of the release

The plugins `banana_cad`, `ea_mode`, `lab_camera` and `easter_egg`, and two
novelty modes named after real people, have been dropped. They were
experiments that do not belong in a release other people are expected to run,
and three of the six were named after real people or companies.

An installation that had them keeps its rows in the `plugins` table. They are
inert: the administrator plugin list still shows them, nothing loads, and the
pages they served now return 404. Delete the rows if the clutter bothers you.
Their site settings columns stay in the database untouched and unused; nothing
reads or writes them.

The Easter Egg also owned a `users.easter_egg_found` column and the
`easter_egg` badge trigger. The column stays on an existing installation and is
no longer read. A badge that used that trigger, such as the seeded Easter Egg
Hunter, stops awarding itself and keeps every award it has already made. Edit
it to another trigger or delete it in Admin, Badges.

## Where the database file is looked for

`BW_DATABASE_PATH` still decides the database location, and the default is
still `instance/bananawiki.db` beside the application. When `BW_INSTANCE_DIR`
points somewhere else, the application now looks for the database in that
directory by default, next to the rest of the persistent data, instead of
inside the source tree.

This only affects an installation that set `BW_INSTANCE_DIR` and left
`BW_DATABASE_PATH` unset. Managed installations set both, so `banana install`
and `bananawiki update` are unaffected, as is the portable launcher. If yours
is a hand-configured installation of that kind, either move
`instance/bananawiki.db` into the directory `BW_INSTANCE_DIR` names, or set
`BW_DATABASE_PATH` to the file's current location before restarting. Starting
the application without doing one of the two creates an empty database rather
than reporting an error, so check before restarting rather than afterwards.

## Hosting tenant networking

Domain-based hosted wikis now use internal Docker networks by default. Before updating a platform that relies on remote AI, voice downloads or LAN camera integrations, review [tenant networking](docs/deployment.md) and explicitly set `HOSTING_TENANT_NETWORK=outbound` if those integrations need it. Legacy port mode retains outbound networking for Docker port publication. The host operator controls the setting. The hosting portal's own email, backup and update connections are separate from tenant networking.

When upgrading an earlier managed installation to this release, run `sudo ./banana --root /opt/bananawiki update` from the new checkout. This uses the updated deployment controller, whose readiness checks follow recreated tenants by their data directories and private addresses. Subsequent updates use the installed `bananawiki` command as usual.

## Hosting portal database schema

On first start, the hosting portal upgrades its database to schema version 2 (the REST API switch, API tokens and signup approval notification settings). Older portal versions refuse to start against it, so to roll back use `banana rollback` or restore the code and the data backup taken before the update together.

## Existing API integrations

API tokens now enforce their recorded scopes and read/write flags for every account, including administrators. Older administrator tokens that relied on unrestricted access may receive `403` responses. Review each integration and issue a token with only the scopes and write access it needs. Userbot profile updates require both the `userbot` scope and write access. Bearer-authenticated API calls work without a browser session or CSRF token; browser management forms retain CSRF protection.

A token created through the API cannot exceed its issuing token's permissions or expiry. An omitted expiry inherits the issuing token's deadline. Each token remains a separate credential; revoking the issuing token does not revoke tokens it previously created. Review and revoke those separately when retiring an integration.

This release tightens the API further. Check integrations for these changes:

- `PUT /api/v1/settings` refuses unknown keys, internal and platform-managed
  settings, the remote GPU settings (`tts_gpu_*`) and secret keys with `403`,
  and invalid values with `400`. When it refuses anything it saves nothing,
  so a request that mixes allowed and refused keys changes no setting.
- `POST /api/v1/users/bulk` creates at most 20 accounts per request.
- Usernames and passwords sent through the API follow the same rules as the
  web forms: usernames are 3 to 50 letters, digits, underscores or hyphens,
  and passwords 8 to 1024 characters.
- `suspended`, `api_access_enabled` and `enabled` must be JSON booleans or
  `0`/`1`. Strings such as `"false"` get `400`.
- Page slugs are limited to 200 characters, and category ids must be whole
  numbers.
- A token gets `403` while its account has to change its password or finish
  onboarding. In maintenance mode, tokens of accounts that are not
  administrators get `503`.
- Changing an account's password through the API revokes that account's
  tokens. Changing or resetting a password in the web interface, signing out
  of all sessions and `reset_password.py` do the same, so an integration
  whose account password changes needs a new token.
- Only administrator and owner accounts can be given the `admin`, `settings`
  and `users` scopes. The token form drops them for other accounts and
  `/api/v1/tokens` refuses them with `403`. An older token of another account
  that still carries one of them gets `403` from those endpoints, Banana Mode
  included.

New API audit entries omit request bodies. Older entries may contain plaintext passwords sent to account-creation or account-update endpoints. Review the existing audit history and retained backups, restrict their access, clear historical request bodies if they are no longer needed, and rotate affected passwords. Upgrading does not rewrite existing audit records or backup packages.
