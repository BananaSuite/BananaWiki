"""Custom domains: permission, claims, verification and routing decisions.

An administrator allows custom domains per wiki; the owner claims a hostname,
publishes a TXT proof at ``_bananawiki-challenge.<domain>`` and a CNAME (or
A/AAAA records) to ``HOSTING_CUSTOM_DOMAIN_TARGET``, then verifies. The DNS
lookups themselves are done by the runtime (:meth:`Runtime.check_domain`).
A verification is valid for 24 hours and renewed by the maintenance loop;
an expired proof stops routing. Unverified claims expire after an hour so a
claim cannot reserve a name.

A proof that lapsed more than ``LAPSED_DAYS`` ago (the DNS moved away, the
domain changed hands) does not hold the domain against another wiki: that
wiki may claim it (``instance_domain_claims``, with a token of its own) and
take it over once it proves the domain with that token. Until then the old
binding stays, so its owner keeps it if their own proof comes back first
(the maintenance loop renews it), and nobody can release a domain without
proving it.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from typing import Any

from flask import current_app

from ..core.timeutil import now_sql, sql_in
from . import certwarm, events, urls
from .db import db
from .errors import ServiceError
from .runtime import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.domains")

VERIFICATION_HOURS = 24
LAPSED_DAYS = 7
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def configured() -> bool:
    cfg = current_app.config["HOSTING"]
    return cfg.hosting_mode == "subdomain" and bool(cfg.custom_domain_target)


def normalize(value: str) -> str:
    value = str(value or "").strip().lower().rstrip(".")
    if not value or any(c in value for c in "/:@?#\\*\x00 "):
        raise ServiceError("hosting.domains.invalid")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ServiceError("hosting.domains.invalid") from error
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels):
        raise ServiceError("hosting.domains.invalid")
    if labels[-1].isdigit() or labels[-1] in {"localhost", "local", "internal", "onion"}:
        raise ServiceError("hosting.domains.not_public")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ServiceError("hosting.domains.not_public")


def _external(value: str) -> str:
    domain = normalize(value)
    cfg = current_app.config["HOSTING"]
    for reserved in (cfg.base_domain, cfg.effective_portal_domain, cfg.custom_domain_target):
        if reserved and (domain == reserved or domain.endswith("." + reserved)):
            raise ServiceError("hosting.domains.platform_domain")
    return domain


def binding(instance_id: str) -> dict[str, Any] | None:
    """The wiki's custom domain, or its claim of a lapsed one (``takeover``: shown like an unverified domain)."""
    row = db.one("SELECT * FROM instance_custom_domains WHERE instance_id = ?", (instance_id,))
    if row is not None:
        return row
    claim = db.one("SELECT * FROM instance_domain_claims WHERE instance_id = ?", (instance_id,))
    if claim is None:
        return None
    return {**claim, "verified_at": None, "verified_until": None, "takeover": True}


def _lapsed(row: dict[str, Any]) -> bool:
    """A verified binding whose proof ran out more than ``LAPSED_DAYS`` ago."""
    return bool(row["verified_at"]) and (not row["verified_until"]
                                         or row["verified_until"] < sql_in(days=-LAPSED_DAYS))


def _forget(instance_id: str) -> None:
    """Drop the wiki's domain and its claim of a lapsed one."""
    db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (instance_id,))
    db.execute("DELETE FROM instance_domain_claims WHERE instance_id = ?", (instance_id,))


def set_permission(inst: dict[str, Any], allowed: bool, actor_id: str) -> None:
    with db.transaction():
        db.update("instances", {"custom_domain_allowed": 1 if allowed else 0}, "id = ?", (inst["id"],))
        if not allowed:
            _forget(inst["id"])
        events.record("instance", inst["id"], "domain.permission." + ("granted" if allowed else "revoked"), actor_id)


def claim(inst: dict[str, Any], value: str, actor_id: str) -> dict[str, Any]:
    if not configured():
        raise ServiceError("hosting.domains.not_configured")
    domain = _external(value)
    with db.transaction():
        current = db.one("SELECT * FROM instances WHERE id = ?", (inst["id"],))
        if not current or not current["custom_domain_allowed"]:
            raise ServiceError("hosting.domains.not_allowed")
        if current["status"] != "running":
            raise ServiceError("hosting.domains.start_first")
        db.execute("DELETE FROM instance_custom_domains WHERE verified_at IS NULL AND created_at < ?",
                   (sql_in(hours=-1),))
        db.execute("DELETE FROM instance_domain_claims WHERE created_at < ?", (sql_in(days=-LAPSED_DAYS),))
        existing = db.one("SELECT * FROM instance_custom_domains WHERE domain = ?", (domain,))
        if existing and existing["instance_id"] == inst["id"]:
            return existing
        if existing and not _lapsed(existing):
            raise ServiceError("hosting.domains.taken")
        mine = binding(inst["id"])
        if existing and mine and mine["domain"] == domain:
            return mine  # this wiki's claim of the lapsed domain, token unchanged
        _forget(inst["id"])
        record = {"domain": domain, "instance_id": inst["id"], "verification_token": secrets.token_hex(32),
                  "created_at": now_sql()}
        # Another wiki's proof lapsed long ago: claim it; that binding goes only once this wiki proves the domain.
        db.insert("instance_domain_claims" if existing else "instance_custom_domains", record)
        events.record("instance", inst["id"], "domain.claimed", actor_id, domain)
    return binding(inst["id"])  # type: ignore[return-value]


def remove(inst: dict[str, Any], actor_id: str) -> None:
    _forget(inst["id"])
    events.record("instance", inst["id"], "domain.removed", actor_id)


def verify(inst: dict[str, Any], actor_id: str | None) -> dict[str, Any]:
    from . import instances

    if not configured():
        raise ServiceError("hosting.domains.not_configured")
    record = binding(inst["id"])
    if record is None:
        raise ServiceError("hosting.domains.claim_first")
    try:
        check = instances.runtime().check_domain(record["domain"], record["verification_token"])
    except RuntimeFailure as error:
        raise ServiceError(f"hosting.runtime.{error.code}") from error
    table = "instance_domain_claims" if record.get("takeover") else "instance_custom_domains"
    db.update(table, {"last_checked_at": now_sql()}, "instance_id = ?", (inst["id"],))
    if check.dns_error:
        raise ServiceError("hosting.domains.dns_pending")
    if not check.ownership:
        raise ServiceError("hosting.domains.txt_mismatch")
    if not check.routing:
        raise ServiceError("hosting.domains.wrong_target")
    if record.get("takeover"):
        _take_over(inst, record, actor_id)
        instances.sync_routes()
        certwarm.schedule(record["domain"])
        return {**binding(inst["id"]), "proxied": check.proxied}  # type: ignore[dict-item]
    with db.transaction():
        changed = db.execute(
            "UPDATE instance_custom_domains SET verified_at = COALESCE(verified_at, ?), verified_until = ? "
            "WHERE instance_id = ? AND domain = ? AND verification_token = ? AND EXISTS "
            "(SELECT 1 FROM instances WHERE id = ? AND custom_domain_allowed = 1)",
            (now_sql(), sql_in(hours=VERIFICATION_HOURS), inst["id"], record["domain"],
             record["verification_token"], inst["id"]),
        ).rowcount
        if not changed:
            raise ServiceError("hosting.domains.changed")
        if not record["verified_at"]:
            events.record("instance", inst["id"], "domain.verified", actor_id, record["domain"])
    instances.sync_routes()
    if not record["verified_at"]:
        certwarm.schedule(record["domain"])
    # ``proxied``: accepted because the record is proxied by Cloudflare (not stored).
    return {**binding(inst["id"]), "proxied": check.proxied}  # type: ignore[dict-item]


def _take_over(inst: dict[str, Any], claim: dict[str, Any], actor_id: str | None) -> None:
    """The claimant proved a lapsed domain: release the old binding and bind the domain to its wiki."""
    domain = claim["domain"]
    try:
        with db.transaction():
            current = db.one("SELECT 1 FROM instance_domain_claims WHERE instance_id = ? AND domain = ? "
                             "AND verification_token = ?", (inst["id"], domain, claim["verification_token"]))
            if current is None or not db.scalar("SELECT 1 FROM instances WHERE id = ? AND custom_domain_allowed = 1",
                                                (inst["id"],)):
                raise ServiceError("hosting.domains.changed")
            holder = db.one("SELECT * FROM instance_custom_domains WHERE domain = ?", (domain,))
            if holder is not None and holder["instance_id"] != inst["id"]:
                if not _lapsed(holder):  # its owner proved it again meanwhile, or another wiki holds it now
                    raise ServiceError("hosting.domains.taken")
                db.execute("DELETE FROM instance_custom_domains WHERE domain = ?", (domain,))
                events.record("instance", holder["instance_id"], "domain.lapsed", None, domain)
            _forget(inst["id"])
            db.execute("DELETE FROM instance_domain_claims WHERE domain = ?", (domain,))  # other claims are moot
            db.insert("instance_custom_domains", {
                "domain": domain, "instance_id": inst["id"], "verification_token": claim["verification_token"],
                "created_at": claim["created_at"], "verified_at": now_sql(),
                "verified_until": sql_in(hours=VERIFICATION_HOURS), "last_checked_at": now_sql(),
            })
            events.record("instance", inst["id"], "domain.verified", actor_id, domain)
    except ServiceError as error:
        if error.key == "hosting.domains.taken":
            db.execute("DELETE FROM instance_domain_claims WHERE instance_id = ?", (inst["id"],))
        raise


def refresh(limit: int = 10) -> int:
    """Re-verify proofs that expire within 12 hours (maintenance)."""
    from . import instances

    if not configured():
        return 0
    renewed = 0
    rows = db.all("SELECT instance_id FROM instance_custom_domains WHERE verified_at IS NOT NULL AND verified_until < ? "
                  "ORDER BY COALESCE(last_checked_at, created_at) LIMIT ?", (sql_in(hours=12), limit))
    for row in rows:
        inst = instances.get(row["instance_id"])
        if inst is None:
            continue
        try:
            verify(inst, None)
            renewed += 1
        except ServiceError as error:
            log.info("Custom domain of %s not renewed: %s", inst["id"], error.key)
    return renewed


# Routing decisions


def _serving_row(where: str, params: tuple) -> dict[str, Any] | None:
    return db.one(
        "SELECT i.*, a.suspended AS account_suspended, a.deleted_at AS account_deleted, a.approval_status, "
        "a.pending_deletion FROM instances i JOIN accounts a ON a.id = i.account_id WHERE " + where, params,
    )


def _can_serve(row: dict[str, Any] | None) -> bool:
    if not row or row["status"] not in ("running", "stopped") or row["account_suspended"] or row["account_deleted"]:
        return False
    if row["approval_status"] != "approved" or row["pending_deletion"]:
        return False
    return not (row["expires_at"] and row["expires_at"] <= now_sql())


def may_serve(instance_id: str) -> bool:
    """Whether traffic for this wiki may be routed (and certificates issued)."""
    return _can_serve(_serving_row("i.id = ?", (instance_id,)))


def resolve(host: str) -> dict[str, Any] | None:
    """The wiki a verified custom domain routes to, if it may serve."""
    if not configured():
        return None
    try:
        domain = normalize(host)
    except ServiceError:
        return None
    row = _serving_row("i.id = (SELECT instance_id FROM instance_custom_domains WHERE domain = ? AND verified_until > ?) "
                       "AND i.custom_domain_allowed = 1", (domain, now_sql()))
    return row if _can_serve(row) else None


def platform_slug(host: str) -> tuple[str, str] | None:
    """``(slug, domain_mode)`` for a platform wiki host, or None.

    A label that reads as a hosting address (any label without a suffix, or
    ``team-hosting``) stays the address of the live apex wiki of that name.
    """
    cfg = current_app.config["HOSTING"]
    base = cfg.base_domain
    host = host.lower().rstrip(".")
    if not base or not host.endswith("." + base):
        return None
    label = host[: -len(base) - 1]
    if not label or "." in label:
        return None
    suffix = urls.instance_suffix()
    if suffix and not label.endswith("-" + suffix):
        return label, "apex"
    if db.scalar("SELECT 1 FROM instances WHERE subdomain = ? COLLATE NOCASE AND domain_mode = 'apex' "
                 "AND status != 'terminated'", (label,)):
        return label, "apex"
    return (label[: -len(suffix) - 1] if suffix else label), "hosting"


def certificate_allowed(host: str) -> bool:
    """On-demand TLS: issue certificates only for hosts that route to a serving wiki."""
    try:
        domain = normalize(host)
    except ServiceError:
        return False
    if resolve(domain):
        return True
    base = current_app.config["HOSTING"].base_domain
    if base and domain == "www." + base:
        return True  # the managed Caddyfile redirects it to the base domain
    found = platform_slug(domain)
    if not found:
        return False
    slug, mode = found
    return _can_serve(_serving_row("i.subdomain = ? COLLATE NOCASE AND i.domain_mode = ?", (slug, mode)))
