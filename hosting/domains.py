"""Custom domain ownership, permission, and routing checks."""

import hmac
import ipaddress
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import dns.exception
import dns.resolver

from . import config
from .db._connection import get_hosting_db_context
from .db._events import record_event

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
VERIFICATION_HOURS = 24


def normalize_domain(value):
    value = str(value or "").strip().lower().rstrip(".")
    if not value or any(c in value for c in "/:@?#\\*\x00"):
        raise ValueError("Enter a hostname, such as wiki.example.org, without a URL or port.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain name.") from exc
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels):
        raise ValueError("Invalid domain name.")
    if labels[-1].isdigit() or labels[-1] in {"localhost", "local", "internal", "onion"}:
        raise ValueError("Use a public DNS hostname.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("IP addresses cannot be used as custom domains.")


def _external_domain(value):
    domain = normalize_domain(value)
    for reserved in (config.BASE_DOMAIN, config.EFFECTIVE_PORTAL_DOMAIN, config.HOSTING_CUSTOM_DOMAIN_TARGET):
        reserved = (reserved or "").lower().rstrip(".")
        if reserved and (domain == reserved or domain.endswith("." + reserved)):
            raise ValueError("The hosting platform's own domains cannot be claimed as custom domains.")
    return domain


def _now():
    return datetime.now(timezone.utc)


def get_domain(instance_id):
    with get_hosting_db_context() as conn:
        row = conn.execute("SELECT * FROM instance_custom_domains WHERE instance_id=?", (instance_id,)).fetchone()
    return dict(row) if row else None


def set_permission(instance_id, allowed, actor_id):
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM instances WHERE id=?", (instance_id,)).fetchone():
            raise ValueError("Instance not found.")
        conn.execute("UPDATE instances SET custom_domain_allowed=? WHERE id=?", (int(bool(allowed)), instance_id))
        if not allowed:
            conn.execute("DELETE FROM instance_custom_domains WHERE instance_id=?", (instance_id,))
        record_event(conn, "instance", instance_id, "domain.permission.granted" if allowed else "domain.permission.revoked", actor_id)
        conn.commit()


def claim_domain(instance_id, value, actor_id):
    if config.HOSTING_MODE != "subdomain" or not config.HOSTING_CUSTOM_DOMAIN_TARGET:
        raise ValueError("Custom domains are not configured by this hosting service.")
    domain = _external_domain(value)
    now = _now()
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        instance = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if not instance or not instance["custom_domain_allowed"]:
            raise ValueError("A platform administrator must enable custom domains for this instance.")
        if instance["status"] != "running":
            raise ValueError("Start the instance before adding a custom domain.")
        # Pending claims expire so an unverified claim cannot reserve a name indefinitely.
        conn.execute(
            "DELETE FROM instance_custom_domains WHERE verified_at IS NULL AND created_at<?",
            ((now - timedelta(hours=1)).isoformat(),),
        )
        existing = conn.execute("SELECT * FROM instance_custom_domains WHERE domain=?", (domain,)).fetchone()
        if existing and existing["instance_id"] != instance_id:
            raise ValueError("This domain is already assigned to another instance.")
        if existing:
            return dict(existing)
        conn.execute("DELETE FROM instance_custom_domains WHERE instance_id=?", (instance_id,))
        conn.execute(
            "INSERT INTO instance_custom_domains (domain, instance_id, verification_token, created_at) VALUES (?, ?, ?, ?)",
            (domain, instance_id, secrets.token_hex(32), now.isoformat()),
        )
        record_event(conn, "instance", instance_id, "domain.claimed", actor_id, domain)
        conn.commit()
    return get_domain(instance_id)


def remove_domain(instance_id, actor_id):
    with get_hosting_db_context() as conn:
        conn.execute("DELETE FROM instance_custom_domains WHERE instance_id=?", (instance_id,))
        record_event(conn, "instance", instance_id, "domain.removed", actor_id)
        conn.commit()


def _records(name, record_type):
    answer = dns.resolver.resolve(name, record_type, search=False, lifetime=4)
    if record_type == "TXT":
        return {b"".join(row.strings).decode("utf-8", "replace") for row in answer}
    return {row.to_text().rstrip(".").lower() for row in answer}


def _addresses(name):
    result = set()
    for record_type in ("A", "AAAA"):
        try:
            result.update(_records(name, record_type))
        except dns.resolver.NoAnswer:
            pass
    return result


def verify_domain(instance_id, actor_id=None):
    if config.HOSTING_MODE != "subdomain" or not config.HOSTING_CUSTOM_DOMAIN_TARGET:
        raise ValueError("Custom domains are not configured by this hosting service.")
    binding = get_domain(instance_id)
    if not binding:
        raise ValueError("Add a domain first.")
    domain = binding["domain"]
    try:
        proofs = _records("_bananawiki-challenge." + domain, "TXT")
        if not any(hmac.compare_digest(value.encode("utf-8"), binding["verification_token"].encode("ascii")) for value in proofs):
            raise ValueError("The ownership TXT record does not match. Check its name and value.")
        target = config.HOSTING_CUSTOM_DOMAIN_TARGET.lower().rstrip(".")
        try:
            correct_route = target in _records(domain, "CNAME")
        except dns.resolver.NoAnswer:
            correct_route = False
        if not correct_route:
            expected = set(config.HOSTING_CUSTOM_DOMAIN_IPS) or _addresses(target)
            actual = _addresses(domain)
            correct_route = bool(actual) and bool(expected) and actual.issubset(expected)
        if not correct_route:
            raise ValueError("The domain does not point to this hosting service. Check its CNAME or A/AAAA records.")
    except dns.exception.DNSException as exc:
        raise ValueError("DNS records are not available yet. Check the records and try again after they propagate.") from exc
    now = _now()
    with get_hosting_db_context() as conn:
        cursor = conn.execute(
            "UPDATE instance_custom_domains SET verified_at=?, verified_until=?, last_checked_at=? "
            "WHERE instance_id=? AND domain=? AND verification_token=? "
            "AND EXISTS (SELECT 1 FROM instances WHERE id=? AND custom_domain_allowed=1)",
            (now.isoformat(), (now + timedelta(hours=VERIFICATION_HOURS)).isoformat(), now.isoformat(), instance_id,
             domain, binding["verification_token"], instance_id),
        )
        if not cursor.rowcount:
            raise ValueError("The domain or its permission changed. Reload this page before verifying again.")
        if not binding["verified_at"]:
            record_event(conn, "instance", instance_id, "domain.verified", actor_id, domain)
        conn.commit()
    return get_domain(instance_id)


def _instance_can_serve(row):
    if row["status"] not in {"running", "stopped"} or row["account_suspended"] or row["deleted_at"]:
        return False
    if row["approval_status"] != "approved" or row["pending_deletion"]:
        return False
    if row["expires_at"]:
        try:
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= _now():
                return False
        except (ValueError, TypeError):
            return False
    return True


def resolve_domain(value):
    if config.HOSTING_MODE != "subdomain" or not config.HOSTING_CUSTOM_DOMAIN_TARGET:
        return None
    try:
        domain = normalize_domain(value)
    except ValueError:
        return None
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT i.*, a.suspended AS account_suspended, a.deleted_at, a.approval_status, a.pending_deletion "
            "FROM instance_custom_domains d JOIN instances i ON i.id=d.instance_id "
            "JOIN accounts a ON a.id=i.account_id "
            "WHERE d.domain=? AND d.verified_until>? AND i.custom_domain_allowed=1",
            (domain, _now().isoformat()),
        ).fetchone()
    return dict(row) if row and _instance_can_serve(row) else None


def certificate_allowed(value):
    try:
        domain = normalize_domain(value)
    except ValueError:
        return False
    if resolve_domain(domain):
        return True
    from ._subdomain_proxy import _extract_subdomain
    slug, mode = _extract_subdomain(domain)
    if not slug:
        return False
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT i.*, a.suspended AS account_suspended, a.deleted_at, a.approval_status, a.pending_deletion "
            "FROM instances i JOIN accounts a ON a.id=i.account_id WHERE i.subdomain=? AND i.domain_mode=?",
            (slug, mode),
        ).fetchone()
    return bool(row and _instance_can_serve(row))


def instance_can_serve(instance_id):
    with get_hosting_db_context() as conn:
        row = conn.execute(
            "SELECT i.*, a.suspended AS account_suspended, a.deleted_at, a.approval_status, a.pending_deletion "
            "FROM instances i JOIN accounts a ON a.id=i.account_id WHERE i.id=?", (instance_id,),
        ).fetchone()
    return bool(row and _instance_can_serve(row))


def refresh_domains(limit=10):
    """Renew bounded batches of proofs; expired proofs automatically stop routing."""
    if config.HOSTING_MODE != "subdomain" or not config.HOSTING_CUSTOM_DOMAIN_TARGET:
        return
    deadline = (_now() + timedelta(hours=12)).isoformat()
    with get_hosting_db_context() as conn:
        rows = conn.execute(
            "SELECT instance_id FROM instance_custom_domains WHERE verified_at IS NOT NULL "
            "AND verified_until<? ORDER BY COALESCE(last_checked_at, created_at) LIMIT ?", (deadline, limit),
        ).fetchall()

    def refresh(row):
        try:
            verify_domain(row["instance_id"])
            return 1
        except (ValueError, sqlite3.Error):
            with get_hosting_db_context() as conn:
                conn.execute(
                    "UPDATE instance_custom_domains SET last_checked_at=? WHERE instance_id=?",
                    (_now().isoformat(), row["instance_id"]),
                )
                conn.commit()
            return 0

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        return sum(executor.map(refresh, rows))
