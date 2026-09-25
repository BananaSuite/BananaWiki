# Move BananaWiki to another server

The managed command uses one portable package for both deployment modes. It includes the exact source revision, databases, uploaded files, tenant data, signing/encryption keys, application settings, the static site, and private Git credentials. Python environments and container images are rebuilt on the destination. Operating-system packages, DNS, firewall rules, external storage, external services, and the host's shared proxy configuration are managed separately.

## Export the old server

```sh
sudo bananawiki migrate --output /root/bananawiki-migration.tar.gz
```

The command briefly stops this installation and its hosted tenants, writes a checked package, and returns services to their prior running state. Store the package outside the installation root before an uninstall. It is a private, unencrypted archive with mode `0600`; it contains user data, signing keys, and repository credentials. Transfer it over SSH and retain a separate protected copy.

For a final cutover, prevent further writes on the old server after making the package. A migration backup alone does not stop later users from changing the old installation. Keep one authoritative server until the new one has passed checks and DNS has been switched.

## Install and restore together

On a new server, install the [operating-system prerequisites](deployment.md#requirements) and obtain a clean compatible BananaWiki checkout:

```sh
sudo ./banana install --restore /root/bananawiki-migration.tar.gz
sudo bananawiki status
sudo bananawiki proxy
sudo bananawiki proxy --install
```

The saved deployment mode is selected automatically. The bundled source is used even if the update repository is unavailable or private. Its Python dependencies and, for hosting, the Docker base image still require access to their package registries. The package's source is trusted software: restore packages from an operator you trust.

For a different public hostname, add `--domain new.example.org`. Hosting derives `portal.new.example.org`; review `config/app.env`, the generated proxy, DNS, custom domains, OAuth callbacks, and external integrations before opening traffic. A different installation location can be chosen with `./banana --root /opt/bananawiki-new install --restore PACKAGE --name bananawiki-new`.

Automatic updates are always disabled after restoration, even when the old server had them enabled. Check the restored source access with `sudo bananawiki source check` and opt in again only when ready.

## Restore after installation

You can install an empty matching mode first, then restore later:

```sh
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki restore /root/bananawiki-migration.tar.gz
```

The restore command saves a `before-restore` package of the current data before replacing it. It refuses to change a wiki deployment into a hosting deployment or vice versa. Use a separate root and an explicit content migration for a mode change. Packages are validated for product, hashes, regular files, safe paths, and SQLite integrity before application.

Verify administrator/user login, pages, history, attachments, chat, plugins, and a new backup. On hosting, also verify instance startup, portal access, custom domains, and the separately deployed website. Readiness checks run automatically, but they do not prove every custom integration works.

## Recovery

An update or restore interrupted after its backup is journaled is recovered with `sudo bananawiki recover`; later maintenance operations also attempt that recovery first. Failed readiness restores the previous source and data together. Do not roll a migrated database back by changing code alone. `sudo bananawiki rollback` selects the package saved before the last successful update; `rollback --package PATH` selects another reviewed package.

If the new installation cannot become ready, keep traffic on the old server, inspect `journalctl -u bananawiki` and `bananawiki status`, correct the cause, and retry. A failed fresh install can reuse its retained data with the same `install --mode ... --reuse-data` options. Keep migration packages until the new installation and a subsequent backup have been verified.

## Full-site export and import in the wiki

The wiki's **Site Migration** page, linked from the admin settings, exports the whole wiki as one ZIP and imports such a ZIP. It is how a standalone wiki from an older release is moved, and it also works between two wikis of this release. It is separate from the `bananawiki migrate` package described above.

Both actions ask for the admin's password again, even inside a signed-in session, and both are written to the wiki log: `export_site` and `import_site` when they run, `export_site_refused` and `import_site_refused` when the password is missing or wrong. An admin who is impersonating another account has to stop impersonating first.

The export holds a full copy of the database (`bananawiki.db`) and a JSON dump of it (`site_export.json`): every page, chat and setting, and every account with its password hash and API token hashes. The database copy also keeps the sign-in session records (hashed session tokens, IP addresses and browsers), which the JSON dump leaves out and an import through this page does not restore. The export also holds uploaded files and `config.py`, but not `.secret_key` or log files. Anyone with a copy can try to recover the passwords offline and can read everything on the wiki, so keep it private, move it over an encrypted connection, and delete copies you no longer need.

An import gives complete control of the wiki. Every mode can add accounts with any role, owners and superusers included, and every mode signs everyone out. The modes differ in what they do to what is already there:

- **Delete all previous data and restore from file** replaces everything. It is refused before anything is deleted unless the file contains an admin or owner account, a finished setup and a home page.
- **Keep previous data and restore from file (override conflicts)** keeps the rows the file does not mention and replaces the ones it does. An account in the file replaces a local account with the same id or the same username, the owner's included.
- **Keep previous data and restore from file (keep conflicts as-is)** is a merge. Local rows win every conflict, and accounts that already exist are left exactly as they are: the file's copy of such an account is ignored, and so are the role and deletion schedules, API and userbot tokens, and account merge requests that point at it. After the import the wiki reports how many of those rows it left out.

The **Restore system configuration files** option also writes `config.py`, `.secret_key` and the database file from the ZIP when they are present (the keep mode only writes files that are missing). A replaced `config.py` runs as code on the next restart, so use the option only for backups you made yourself.

Owner status protects an account from being demoted or deleted through the normal interface. It does not protect against an admin who imports a backup or installs a plugin, since either one can rewrite any account. Give the admin role only to people you trust with everything.

To move a wiki to a fresh installation, finish setup with a temporary account, complete its onboarding, open **Site Migration**, and import the old export with **override conflicts** or **Delete all previous data**. With override, an exported account that has the same username as the temporary account replaces it; under a different username the temporary account stays next to the imported ones as a second owner. Other admins cannot delete an owner, so sign in with the temporary account once more and delete it from its own account settings. With delete all, only the exported accounts remain. The import signs you out: sign in again with an exported account and its old password.

## Older installations

The new `banana` package format replaces the old deployment scripts. An old `deploy.sh` bundle, a wiki export ZIP, and an encrypted platform export are different formats; do not pass them to `banana restore`. The hosting portal's restore form accepts the hosting portion of old deployment bundles as described below.

For a standalone wiki, use the old release's administrator full-site migration export. Install the new managed `wiki` mode, create its temporary administrator, and import the full-site ZIP through the administrator migration interface as described in [Full-site export and import in the wiki](#full-site-export-and-import-in-the-wiki). Restart with `sudo bananawiki restart`, log in with the imported accounts, and verify content and assets. Preserve operator-only environment settings, external plugins, and integrations separately when the old export did not include them.

For a hosting platform, use its full-platform export and separately save the platform backup encryption key. Install an empty managed `hosting` mode. Before uploading an encrypted `.bwenc` backup, install the old raw 32-byte key at the new installation's configured `HOSTING_BACKUP_KEY_PATH` (normally `/opt/bananawiki/data/.backup_encryption_key`), owned by the service account with mode `0600`; an explicit `HOSTING_BACKUP_ENCRYPTION_KEY` environment value takes precedence. Restore through the platform administration interface and restart with `sudo bananawiki restart`. Restore onto a platform with no tenant instances. Copy a customized static website into the new `site/` directory and restore external email/SSO/backup settings separately. The platform restore uses the new instance root, retains account/instance IDs and wiki data, and reconstructs omitted relative storage aliases.

### Old deployment bundles and disabled export buttons

Some old releases left the portal's full-backup button connected to a disabled Telegram backup function. If it produces no backup, use the existing release's `./deploy.sh migrate` command. Confirm that its configured application, instance, and landing paths match the running deployment. Preserve a server snapshot and any data or environment-provided keys outside those paths separately. The version-three exporter uses SQLite snapshots, but the final export still needs all writers stopped to keep database records and attachments consistent.

The new hosting **Platform Settings → Disaster Recovery** form accepts the resulting deployment ZIP on an empty platform. It restores `hosting/hosting.db`, `hosting/instances/`, the platform session key, and the backup encryption key when included. Customer ZIP attachments remain ordinary files. It deliberately does not install the bundle's `landing/`, `configuration/`, `wiki/`, or `sites-data/` entries. Copy the landing files separately, review operator settings, and migrate any independent wiki or additional site to its own matching installation. Do not upload the deployment ZIP to the standalone-wiki importer or pass it to `banana restore`.

Before a rehearsal import, save an empty managed baseline with `sudo bananawiki backup --output /root/empty-hosting-baseline.tar.gz`. After testing the copy, restore that baseline on the new, still-private server before importing the final legacy export. The portal refuses to overwrite an installation that already has tenants. Restoring the empty baseline discards the rehearsal data; it is only for this pre-cutover step and must never be used after the new server starts accepting real writes.

Keep only one installation writable during cutover. Block old public traffic and stop old application workers, scheduled jobs, and tenant processes before taking the final export. Verify the final restore before changing DNS, including both A and AAAA records, tenant/custom domains, and HTTPS. Hosts-file overrides do not make a public certificate authority validate the new server; use a deliberate test certificate or temporary hostname for full HTTPS rehearsal. An SSH tunnel can test the local portal without changing public DNS.

Keep the old installation and its service files available until the migration is verified. Install on another server or use a different root, port, and service name during transition; the new installer refuses to overwrite unrelated services. Afterwards, disable the old services using the old release's instructions. Future updates and migrations use only the managed command.
