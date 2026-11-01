"""Wiki collaborators and ownership transfers.

A collaborator holding ``manage_collaborators`` may only hand out
permissions it holds itself, cannot grant ``full_access`` and cannot change
or remove owners of full access. Transfers are always made from the current
owner, also when an administrator starts one. The recipient must be an
active, approved account, and accepting a transfer applies the new owner's
privileges.

Accepting a transfer does not leave the previous owner as a full-access
collaborator (able to terminate the wiki or reset its administrator's
password). The recipient chooses: remove them (the default) or keep them as
a viewer. The other collaborators leave with the previous owner unless the
recipient keeps each of them on the offer (anyone the previous owner added,
an account of their own among them, could otherwise hand them back every
permission). The recipient's wiki limit applies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from ..core.timeutil import now_sql, sql_in
from . import accounts, events, instances, limits
from .auth import admin_mfa_missing
from .db import db
from .errors import ServiceError

PERMISSIONS = ("view", "start_stop", "terminate", "reset_password", "reset_wiki", "download", "toggle_mode",
               "manage_collaborators", "use_case", "analytics")
ROLES = ("full_access", "custom")
# What the previous owner keeps after a transfer, as the recipient chooses.
PREVIOUS_OWNER_ACCESS = {"none": None, "view": ["view"]}
# Ownership offers one wiki may make in 24 hours: each new offer replaces the last and emails the
# receiver, so offering again and again would otherwise add rows and emails without end.
MAX_TRANSFERS_PER_DAY = 10
# Grants one wiki may make in 24 hours (collaborators added, access widened): each writes a moderation-log
# entry kept for years, so adding and removing someone again and again would otherwise grow it without end.
# Taking access away is never refused, and costs one entry per grant at most.
MAX_GRANTS_PER_DAY = 100


def _permissions(row: dict[str, Any]) -> set[str]:
    if row["role"] == "full_access":
        return set(PERMISSIONS)
    try:
        return {p for p in json.loads(row["permissions"] or "[]") if p in PERMISSIONS} | {"view"}
    except (TypeError, ValueError):
        return {"view"}


def permissions_from(role: str | None, permissions: str | None) -> set[str]:
    """What a collaborator with this stored *role* and *permissions* (JSON) may do; nothing without a role."""
    return _permissions({"role": role, "permissions": permissions}) if role else set()


def membership(instance_id: str, account_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                  (instance_id, account_id))


def acts_as_admin(account: dict[str, Any]) -> bool:
    """An administrator who may use administrator rights now (two-step verification on when required)."""
    return bool(account["is_admin"]) and not admin_mfa_missing(account)


def permissions_of(inst: dict[str, Any], account: dict[str, Any]) -> set[str]:
    """What *account* may do on *inst*: everything for owners and administrators."""
    if inst["account_id"] == account["id"] or acts_as_admin(account):
        return set(PERMISSIONS)
    row = membership(inst["id"], account["id"])
    return _permissions(row) if row else set()


def can(inst: dict[str, Any] | None, account: dict[str, Any], permission: str) -> bool:
    return inst is not None and permission in permissions_of(inst, account)


def list_for(instance_id: str) -> list[dict[str, Any]]:
    rows = db.all("SELECT c.*, a.username FROM instance_collaborators c JOIN accounts a ON a.id = c.account_id "
                  "WHERE c.instance_id = ? ORDER BY c.created_at", (instance_id,))
    return [{**row, "granted": sorted(_permissions(row))} for row in rows]


def _check_grant(inst: dict[str, Any], actor: dict[str, Any], role: str, permissions: list[str]) -> list[str]:
    if role not in ROLES:
        raise ServiceError("hosting.collaborators.invalid_role")
    granted = sorted({p for p in permissions if p in PERMISSIONS})
    held = permissions_of(inst, actor)
    privileged = acts_as_admin(actor) or inst["account_id"] == actor["id"]
    if not privileged and (role == "full_access" or not set(granted) <= held):
        raise ServiceError("hosting.collaborators.cannot_grant")
    return [] if role == "full_access" else granted


def _count_grant(inst: dict[str, Any], actor: dict[str, Any]) -> None:
    """Count one grant against the wiki's daily budget (:data:`MAX_GRANTS_PER_DAY`); administrators are not counted."""
    if acts_as_admin(actor):
        return
    if not limits.hit("hosting_collaborator_grants", MAX_GRANTS_PER_DAY, 86400, key=inst["id"]):
        raise ServiceError("hosting.collaborators.too_many", limit=MAX_GRANTS_PER_DAY)


def add(inst: dict[str, Any], actor: dict[str, Any], username: str, role: str, permissions: list[str]) -> None:
    target = accounts.active_by_username(username)
    if target is None or target["approval_status"] != "approved":
        raise ServiceError("hosting.accounts.not_found")
    if target["id"] == inst["account_id"]:
        raise ServiceError("hosting.collaborators.owner")
    granted = _check_grant(inst, actor, role, permissions)
    try:
        with db.transaction():
            _count_grant(inst, actor)
            db.insert("instance_collaborators", {
                "instance_id": inst["id"], "account_id": target["id"], "role": role,
                "permissions": json.dumps(granted), "invited_by": actor["id"], "created_at": now_sql(),
            })
            events.record("instance", inst["id"], "collaborator.added", actor["id"], target["username"])
    except sqlite3.IntegrityError as error:
        raise ServiceError("hosting.collaborators.exists") from error
    _tell(target, "collaborator_added", inst, actor)


def _editable(inst: dict[str, Any], actor: dict[str, Any], member: dict[str, Any] | None) -> dict[str, Any]:
    if member is None:
        raise ServiceError("hosting.collaborators.not_found")
    privileged = acts_as_admin(actor) or inst["account_id"] == actor["id"]
    if not privileged and member["role"] == "full_access":
        raise ServiceError("hosting.collaborators.cannot_grant")
    return member


def update(inst: dict[str, Any], actor: dict[str, Any], account_id: str, role: str, permissions: list[str]) -> None:
    member = _editable(inst, actor, membership(inst["id"], account_id))
    granted = _check_grant(inst, actor, role, permissions)
    if member["role"] == role and _permissions(member) == _permissions({"role": role, "permissions": json.dumps(granted)}):
        return  # the same access ("view" always included, in any order): nothing to write or record
    with db.transaction():
        if (role == "full_access" and member["role"] != "full_access") or not set(granted) <= _permissions(member):
            _count_grant(inst, actor)  # widened: a grant
        db.update("instance_collaborators", {"role": role, "permissions": json.dumps(granted)},
                  "instance_id = ? AND account_id = ?", (inst["id"], account_id))
        events.record("instance", inst["id"], "collaborator.updated", actor["id"], account_id)


def remove(inst: dict[str, Any], actor: dict[str, Any], account_id: str) -> None:
    member = membership(inst["id"], account_id)
    if account_id != actor["id"]:
        _editable(inst, actor, member)
    elif member is None:
        raise ServiceError("hosting.collaborators.not_found")
    db.execute("DELETE FROM instance_collaborators WHERE instance_id = ? AND account_id = ?", (inst["id"], account_id))
    events.record("instance", inst["id"], "collaborator.removed", actor["id"], account_id)


# Ownership transfers


def pending_transfer(instance_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT t.*, a.username AS to_username FROM instance_ownership_transfers t JOIN accounts a "
        "ON a.id = t.to_account_id WHERE t.instance_id = ? AND t.status = 'pending' ORDER BY t.id DESC LIMIT 1",
        (instance_id,),
    )


def outgoing_transfers(account_id: str) -> dict[str, str]:
    """The account's wikis with a transfer waiting for the receiver: ``{instance_id: receiver's username}``."""
    rows = db.all(
        "SELECT t.instance_id, a.username AS to_username FROM instance_ownership_transfers t "
        "JOIN accounts a ON a.id = t.to_account_id JOIN instances i ON i.id = t.instance_id "
        "WHERE i.account_id = ? AND t.status = 'pending' AND i.status != 'terminated' ORDER BY t.id",
        (account_id,),
    )
    return {row["instance_id"]: row["to_username"] for row in rows}


def incoming_transfers(account_id: str) -> list[dict[str, Any]]:
    """Offers waiting for *account_id*, each with ``team``: the wiki's other collaborators, each with the
    ``keep`` value the accept form posts to keep them."""
    rows = db.all(
        "SELECT t.*, f.username AS from_username, i.subdomain FROM instance_ownership_transfers t "
        "JOIN accounts f ON f.id = t.from_account_id JOIN instances i ON i.id = t.instance_id "
        "WHERE t.to_account_id = ? AND t.status = 'pending' AND i.status != 'terminated' ORDER BY t.created_at DESC",
        (account_id,),
    )
    return [{**row, "team": [{**member, "keep": keep_token(member)} for member in list_for(row["instance_id"])
                             if member["account_id"] != account_id]} for row in rows]


def keep_token(member: dict[str, Any]) -> str:
    """What the accept form posts to keep *member*: it names the access the recipient saw, so a collaborator
    given more access after the offer was shown is not kept by that tick."""
    seen = f"{member['account_id']}|{member['role']}|{','.join(sorted(_permissions(member)))}"
    return hashlib.sha256(seen.encode("utf-8")).hexdigest()[:24]


def _leave_team(instance_id: str, recipient_id: str, keep: set[str]) -> None:
    """Remove the collaborators the recipient of a transfer did not keep (in the transaction that changes
    the owner, so no one left over can act as a collaborator once the wiki is theirs)."""
    for member in db.all("SELECT * FROM instance_collaborators WHERE instance_id = ? AND account_id != ?",
                         (instance_id, recipient_id)):
        if keep_token(member) in keep:
            continue
        db.execute("DELETE FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                   (instance_id, member["account_id"]))
        events.record("instance", instance_id, "collaborator.removed", recipient_id, member["account_id"])


def start_transfer(inst: dict[str, Any], actor: dict[str, Any], username: str) -> None:
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    if inst["status"] == "suspended":
        raise ServiceError("hosting.instances.suspended_locked")
    target = accounts.active_by_username(username)
    if target is None or target["approval_status"] != "approved" or target["suspended"]:
        raise ServiceError("hosting.accounts.not_found")
    if target["id"] == inst["account_id"]:
        raise ServiceError("hosting.transfers.already_owner")
    with db.transaction():
        offered = db.scalar("SELECT COUNT(*) FROM instance_ownership_transfers WHERE instance_id = ? AND created_at > ?",
                            (inst["id"], sql_in(hours=-24)), default=0)
        if int(offered) >= MAX_TRANSFERS_PER_DAY:
            raise ServiceError("hosting.transfers.too_many", limit=MAX_TRANSFERS_PER_DAY)
        db.execute("UPDATE instance_ownership_transfers SET status = 'cancelled', resolved_at = ? "
                   "WHERE instance_id = ? AND status = 'pending'", (now_sql(), inst["id"]))
        db.insert("instance_ownership_transfers", {
            "instance_id": inst["id"], "from_account_id": inst["account_id"], "to_account_id": target["id"],
            "status": "pending", "created_at": now_sql(),
        })
        events.record("instance", inst["id"], "transfer.started", actor["id"], target["username"])
    _tell(target, "transfer_offered", inst, actor)


def _tell(target: dict[str, Any], kind: str, inst: dict[str, Any], actor: dict[str, Any]) -> None:
    """Email *target* that *actor* added them to the wiki or offered it to them (best effort)."""
    from . import notifications, urls

    # Charged to the account that acted (administrators excepted), so adding and removing people cannot
    # spend the platform's daily email budget.
    notifications.notify_account(target, kind, charge=None if acts_as_admin(actor) else actor["id"],
                                 slug=urls.original_slug(inst["subdomain"]) or inst["subdomain"],
                                 by=actor.get("username", ""), action_url=urls.external_url("dashboard.dashboard"))


def accept_transfer(transfer_id: int, account: dict[str, Any], *, previous_owner: str = "none",
                    keep: Iterable[str] = ()) -> str:
    """Make *account* the owner; *previous_owner* is what the old owner keeps (``none`` or ``view``).

    *keep* holds the :func:`keep_token` of each other collaborator the
    recipient keeps; every other collaborator is removed with the move.
    The recipient's wiki limit is checked before anything changes, so a
    refused transfer stays pending. Anything but a known choice keeps nothing.
    """
    keep = set(keep)
    row = db.one("SELECT * FROM instance_ownership_transfers WHERE id = ? AND status = 'pending'", (transfer_id,))
    if row is None or row["to_account_id"] != account["id"]:
        raise ServiceError("hosting.transfers.not_found")
    inst = instances.get(row["instance_id"])
    if inst is None or inst["status"] == "terminated" or inst["account_id"] != row["from_account_id"]:
        _cancel(transfer_id)
        raise ServiceError("hosting.transfers.stale")
    # An offer made before an administrator stepped in must not hand the wiki on (and back online)
    # afterwards: a suspended wiki, or an owner suspended, denied or due for deletion, ends it.
    if inst["status"] == "suspended" or not _may_hand_over(accounts.get(row["from_account_id"])):
        _cancel(transfer_id)
        raise ServiceError("hosting.transfers.moderated")
    if not _may_hand_over(account):  # nor may a moderated account take a wiki over
        raise ServiceError("hosting.transfers.not_found")

    def in_good_standing() -> None:
        current = instances.get(inst["id"])
        if (current is None or current["status"] in ("terminated", "suspended")
                or current["account_id"] != row["from_account_id"]
                or not _may_hand_over(accounts.get(row["from_account_id"]))
                or not _may_hand_over(accounts.get(account["id"]))):
            raise ServiceError("hosting.transfers.moderated")

    def hand_over() -> None:
        in_good_standing()
        _leave_team(inst["id"], account["id"], keep)

    # The offer is claimed, its checks made again under the write lock, before anything changes (an apex
    # wiki is renamed first): a decline, a cancellation or moderation committed meanwhile wins, and none
    # of them can come between the claim and the move.
    with db.transaction():
        in_good_standing()
        # The recipient's place is checked here and held from now on: the accepted offer counts in
        # check_account_capacity, so a wiki created meanwhile cannot fail the move half-way.
        instances.check_account_capacity(account, receiving=transfer_id)
        if not db.execute("UPDATE instance_ownership_transfers SET status = 'accepted', resolved_at = ? "
                          "WHERE id = ? AND status = 'pending'", (now_sql(), transfer_id)).rowcount:
            raise ServiceError("hosting.transfers.not_found")
    try:
        # Checked once more in the transaction that changes the owner (moderation after the claim).
        instances.move_to_owner(inst, account, actor_id=account["id"], check_capacity=True,
                                guard=hand_over, transfer_id=transfer_id)
    except (ServiceError, sqlite3.Error) as error:
        # The wiki did not move: moderation ends the offer, anything else (the recipient's wiki limit)
        # leaves it open.
        ended = getattr(error, "key", None) == "hosting.transfers.moderated"
        db.execute("UPDATE instance_ownership_transfers SET status = ?, resolved_at = ? WHERE id = ? "
                   "AND status = 'accepted'", ("cancelled" if ended else "pending", now_sql() if ended else None,
                                                transfer_id))
        raise
    kept = PREVIOUS_OWNER_ACCESS.get(previous_owner)
    with db.transaction():
        if kept:
            db.execute(
                "INSERT OR IGNORE INTO instance_collaborators (instance_id, account_id, role, permissions, "
                "invited_by, created_at) VALUES (?, ?, 'custom', ?, ?, ?)",
                (inst["id"], row["from_account_id"], json.dumps(kept), account["id"], now_sql()),
            )
        events.record("instance", inst["id"], "transfer.accepted", account["id"],
                      f"previous owner: {previous_owner if kept else 'none'}")
    return inst["id"]


def _cancel(transfer_id: int) -> None:
    db.update("instance_ownership_transfers", {"status": "cancelled", "resolved_at": now_sql()}, "id = ?",
              (transfer_id,))


def _may_hand_over(owner: dict[str, Any] | None) -> bool:
    """Whether *owner* is in good standing to give a wiki away (not suspended, denied or due for deletion)."""
    return bool(owner) and not owner["deleted_at"] and not accounts.is_suspended(owner) \
        and not owner["pending_deletion"] and owner["approval_status"] == "approved"


def resolve_transfer(transfer_id: int, account_id: str, *, as_sender: bool) -> None:
    column, status = ("from_account_id", "cancelled") if as_sender else ("to_account_id", "declined")
    changed = db.execute(
        f"UPDATE instance_ownership_transfers SET status = ?, resolved_at = ? WHERE id = ? AND {column} = ? "
        "AND status = 'pending'", (status, now_sql(), transfer_id, account_id),
    ).rowcount
    if not changed:
        raise ServiceError("hosting.transfers.not_found")
