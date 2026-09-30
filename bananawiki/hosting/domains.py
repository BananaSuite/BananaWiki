"""Custom domains: permission, claims, verification and routing decisions.

An administrator allows custom domains per wiki; the owner claims a hostname,
publishes a TXT proof at ``_bananawiki-challenge.<domain>`` and a CNAME (or
A/AAAA records) to ``HOSTING_CUSTOM_DOMAIN_TARGET``, then verifies. The DNS
lookups themselves are done by the runtime (:meth:`Runtime.check_domain`).
A verification is valid for 24 hours and renewed by the maintenance loop;
an expired proof stops routing. Unverified claims expire after an hour so a
claim cannot reserve a name.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from typing import Any

from flask import current_app

from ..core.timeutil import now_sql, sql_in
from . import events, urls
from .db import db
from .errors import ServiceError
from .runtime import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.domains")

VERIFICATION_HOURS = 24
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
    return db.one("SELECT * FROM instance_custom_domains WHERE instance_id = ?", (instance_id,))


def set_permission(inst: dict[str, Any], allowed: bool, actor_id: str) -> None:
    with db.transaction():
        db.update("instances", {"custom_domain_allowed": 1 if allowed else 0}, "id = ?", (inst["id"],))
        if not allowed:
            db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (inst["id"],))
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
        existing = db.one("SELECT * FROM instance_custom_domains WHERE domain = ?", (domain,))
        if existing and existing["instance_id"] != inst["id"]:
            raise ServiceError("hosting.domains.taken")
        if existing:
            return existing
        db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (inst["id"],))
        db.insert("instance_custom_domains", {"domain": domain, "instance_id": inst["id"],
                                              "verification_token": secrets.token_hex(32), "created_at": now_sql()})
        events.record("instance", inst["id"], "domain.claimed", actor_id, domain)
    return binding(inst["id"])  # type: ignore[return-value]


def remove(inst: dict[str, Any], actor_id: str) -> None:
    db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (inst["id"],))
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
    db.update("instance_custom_domains", {"last_checked_at": now_sql()}, "instance_id = ?", (inst["id"],))
    if check.dns_error:
        raise ServiceError("hosting.domains.dns_pending")
    if not check.ownership:
        raise ServiceError("hosting.domains.txt_mismatch")
    if not check.routing:
        raise ServiceError("hosting.domains.wrong_target")
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
    # ``proxied``: accepted because the record is proxied by Cloudflare (not stored).
    return {**binding(inst["id"]), "proxied": check.proxied}  # type: ignore[dict-item]


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


# ── Routing decisions ─────────────────────────────────────────────────────────


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
    """``(slug, domain_mode)`` for a platform wiki host, or None."""
    cfg = current_app.config["HOSTING"]
    base = cfg.base_domain
    host = host.lower().rstrip(".")
    if not base or not host.endswith("." + base):
        return None
    label = host[: -len(base) - 1]
    if not label or "." in label:
        return None
    suffix = urls.instance_suffix()
    if suffix and label.endswith("-" + suffix):
        return label[: -len(suffix) - 1], "hosting"
    return label, "apex" if suffix else "hosting"


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
