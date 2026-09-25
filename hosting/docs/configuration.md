# Hosting configuration

Use [deploy/hosting.env.example](../../deploy/hosting.env.example) as the starting point. The full environment reference is the commented [hosting/config.py](../config.py); platform settings changed in the admin UI are stored in the hosting database.

| Variable | Purpose |
| --- | --- |
| `BASE_DOMAIN` | Base for managed wiki addresses |
| `PORTAL_DOMAIN` | Canonical portal hostname |
| `INSTANCE_URL_SUFFIX` | Suffix in managed instance hostnames |
| `HOSTING_HOST`, `HOSTING_PORT` | Portal listener; keep it on loopback behind Caddy |
| `HOSTING_PROXY_MODE` | Trust forwarded headers only from a protected proxy listener |
| `HOSTING_BOOTSTRAP_TOKEN` | Token required to create the first platform administrator |
| `HOSTING_INSTANCE_RUNTIME` | Set to `docker` for tenant isolation |
| `HOSTING_TENANT_NETWORK` | `isolated` keeps tenant containers off the network, `outbound` lets them reach the Internet, the host's LAN and services on the Docker bridge. Defaults to `isolated` in subdomain mode and `outbound` in port and onion mode. With the Docker runtime, `isolated` is rejected at startup in port and onion mode, because Docker publishes no ports from an internal network |
| `HOSTING_TENANT_PLUGIN_DENYLIST` | Comma-separated plugin ids to keep off every tenant, on top of the wiki's own list. The wiki matches ids only, so this is a policy switch rather than a security boundary: the same code under another id is not caught |
| `HOSTING_PLATFORM_STATE_DIR` | Where the portal keeps per-wiki state that tenants must not reach: the plugin safety snapshots it saves and the plugin quarantine markers. Defaults to `platform_state` next to the hosting database. The portal refuses to start if it is inside `INSTANCES_DIR` |
| `HOSTING_CONTAINER_IMAGE` | Image built from the deployed public source |
| `HOSTING_CUSTOM_DOMAIN_TARGET` | DNS target that enables custom-domain claims |
| `HOSTING_CUSTOM_DOMAIN_IPS` | Optional server addresses for A/AAAA verification |
| `HOSTING_CONTACT_EMAIL` | Address the portal, its emails and its help centre tell people to write to. Falls back to the address in `HOSTING_EMAIL_REPLY_TO`, then, in subdomain mode, to `contact@` your `BASE_DOMAIN`. Set it in port and onion mode, where there is no domain to fall back to; the portal logs a warning until you do |
| `HOSTING_STATUS_URL` | Optional status page, linked from the footer of transactional emails. Must be an `http` or `https` URL without credentials |
| `BW_SOURCE_URL` | Corresponding source for the deployed version |

Preserve the hosting database, session key, backup encryption key, `HOSTING_PLATFORM_STATE_DIR` and tenant directories across updates. The platform backups the portal makes, for download or cloud storage, do not include `HOSTING_PLATFORM_STATE_DIR`, so a restore from one of them loses plugin quarantines and saved snapshots. The installer's `bananawiki backup` covers the folder while it stays in its default place next to the hosting database. Keep environment files private. Never pass platform secrets or bootstrap tokens into tenants.

See [deployment](../../docs/deployment.md), [moderation and lifecycle](../../docs/hosting.md), and [custom domains](../../docs/custom-domains.md) for complete setup steps.

The portal's [REST API](api.md) is switched on in Platform Settings rather than the environment, and stays off until an administrator does so.
