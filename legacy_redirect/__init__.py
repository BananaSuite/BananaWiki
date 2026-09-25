"""Legacy domain redirect daemon.

Serves a branded "We moved" page on every wildcard subdomain of one or
more old domains, with a button that sends the visitor to the same
subdomain on the new domain.

This daemon is optional and stands on its own.  The BananaWiki installer
does not set it up; install it yourself when you move a deployment to a
new domain and want the old subdomains to keep working.

Installation:
    sudo bash legacy_redirect/manage.sh install
    sudo bash legacy_redirect/manage.sh add old.example.com
    sudo bash legacy_redirect/manage.sh set-target wiki.example.net
    # Then point the old domain's wildcard records and a proxy server
    # block at the port below.  See docs/deployment.md.

Installation (by hand):
    sudo cp legacy_redirect/bananawiki-legacy-redirect.service /etc/systemd/system/
    # Edit the service file: LEGACY_OLD_DOMAINS and LEGACY_NEW_DOMAIN have
    # placeholder values and the service will not start until you change them.
    sudo systemctl daemon-reload
    sudo systemctl enable --now bananawiki-legacy-redirect

Uninstallation:
    sudo bash legacy_redirect/uninstall.sh

Usage (standalone, for testing):
    python -m legacy_redirect --old old.example.com --new wiki.example.net
    python -m legacy_redirect --old old.example.com --new wiki.example.net --port 8090
    python -m legacy_redirect --old old.example.com --new wiki.example.net
    python -m legacy_redirect --old old1.example.com --old old2.example.org \
        --new wiki.example.net

Environment variables:
    LEGACY_OLD_DOMAINS   Comma-separated list of old domains to match (required)
    LEGACY_OLD_DOMAIN    Single old domain (backward compat, added to the list)
    LEGACY_NEW_DOMAIN    New domain to redirect to (required)
    LEGACY_NEW_SCHEME    URL scheme for redirect links (default: https)
    LEGACY_BIND_HOST     Address to bind to (default: 0.0.0.0)
    LEGACY_BIND_PORT     Port to listen on (default: 8090)

No external dependencies: uses only the Python standard library.
"""
