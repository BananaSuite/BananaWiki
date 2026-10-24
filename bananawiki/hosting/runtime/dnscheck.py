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
import time
from collections.abc import Iterable

from ...core.cloudflare import all_cloudflare
from . import DomainCheck, RuntimeFailure

LIFETIME_SECONDS = 4.0
INSPECT_SECONDS = 6.0  # all of inspect()'s lookups together
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


def addresses(name: str) -> tuple[str, ...]:
    """The A and AAAA records of *name*, sorted (empty when it has none or DNS fails)."""
    resolver, exception = _resolver()
    try:
        return tuple(sorted(_addresses(resolver, name.lower().rstrip("."))))
    except exception.DNSException:
        return ()


def inspect(domain: str, token: str, target: str, allowed_ips: Iterable[str], *,
            allow_proxied: bool = True) -> dict:
    """What DNS shows for a custom domain claim, record by record, for the domain page's checklist.

    The same rules as :func:`check`, with the values found: ``txt`` (at the
    challenge name), ``cname``, ``addresses`` and ``expected_addresses``.
    Each lookup fails on its own, so one record that does not resolve yet does
    not hide the others; ``dns_error`` says that some lookup failed.
    """
    resolver, exception = _resolver()
    target = target.lower().rstrip(".")
    failed = False
    # One deadline for every lookup together: a nameserver that drops queries must not hold a portal
    # thread for a timeout per record. After a timeout, the remaining lookups are skipped.
    deadline = time.monotonic() + INSPECT_SECONDS

    def lookup(name: str, record_type: str) -> set[str]:
        nonlocal failed, deadline
        if time.monotonic() >= deadline:
            failed = True
            return set()
        try:
            return _optional(resolver, name, record_type)
        except exception.Timeout:
            failed = True
            deadline = 0.0
            return set()
        except exception.DNSException:
            failed = True
            return set()

    txt = lookup(CHALLENGE_PREFIX + domain, "TXT")
    cname = lookup(domain, "CNAME")
    actual = lookup(domain, "A") | lookup(domain, "AAAA")
    expected = set(allowed_ips) or ((lookup(target, "A") | lookup(target, "AAAA")) if target else set())
    ownership = any(hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8")) for value in txt)
    routing = bool(target) and target in cname
    proxied = False
    if not routing:
        routing = bool(actual) and bool(expected) and actual <= expected
        if not routing and allow_proxied and all_cloudflare(actual):
            routing = proxied = True
    return {"txt": sorted(txt), "cname": sorted(cname), "addresses": sorted(actual),
            "expected_addresses": sorted(expected), "ownership": ownership, "routing": routing,
            "proxied": proxied, "dns_error": failed and not (ownership and routing)}
