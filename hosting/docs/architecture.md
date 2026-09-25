# Hosting architecture

The hosting portal is a Flask application served through `hosting.wsgi:app`. It owns account registration, moderation, instance provisioning, routing, and platform backups. A separate SQLite database records these operations.

`instance_manager.py` creates and manages tenant data directories and delegates Docker operations to `container_runtime.py`. Each tenant gets a private writable data mount, its own session key and users, and a loopback host port. Containers have a read-only root filesystem and bounded CPU, memory, and process counts. They do not receive the Docker socket or the portal's environment secrets. The service account that controls Docker has host-level privileges and must be reserved for trusted operators.

The manager coordinates lifecycle mutations and re-exports its existing API. `instance_paths.py`, `instance_environment.py`, `instance_database.py`, `instance_archives.py`, and `instance_diagnostics.py` separate filesystem naming, tenant configuration, database preparation, portable archives, and read-only status queries. These modules do not depend on the lifecycle manager.

`routes/dashboard.py` registers smaller route groups for owners, account administration, instance administration, feature requests, collaboration, archives, and settings. Shared authorization checks remain in `routes/dashboard_common.py`. `routes/api.py` serves the token-authenticated [REST API](api.md) under `/api/v1` and reuses those checks rather than keeping its own. See the [development guide](../../docs/development.md) when changing these boundaries.

`_subdomain_proxy.py` and `subdomain.py` route managed hostnames and verified custom domains to active tenants. They reject unknown hostnames. `domains.py` verifies TXT ownership plus the serving DNS records, expires stale verification, and authorizes certificate requests only for allowed names. Caddy terminates HTTPS using the supplied configuration.

The lifecycle code applies account and instance suspensions, service expiry, retention windows, and recovery. Hosting duration limits do not register or license the wiki software. Moderation events identify the action, actor, target, time, and reason.

`backups.py` snapshots SQLite and tenant files into an encrypted platform export. Restore validates archive paths and the database before installing data on an empty platform. External website files are deployed and backed up separately.

Tenant data directories are writable from inside their containers, so portal code treats every entry in them as hostile. `instance_environment.py` provides the helpers for that: files are opened relative to the data directory without following links and only when they are regular files, the portal's own files in that directory are replaced rather than written through, and a tenant database is opened only after checking which file SQLite actually opened, with triggers and views switched off and a time limit on its statements. Exports, duplicates and resets skip or remove links instead of following them. What the portal must keep out of a tenant's reach, the plugin safety snapshots it takes and the plugin quarantine markers, lives under `HOSTING_PLATFORM_STATE_DIR`, next to the hosting database and outside every tenant mount.

The legacy process runtime is available for local development. For a service hosting other people's wikis, use the [Docker deployment](../../docs/deployment.md). With the Docker runtime, tenant admins can install external Python plugins, which run with their wiki's privileges inside its container; a quarantined tenant starts with them switched off, and the process runtime never loads them.
