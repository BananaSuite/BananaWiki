# Custom domains

Custom domains work in the hosting platform's subdomain mode. A platform administrator enables the feature separately for each instance. An owner cannot claim a name merely by sending a matching `Host` header.

## Operator setup

1. Install `deploy/Caddyfile.hosting` as described in [deployment](deployment.md). Keep the portal's Gunicorn port on loopback.
2. Create a DNS-only `A`/`AAAA` record such as `domains.example.org` pointing at this server. Set `HOSTING_CUSTOM_DOMAIN_TARGET=domains.example.org` in the portal environment. Optionally set `HOSTING_CUSTOM_DOMAIN_IPS` to a comma-separated list of the server's public addresses for explicit address verification.
3. Allow incoming TCP ports 80 and 443 so Caddy can validate and renew certificates. A firewall or proxy that blocks certificate validation will prevent HTTPS from becoming available.
4. Restart the portal. Open an instance's **Custom domain** page as a platform administrator and enable its permission.

Caddy asks `/internal/domains/authorize?domain=...` before issuing an on-demand certificate. The endpoint returns success only for a permitted, verified custom domain or an existing managed instance address whose account and instance can serve traffic. Unknown and unverified names are refused. The endpoint does not provision instances or accept ownership claims.

## Owner setup

Open your instance's **Custom domain** page, enter a hostname such as `wiki.your-domain.org`, and save it. Use a hostname without a scheme, path, port, or wildcard.

Create the two records shown on that page:

| Type | Name | Value |
| --- | --- | --- |
| `TXT` | `_bananawiki-challenge.wiki.your-domain.org` | The random verification value shown by the portal |
| `CNAME` | `wiki.your-domain.org` | The hosting service's domain target |

Some DNS providers expect a relative name, such as `_bananawiki-challenge.wiki`, when editing the `your-domain.org` zone. Check the complete resulting name in the provider's preview.

For a root domain, use an `ALIAS`/`ANAME` record where supported, or the service's published `A`/`AAAA` addresses. All returned addresses must belong to the hosting service. Remove old address records that point elsewhere. Use DNS-only records while configuring this service; a proxy that hides the configured address or intercepts validation can prevent verification.

Wait for DNS propagation, then choose **Verify DNS**. Both ownership and routing must match. Keep the TXT record in place after verification. Caddy obtains a certificate when the hostname is first visited; the first HTTPS request can take longer while that happens.

## Permission and lifecycle

An instance can have one custom domain. Saving another hostname replaces its previous binding, so plan DNS changes before switching. An unverified claim expires after an hour and cannot reserve a name indefinitely.

Verified ownership is rechecked in the background. A successful check is valid for 24 hours; repeated DNS failures eventually stop routing. Keep the TXT and routing records intact, and use **Verify DNS** after fixing them.

Revoking the instance's permission removes its domain binding immediately. Suspension, account denial or deletion, instance expiry, and expired verification stop access and new certificate authorization. Removing DNS records alone does not cancel a hosting account or delete wiki data.

For troubleshooting, confirm that the instance is running, its owner is approved, permission remains enabled, both DNS records are public, and ports 80/443 reach Caddy. The administrator's moderation history records domain and account decisions.
