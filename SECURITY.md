# Security reports

Report a vulnerability through [GitHub private vulnerability reporting](https://github.com/BananaSuite/BananaWiki/security/advisories/new) when it is enabled. Include the affected revision, reproduction steps, impact, and any suggested fix. Avoid placing credentials or private user data in a report.

If private reporting is unavailable, contact [Luca Zani (OverloadedTech)](https://github.com/OverloadedTech) using the contact information on that profile. Use a public issue only to request a private reporting channel; do not publish exploit details or private data there.

## Trust model

Admins are fully trusted. Any admin can install a plugin or import a full-site backup, and either one gives complete control of the wiki: an external plugin runs inside the wiki process with the wiki's own privileges, and a backup can replace any account. Owner status protects an account from being demoted or deleted through the normal interface, not from an admin who installs code or imports a backup. Give the admin role only to people you would trust with everything.

The admin pages say this where the decision is made, ask for the admin's password again before an external plugin is imported, enabled or deleted and before a backup is exported or imported, and log each of these actions. External plugins are on by default on a self-hosted wiki; set `BW_ALLOW_EXTERNAL_PLUGINS=0` to turn them off. See [Plugins](docs/plugins/overview.md#what-an-external-plugin-can-do) for what a plugin can do and how to recover from a bad one.

An admin gaining owner or superuser rights through a plugin or a backup is therefore expected behaviour, not a vulnerability. A way for someone without the admin role to reach those powers is one, and so is a way to run plugin code that no admin enabled.

## Supported versions

Security fixes target the current public release. Operators should keep dependencies and operating-system packages current and test backups before upgrades.
