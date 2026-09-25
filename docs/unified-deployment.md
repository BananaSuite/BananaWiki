# One deployment command

Use [the deployment guide](deployment.md) and the `banana` command. Install once with `--mode wiki` or `--mode hosting`; subsequent `bananawiki update`, `backup`, `restore`, and `uninstall` commands remember that choice.

Automatic updates are off by default. The same command configures a custom Git URL, branch, explicit fallback, and private-repository token or SSH key. [Server migration](server-migration.md) covers installing and restoring together or restoring later.

## Existing nginx deployments

An existing nginx proxy can stay in use. Forward only to the application's private loopback listener, overwrite forwarded headers, support the application's request sizes/timeouts, and serve public traffic over HTTPS. The generated `bananawiki proxy` output documents the supported Caddy equivalent.

The old `deploy.sh`, `install.sh`, `update.sh`, and `uninstall.sh` entrypoints have been retired from the new release. Preserve an old checkout while migrating its data and services using the [legacy migration path](server-migration.md#older-installations). The new installer deliberately refuses to overwrite unrelated services or proxy configuration.
