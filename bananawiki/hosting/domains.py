"""Custom domains: permission, claims, verification and routing decisions.

Who may use one (:func:`permission`): administrators' wikis always; then, as
the platform setting ``custom_domains_policy`` says, nobody else (``admins``,
the default), the accounts an administrator allows (``selected``,
``accounts.custom_domains_allowed``) or everyone (``everyone``). An
administrator can also allow one wiki (``instances.custom_domain_allowed``),
which is what approving an owner's request does. Routing and certificates
follow the same rule in SQL (:func:`allowed_sql`), so a wiki that loses the
permission stops answering on its domain at once; its claim stays, and a new
proof brings it back once it is allowed again.

The owner claims a hostname,
publishes a TXT proof at ``_bananawiki-challenge.<domain>`` and a CNAME (or
A/AAAA records) to ``HOSTING_CUSTOM_DOMAIN_TARGET``, then verifies. The DNS
lookups themselves are done by the runtime (:meth:`Runtime.check_domain`).
A verification is valid for 24 hours and renewed by the maintenance loop;
an expired proof stops routing. Unverified claims expire after an hour, and
another wiki may claim a name that is claimed but not proven (whoever proves
it first gets it), so a claim cannot reserve a name.

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
import time
from typing import Any

from flask import current_app

from ..core.timeutil import now_sql, sql_in
from . import certwarm, events, settings, urls
from .db import db
from .errors import ServiceError
from .runtime import RuntimeFailure

log = logging.getLogger("bananawiki.hosting.domains")

VERIFICATION_HOURS = 24
LAPSED_DAYS = 7
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Endings used for private networks and tests, never a public domain: refused as a custom domain, so the
# portal's resolver is never asked about a network's own names on a customer's behalf.
PRIVATE_SUFFIXES = frozenset({
    "localhost", "local", "internal", "onion", "lan", "home", "corp", "intranet", "private", "localdomain",
    "test", "invalid", "example", "consul", "svc", "arpa",
})


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
    # A customer never claims a private network's name (the portal would look it up for them), unless the
    # platform itself lives under that ending (an intranet platform at wiki.lan serves lan names).
    ending = domain.rsplit(".", 1)[-1]
    own = (cfg.base_domain or "").rsplit(".", 1)[-1]
    if ending in PRIVATE_SUFFIXES and ending != own:
        raise ServiceError("hosting.domains.not_public")
    return domain


# Who may use custom domains

# Why a wiki may use a custom domain, first match: its owner is an administrator, everyone may, its owner's
# account may (policy "selected"), or an administrator allowed this wiki (or approved its request).
REASONS = ("admin", "everyone", "account", "wiki")


def allowed_sql(instance: str = "i", account: str = "a") -> str:
    """SQL condition: the wiki aliased *instance*, owned by the account aliased *account*, may use a custom domain."""
    policy = "(SELECT custom_domains_policy FROM hosting_settings WHERE id = 1)"
    return (f"({instance}.custom_domain_allowed = 1 OR {account}.is_admin = 1 OR {policy} = 'everyone' "
            f"OR ({policy} = 'selected' AND {account}.custom_domains_allowed = 1))")


def permission(inst: dict[str, Any], owner: dict[str, Any] | None = None) -> str | None:
    """Why *inst* may use a custom domain (one of :data:`REASONS`), or None when it may not."""
    if owner is None or "custom_domains_allowed" not in owner:
        owner = db.one("SELECT is_admin, custom_domains_allowed FROM accounts WHERE id = ?", (inst["account_id"],))
    if owner and owner.get("is_admin"):
        return "admin"
    policy = settings.custom_domains_policy()
    if policy == "everyone":
        return "everyone"
    if policy == "selected" and owner and owner.get("custom_domains_allowed"):
        return "account"
    if inst.get("custom_domain_allowed"):
        return "wiki"
    return None


def allowed(inst: dict[str, Any], owner: dict[str, Any] | None = None) -> bool:
    return permission(inst, owner) is not None


def set_account_permission(account: dict[str, Any], allowed_: bool, actor_id: str) -> bool:
    """Let an account use custom domains while the policy is ``selected`` (or stop it); returns whether it changed."""
    changed = db.execute("UPDATE accounts SET custom_domains_allowed = ? WHERE id = ? AND custom_domains_allowed != ?",
                         (1 if allowed_ else 0, account["id"], 1 if allowed_ else 0)).rowcount
    if changed:
        events.record("account", account["id"], "domain.account." + ("granted" if allowed_ else "revoked"), actor_id)
    return bool(changed)


def policy_counts() -> dict[str, int]:
    """For the settings page: wikis answering on a custom domain, accounts allowed one by one."""
    return {
        "serving": db.scalar(
            "SELECT COUNT(*) FROM instance_custom_domains d JOIN instances i ON i.id = d.instance_id "
            "JOIN accounts a ON a.id = i.account_id WHERE d.verified_at IS NOT NULL AND d.verified_until > ? AND "
            + allowed_sql(), (now_sql(),), default=0),
        "claimed": db.scalar("SELECT COUNT(*) FROM instance_custom_domains", default=0),
        "accounts": db.scalar("SELECT COUNT(*) FROM accounts WHERE custom_domains_allowed = 1 AND deleted_at IS NULL",
                              default=0),
        "wikis": db.scalar("SELECT COUNT(*) FROM instances WHERE custom_domain_allowed = 1 AND status != 'terminated'",
                           default=0),
    }


def active_domain(instance_id: str) -> str | None:
    """The custom domain the wiki answers on now: proven, not expired, and the wiki may use one."""
    return db.scalar(
        "SELECT d.domain FROM instance_custom_domains d JOIN instances i ON i.id = d.instance_id "
        "JOIN accounts a ON a.id = i.account_id WHERE d.instance_id = ? AND d.verified_at IS NOT NULL "
        "AND d.verified_until > ? AND " + allowed_sql(), (instance_id, now_sql()))


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


def _releasable(row: dict[str, Any]) -> bool:
    """A binding another wiki may claim and take over by proving the domain first.

    A lapsed proof, or a claim never proven: otherwise a wiki could keep the
    real owner of a domain out by claiming it again and again, each claim
    younger than the hour after which unproven claims are dropped.
    """
    return not row["verified_at"] or _lapsed(row)


def _forget(instance_id: str) -> None:
    """Drop the wiki's domain and its claim of a lapsed one."""
    db.execute("DELETE FROM instance_custom_domains WHERE instance_id = ?", (instance_id,))
    db.execute("DELETE FROM instance_domain_claims WHERE instance_id = ?", (instance_id,))


def set_permission(inst: dict[str, Any], allowed_: bool, actor_id: str) -> None:
    """An administrator allows this one wiki a custom domain, or takes that away.

    Taking it away removes the wiki's domain when nothing else allows it any
    more (its owner is an administrator, the policy allows everyone or its
    owner's account); otherwise the domain stays, as the wiki may still use it.
    """
    with db.transaction():
        db.update("instances", {"custom_domain_allowed": 1 if allowed_ else 0}, "id = ?", (inst["id"],))
        if not allowed_ and not allowed({**inst, "custom_domain_allowed": 0}):
            _forget(inst["id"])
        events.record("instance", inst["id"], "domain.permission." + ("granted" if allowed_ else "revoked"), actor_id)


def claim(inst: dict[str, Any], value: str, actor_id: str) -> dict[str, Any]:
    if not configured():
        raise ServiceError("hosting.domains.not_configured")
    domain = _external(value)
    with db.transaction():
        current = db.one("SELECT * FROM instances WHERE id = ?", (inst["id"],))
        if not current or not allowed(current):
            raise ServiceError("hosting.domains.not_allowed")
        if current["status"] != "running":
            raise ServiceError("hosting.domains.start_first")
        db.execute("DELETE FROM instance_custom_domains WHERE verified_at IS NULL AND created_at < ?",
                   (sql_in(hours=-1),))
        db.execute("DELETE FROM instance_domain_claims WHERE created_at < ?", (sql_in(days=-LAPSED_DAYS),))
        existing = db.one("SELECT * FROM instance_custom_domains WHERE domain = ?", (domain,))
        if existing and existing["instance_id"] == inst["id"]:
            return existing
        if existing and not _releasable(existing):
            raise ServiceError("hosting.domains.taken")
        mine = binding(inst["id"])
        if existing and mine and mine["domain"] == domain:
            return mine  # this wiki's claim of the lapsed domain, token unchanged
        _forget(inst["id"])
        record = {"domain": domain, "instance_id": inst["id"], "verification_token": secrets.token_hex(32),
                  "created_at": now_sql()}
        # Another wiki never proved the domain, or its proof lapsed long ago: claim it; that binding goes
        # only once this wiki proves the domain.
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
            "(SELECT 1 FROM instances i JOIN accounts a ON a.id = i.account_id WHERE i.id = ? AND "
            + allowed_sql() + ")",
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
            if current is None or not db.scalar("SELECT 1 FROM instances i JOIN accounts a ON a.id = i.account_id "
                                                "WHERE i.id = ? AND " + allowed_sql(), (inst["id"],)):
                raise ServiceError("hosting.domains.changed")
            holder = db.one("SELECT * FROM instance_custom_domains WHERE domain = ?", (domain,))
            if holder is not None and holder["instance_id"] != inst["id"]:
                if not _releasable(holder):  # its owner proved it (again) meanwhile, or another wiki holds it now
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
                       "AND " + allowed_sql(), (domain, now_sql()))
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


# Guiding owners through their DNS

ADDRESS_TTL_SECONDS = 600  # how long the platform's looked-up addresses are kept
ADDRESS_RETRY_SECONDS = 60  # after a lookup that found nothing
_addresses_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
# Second-level labels under which registries sell names (example.co.uk): the zone is one label longer.
_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gob", "go", "gov", "gv", "ltd", "me", "mil", "ne", "net", "nhs",
                           "nic", "or", "org", "plc", "sch"})


def platform_addresses() -> tuple[str, ...]:
    """The addresses A/AAAA records of a custom domain may point at.

    ``HOSTING_CUSTOM_DOMAIN_IPS`` when it is set; otherwise the addresses of
    ``HOSTING_CUSTOM_DOMAIN_TARGET`` itself, which the routing check accepts
    as well (``dnscheck.check``). Looked up through the runtime and kept for
    ten minutes (a minute after a lookup that found nothing).
    """
    cfg = current_app.config["HOSTING"]
    if cfg.custom_domain_ips:
        return tuple(cfg.custom_domain_ips)
    target = (cfg.custom_domain_target or "").lower().rstrip(".")
    if not target:
        return ()
    cached = _addresses_cache.get(target)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    from . import instances

    lookup = getattr(instances.runtime(), "lookup_addresses", None)
    try:
        found = tuple(lookup(target)) if lookup else ()
    except (RuntimeFailure, OSError, ValueError) as error:
        log.info("Addresses of %s not looked up: %s", target, error)
        found = ()
    _addresses_cache[target] = (now + (ADDRESS_TTL_SECONDS if found else ADDRESS_RETRY_SECONDS), found)
    return found


def zone_of(domain: str) -> str:
    """The domain's zone as registrars sell it (``example.org``, ``example.co.uk``), guessed from its labels."""
    labels = domain.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def guide(record: dict[str, Any], kind: str = "") -> dict[str, Any]:
    """The DNS records an owner creates for *record*'s domain, as providers ask for them.

    *kind* (``subdomain`` or ``root``) corrects the guess from the domain's
    labels when the owner says otherwise. Names are given relative to the
    zone (``docs``, ``@``) and in full.
    """
    cfg = current_app.config["HOSTING"]
    domain = record["domain"]
    zone = zone_of(domain)
    if kind == "root":
        zone = domain
    elif kind == "subdomain" and zone == domain and domain.count(".") >= 2:
        zone = domain.split(".", 1)[1]
    root = zone == domain
    host = "@" if root else domain[: -len(zone) - 1]
    challenge = "_bananawiki-challenge" + ("" if root else "." + host)
    target = (cfg.custom_domain_target or "").lower().rstrip(".")
    records = [{"step": "proof", "type": "TXT", "name": challenge, "full": "_bananawiki-challenge." + domain,
                "value": record["verification_token"]}]
    addresses = platform_addresses()
    if root:
        records += [{"step": "point", "type": "AAAA" if ":" in address else "A", "name": "@", "full": domain,
                     "value": address} for address in addresses]
    else:
        records.append({"step": "point", "type": "CNAME", "name": host, "full": domain, "value": target})
    return {"domain": domain, "zone": zone, "root": root, "host": host, "target": target, "records": records,
            "addresses": addresses, "allow_proxied": cfg.custom_domain_allow_proxied,
            "www": root and not domain.startswith("www.")}


def _status(found: bool, other: bool) -> str:
    return "ok" if found else ("wrong" if other else "missing")


def inspect(inst: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Record by record, what DNS shows now for the wiki's claim (the "Check DNS now" checklist)."""
    from . import instances

    runtime = instances.runtime()
    detailed = getattr(runtime, "inspect_domain", None)
    try:
        if detailed:
            found = detailed(record["domain"], record["verification_token"])
        else:
            check = runtime.check_domain(record["domain"], record["verification_token"])
            found = {"txt": [], "cname": [], "addresses": [], "expected_addresses": [], "ownership": check.ownership,
                     "routing": check.routing, "proxied": check.proxied, "dns_error": check.dns_error}
    except RuntimeFailure as error:
        raise ServiceError(f"hosting.runtime.{error.code}") from error
    target = (current_app.config["HOSTING"].custom_domain_target or "").lower().rstrip(".")
    # What DNS shows is repeated to the customer only when it is public: a private, loopback or link-local
    # address, or a name ending like a private network's, is shown as "somewhere else" (a value of "").
    cname = [_public_or_blank(value, name=True) for value in found.get("cname", []) if value]
    addresses = [_public_or_blank(value) for value in found.get("addresses", []) if value]
    routing = bool(found.get("routing"))
    via_cname = target in cname
    return {
        "ownership": bool(found.get("ownership")), "routing": routing, "proxied": bool(found.get("proxied")),
        "dns_error": bool(found.get("dns_error")),
        "rows": [
            {"record": "proof", "status": _status(bool(found.get("ownership")), bool(found.get("txt"))),
             "found": [value[:80] + ("…" if len(value) > 80 else "") for value in found.get("txt", [])][:5]},
            {"record": "point", "status": _status(routing, bool(cname or addresses)),
             "found": (cname if cname and not via_cname else ([target] if via_cname else addresses))[:8],
             "via": "cname" if via_cname else ("proxied" if found.get("proxied") else ("addresses" if routing else ""))},
        ],
        "checked_at": now_sql(),
    }


# Refusals of verify() that a later check may not repeat.
RETRYABLE = frozenset({"hosting.domains.dns_pending", "hosting.domains.txt_mismatch", "hosting.domains.wrong_target"})


def _public_or_blank(value: str, *, name: bool = False) -> str:
    value = str(value).lower().rstrip(".")
    if name:
        last = value.rsplit(".", 1)[-1]
        return "" if last in PRIVATE_SUFFIXES or last.isdigit() else value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return ""
    return value if address.is_global else ""


def check_now(inst: dict[str, Any], actor_id: str | None, *, may_verify: bool = True) -> dict[str, Any]:
    """Look at the wiki's DNS records and, once both are right, verify the domain (owners need not press Verify)."""
    record = binding(inst["id"])
    if record is None:
        raise ServiceError("hosting.domains.claim_first")
    if not configured():
        raise ServiceError("hosting.domains.not_configured")
    result = inspect(inst, record)
    result["verified"] = False
    result["error"] = ""
    if may_verify and result["ownership"] and result["routing"] and not active_domain(inst["id"]):
        try:
            verify(inst, actor_id)
            result["verified"] = True
        except ServiceError as error:
            # The checklist's own lookups passed but the strict check did not (a lookup that failed this
            # time, a record still spreading): say so and keep checking instead of giving up.
            if error.key not in RETRYABLE:
                raise
            result["error"] = error.key
    return result
