"""Subdomain validation for the hosting platform.

Regular accounts can only create instances on the flat hosting suffix
(``{slug}-hosting.example.com``).  Admin accounts may additionally
claim *apex* subdomains (``{slug}.example.com``) such as
``wiki.example.com``.  The shared :func:`validate_subdomain` function
covers both code paths.
"""

import re

from . import config

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _reserved_for_mode(domain_mode):
    """Return the set of subdomains reserved for *domain_mode*.

    The reserved set is identical for both ``apex`` and ``hosting``
    modes: it only blocks labels that would collide with the
    platform's own DNS / mail / portal records (``www``, ``mail``,
    ``portal``, the configured portal prefix, ``INSTANCE_URL_SUFFIX``
    …).  Generic content words such as ``wiki`` or ``blog`` are *not*
    blocked: an admin claiming ``wiki.{BASE_DOMAIN}`` (apex) does not
    prevent a regular user from claiming ``wiki-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``
    (hosting), because those are distinct public hosts.
    """
    if domain_mode not in ("apex", "hosting"):
        raise ValueError(f"unknown domain_mode {domain_mode!r}")
    return set(config.RESERVED_SUBDOMAINS)


def validate_subdomain(subdomain, *, account_is_admin=False, domain_mode="hosting"):
    """Validate a subdomain string.

    Parameters
    ----------
    subdomain:
        Raw subdomain provided by the user.
    account_is_admin:
        ``True`` when the requesting account is an admin.  Required for
        ``domain_mode="apex"``.
    domain_mode:
        ``"hosting"`` (default) or ``"apex"``.  In hosting mode the
        instance is served at ``{slug}-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``
        and the full :data:`config.RESERVED_SUBDOMAINS` set applies.  In
        apex mode the instance is served at ``{slug}.{BASE_DOMAIN}`` and
        only admins may claim it; a smaller reserved set is used.

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    """
    if not subdomain:
        return False, "Subdomain is required."

    subdomain = subdomain.lower().strip()

    if domain_mode not in ("hosting", "apex"):
        return False, "Invalid domain mode."

    if domain_mode == "apex" and not account_is_admin:
        return False, (
            "Apex subdomains (e.g. wiki.example.com) are reserved for "
            "admin accounts."
        )

    if len(subdomain) < config.SUBDOMAIN_MIN_LENGTH:
        return False, f"Subdomain must be at least {config.SUBDOMAIN_MIN_LENGTH} characters."

    if len(subdomain) > config.SUBDOMAIN_MAX_LENGTH:
        return False, f"Subdomain cannot exceed {config.SUBDOMAIN_MAX_LENGTH} characters."

    if not _SUBDOMAIN_RE.match(subdomain):
        return False, "Subdomain may only contain lowercase letters, digits, and hyphens, and must start and end with a letter or digit."

    if subdomain in _reserved_for_mode(domain_mode):
        return False, f"'{subdomain}' is a reserved name and cannot be used."

    return True, ""
