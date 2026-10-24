"""Requests for restricted wiki features (public access, page builder, a custom domain).

Public access and the page builder are part of the wiki's policy: granting
one restarts the wiki with it. A custom domain is not: granting it sets the
wiki's own permission (``instances.custom_domain_allowed``, see
:mod:`.domains`) and the owner sets the domain up on its page.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..core.timeutil import now_sql
from . import attention, events, instances, settings
from .db import db
from .errors import ServiceError

# Feature -> the instances column that grants it.
FEATURES = {**instances.FEATURES, "custom_domain": "custom_domain_allowed"}
POLICY_FEATURES = frozenset(instances.FEATURES)  # granting one restarts the wiki with its new policy
RESTRICTION = {"public_access": "forbid_non_admin_public_wikis", "page_builder": "forbid_non_admin_page_builder"}
AUTO_APPROVE = {"public_access": "auto_approve_public_access_requests",
                "page_builder": "auto_approve_page_builder_requests"}


def restricted(feature: str) -> bool:
    """Whether owners must ask for *feature* (a custom domain: see :func:`available`, which depends on the owner)."""
    if feature == "custom_domain":
        return settings.custom_domains_policy() != "everyone"
    return settings.flag(RESTRICTION[feature], True)


def available(inst: dict[str, Any], feature: str, owner: dict[str, Any] | None = None) -> bool:
    """Whether the wiki has *feature* without asking (granted, or not restricted for its owner)."""
    if feature == "custom_domain":
        from . import domains

        return domains.allowed(inst, owner)
    return bool(inst.get(FEATURES[feature])) or not restricted(feature)


def history(instance_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT r.*, q.username AS requested_by_username, v.username AS reviewed_by_username "
        "FROM instance_feature_requests r JOIN accounts q ON q.id = r.requested_by "
        "LEFT JOIN accounts v ON v.id = r.reviewed_by WHERE r.instance_id = ? ORDER BY r.requested_at DESC, r.id DESC",
        (instance_id,),
    )


def pending_queue() -> list[dict[str, Any]]:
    return db.all(
        "SELECT r.*, i.subdomain, a.username AS owner_username FROM instance_feature_requests r "
        "JOIN instances i ON i.id = r.instance_id JOIN accounts a ON a.id = i.account_id "
        "WHERE r.status = 'pending' ORDER BY r.requested_at, r.id"
    )


def request(inst: dict[str, Any], account: dict[str, Any], feature: str, reason: str) -> dict[str, Any]:
    """Owner asks for a feature; policy may approve it at once (then the wiki restarts)."""
    if feature not in FEATURES:
        raise ServiceError("hosting.features.unknown")
    if inst["account_id"] != account["id"]:
        raise ServiceError("hosting.instances.not_found")
    if feature == "custom_domain":
        from . import domains

        if not domains.configured():
            raise ServiceError("hosting.domains.not_configured")
        if domains.allowed(inst, account):
            raise ServiceError("hosting.features.already_available")
    elif account["is_admin"] or not restricted(feature):
        raise ServiceError("hosting.features.already_available")
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if inst.get(FEATURES[feature]):
        raise ServiceError("hosting.features.already_granted")
    reason = (reason or "").strip()
    if not 20 <= len(reason) <= 2000:
        raise ServiceError("hosting.features.reason_length")
    automatic = feature in AUTO_APPROVE and settings.flag(AUTO_APPROVE[feature])
    try:
        with db.transaction():
            request_id = db.insert("instance_feature_requests", {
                "instance_id": inst["id"], "feature": feature, "requested_by": account["id"], "reason": reason,
                "status": "approved" if automatic else "pending", "requested_at": now_sql(),
                "reviewed_at": now_sql() if automatic else None, "review_source": "automatic" if automatic else "manual",
                "review_note": "Automatically approved by platform policy." if automatic else "",
            })
            if automatic:
                db.update("instances", {FEATURES[feature]: 1}, "id = ?", (inst["id"],))
            events.record("instance", inst["id"], f"feature.{feature}.requested", account["id"], reason)
            if not automatic:
                attention.created("features.requests", request_id)
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.features.pending_exists") from error
    if automatic and feature in POLICY_FEATURES:
        instances.apply_policy(inst, actor_id=account["id"])
    return db.one("SELECT * FROM instance_feature_requests WHERE id = ?", (request_id,))  # type: ignore[return-value]


def cancel(request_id: int, account: dict[str, Any]) -> None:
    changed = db.execute(
        "UPDATE instance_feature_requests SET status = 'cancelled', cancelled_at = ? WHERE id = ? AND status = 'pending' "
        "AND instance_id IN (SELECT id FROM instances WHERE account_id = ?)", (now_sql(), request_id, account["id"]),
    ).rowcount
    if not changed:
        raise ServiceError("hosting.features.not_pending")


def review(request_id: int, admin: dict[str, Any], decision: str, note: str) -> dict[str, Any]:
    if decision not in ("approved", "denied"):
        raise ServiceError("hosting.features.invalid_decision")
    note = (note or "").strip()
    if len(note) > 2000:
        raise ServiceError("hosting.features.note_length")
    row = db.one("SELECT r.*, i.status AS instance_status, i.subdomain FROM instance_feature_requests r "
                 "JOIN instances i ON i.id = r.instance_id WHERE r.id = ?", (request_id,))
    if row is None:
        raise ServiceError("hosting.features.not_found")
    if row["status"] != "pending":
        raise ServiceError("hosting.features.not_pending")
    if decision == "approved" and row["instance_status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    with db.transaction():
        changed = db.execute(
            "UPDATE instance_feature_requests SET status = ?, reviewed_by = ?, review_note = ?, review_source = 'manual', "
            "reviewed_at = ? WHERE id = ? AND status = 'pending'", (decision, admin["id"], note, now_sql(), request_id),
        ).rowcount
        if not changed:
            raise ServiceError("hosting.features.not_pending")
        if decision == "approved":
            db.update("instances", {FEATURES[row["feature"]]: 1}, "id = ?", (row["instance_id"],))
        events.record("instance", row["instance_id"], f"feature.{row['feature']}.{decision}", admin["id"], note)
        attention.decided(row["requested_by"], "features.requests", decision, object_id=row["feature"],
                          detail=row["subdomain"])
    applied = True
    if decision == "approved" and row["feature"] in POLICY_FEATURES:
        applied = instances.apply_policy(instances.get(row["instance_id"]),  # type: ignore[arg-type]
                                         actor_id=admin["id"])
    elif decision == "approved":
        instances.sync_routes()  # a custom domain the wiki already proved answers again at once
    return {**row, "policy_applied": applied}


def pending_request(instance_id: str, feature: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM instance_feature_requests WHERE instance_id = ? AND feature = ? AND status = 'pending' "
                  "ORDER BY id LIMIT 1", (instance_id, feature))


def set_entitlement(inst: dict[str, Any], feature: str, allowed: bool, admin: dict[str, Any]) -> bool:
    """Grant or revoke *feature* on the wiki page.

    Granting a feature the owner is waiting for approves that request as
    :func:`review` would (the owner hears about it and the request leaves the
    queue), so the request is never left pending next to a granted feature.
    """
    if feature not in FEATURES:
        raise ServiceError("hosting.features.unknown")
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    waiting = pending_request(inst["id"], feature) if allowed else None
    if waiting is not None:
        return bool(review(waiting["id"], admin, "approved", "")["policy_applied"])
    if feature == "custom_domain":
        from . import domains

        domains.set_permission(inst, allowed, admin["id"])
        instances.sync_routes()
        return True
    db.update("instances", {FEATURES[feature]: 1 if allowed else 0}, "id = ?", (inst["id"],))
    events.record("instance", inst["id"], f"feature.{feature}." + ("granted" if allowed else "revoked"), admin["id"])
    return instances.apply_policy(inst, actor_id=admin["id"])
