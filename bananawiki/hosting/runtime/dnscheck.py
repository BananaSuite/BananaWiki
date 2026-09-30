"""DNS checks for custom domain claims (1.4 ``hosting/domains.py``, dnspython, 4 s lifetime).

A record proxied by Cloudflare (orange cloud) hides both the CNAME and the
origin address: public DNS shows only Cloudflare's edge addresses. The TXT
proof still shows the claimant controls the zone, so with *allow_proxied*
(``HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED``, on by default) a domain whose
addresses all belong to Cloudflare counts as routed and is reported as
``proxied``; the domain page then explains the Cloudflare settings it needs.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from ...core.cloudflare import all_cloudflare
from . import DomainCheck, RuntimeFailure

LIFETIME_SECONDS = 4.0
CHALLENGE_PREFIX = "_bananawiki-challenge."


def _resolver():
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        raise RuntimeFailure("not_configured", "dnspython is not installed") from None
    return dns.resolver, dns.exception


def _records(resolver, name: str, record_type: str) -> set[str]:
    answer = resolver.resolve(name, record_type, search=False, lifetime=LIFETIME_SECONDS)
    if record_type == "TXT":
        return {b"".join(row.strings).decode("utf-8", "replace") for row in answer}
    return {row.to_text().rstrip(".").lower() for row in answer}


def _optional(resolver, name: str, record_type: str) -> set[str]:
    try:
        return _records(resolver, name, record_type)
    except (resolver.NoAnswer, resolver.NXDOMAIN):
        return set()


def _addresses(resolver, name: str) -> set[str]:
    return _optional(resolver, name, "A") | _optional(resolver, name, "AAAA")


def check(domain: str, token: str, target: str, allowed_ips: Iterable[str], *,
          allow_proxied: bool = True) -> DomainCheck:
    """Ownership (TXT proof) and routing (CNAME to *target*, A/AAAA within the allowed addresses,
    or, with *allow_proxied*, only Cloudflare edge addresses)."""
    resolver, exception = _resolver()
    proxied = False
    try:
        proofs = _records(resolver, CHALLENGE_PREFIX + domain, "TXT")
        ownership = any(hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8")) for value in proofs)
        target = target.lower().rstrip(".")
        routing = bool(target) and target in _optional(resolver, domain, "CNAME")
        if not routing:
            expected = set(allowed_ips) or (_addresses(resolver, target) if target else set())
            actual = _addresses(resolver, domain)
            routing = bool(actual) and bool(expected) and actual <= expected
            if not routing and allow_proxied and all_cloudflare(actual):
                routing = proxied = True
    except exception.DNSException:
        return DomainCheck(ownership=False, routing=False, dns_error=True)
    return DomainCheck(ownership=ownership, routing=routing, proxied=proxied)
