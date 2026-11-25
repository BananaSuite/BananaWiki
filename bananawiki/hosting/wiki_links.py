"""Linking a hosted wiki's accounts to hosting accounts: **Portal admin → a wiki → Account links**.

A portal administrator sees the wiki's accounts with their links (read in the
wiki through the runtime, :meth:`~.runtime.Runtime.list_links`), links one
account, runs a bulk link, unlinks, and may then switch the wiki to hosting
accounts only. A link made here is the same as one a person makes: the wiki's
``platform_oauth_links`` row (written by the wiki itself, which checks again
that neither side is linked) and the portal's ``hosting_oauth_account_links``
row, with the same moderation-log entry (``oauth.linked``). Nothing asks the
person's consent: their first sign-in to the wiki still does.

Rules (BananaSuite sign-in spec, section B):

* modes: ``one`` (a wiki account and a hosting account by user name, id or
  verified address), ``username`` (same user name, any case), ``email`` (the
  wiki account's address is a hosting account's verified address), ``both``
  (the account found by user name has that verified address), ``list`` (lines
  ``wiki_username,hosting_username`` or ``…,hosting_id``, at most 500).
* :func:`preview` comes first: each row will link, is already linked, is a
  conflict (the hosting account is linked to another wiki account, the wiki
  account to another hosting account, several candidates, a suspended or
  pending hosting account), is not found, or is skipped (administrators and
  owners unless included, disabled wiki accounts). Its ``token`` is a digest
  of every row: :func:`apply` recomputes the preview and links only when the
  token is unchanged (:class:`StalePreview` otherwise), and only the rows that
  "will link".
* The wiki's addresses are what its members typed (wikis do not verify them):
  the email modes trust them as far as the preview shows.
* Every link and unlink is in the moderation log (who, the rule, the accounts)
  and the wiki's audit log. :func:`switch_to_hosting_only` refuses while an
  active administrator or owner of the wiki is not linked.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from dataclasses import dataclass, field
from typing import Any

from flask import current_app

from . import events, instances, notifications, settings, urls
from .db import db
from .errors import ServiceError
from .runtime import RuntimeFailure, WikiAccount

MODES = ("one", "username", "email", "both", "list")
STATUSES = ("will_link", "conflict", "skipped", "already_linked", "not_found")
LIST_MAX = 500
WIKI_ACCOUNTS_MAX = 10_000
PAGE = 1000
LINK_CHUNK = 200
ADMIN_ROLES = ("admin", "owner")
_CHUNK = 200


class StalePreview(ServiceError):
    """The wiki or the portal changed since the preview; ``rows`` and ``token`` are the new preview."""

    def __init__(self, rows: list[Row], token: str):
        super().__init__("hosting.links.stale")
        self.rows = rows
        self.token = token


@dataclass
class LinkRequest:
    mode: str = "username"
    include_admins: bool = False
    text: str = ""  # mode "list"
    wiki_username: str = ""  # mode "one"
    account: str = ""  # mode "one": a hosting user name, id or verified address

    @classmethod
    def from_form(cls, form: Any) -> LinkRequest:
        mode = (form.get("mode") or "").strip()
        if mode not in MODES:
            raise ServiceError("hosting.links.mode_invalid")
        return cls(mode=mode, include_admins=form.get("include_admins") == "1",
                   text=(form.get("list") or "").replace("\r\n", "\n").replace("\r", "\n")[:LIST_MAX * 200],
                   wiki_username=(form.get("wiki_username") or "").strip()[:50],
                   account=(form.get("account") or "").strip()[:254])

    def fields(self) -> dict[str, str]:
        """The form fields that make this request again (the preview's Link button sends them back)."""
        return {"mode": self.mode, "include_admins": "1" if self.include_admins else "", "list": self.text,
                "wiki_username": self.wiki_username, "account": self.account}


@dataclass
class Row:
    name: str  # the wiki account's name (as typed in a list when it was not found)
    user: WikiAccount | None = None
    account: dict[str, Any] | None = None  # the hosting account the row is about
    status: str = "not_found"
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def key(self) -> list[str]:
        return [self.user.id if self.user else self.name, (self.account or {}).get("id", ""), self.status,
                self.reason]


# Reading both sides


def wiki_accounts(inst: dict[str, Any]) -> list[WikiAccount]:
    """Every account of the wiki with its link (pages of :data:`PAGE` read in the wiki)."""
    spec = instances.spec(inst, with_policy=False)
    found: list[WikiAccount] = []
    while True:
        page, total = instances.runtime().list_links(spec, limit=PAGE, offset=len(found))
        found += page
        if not page or len(found) >= total:
            return found
        if len(found) >= WIKI_ACCOUNTS_MAX:
            raise ServiceError("hosting.links.too_many", max=WIKI_ACCOUNTS_MAX)


def portal_links(inst: dict[str, Any]) -> list[dict[str, Any]]:
    return db.all("SELECT l.*, a.username AS account_username FROM hosting_oauth_account_links l "
                  "LEFT JOIN accounts a ON a.id = l.account_id WHERE l.instance_id = ?", (inst["id"],))


def overview(inst: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The wiki's accounts for the page: each with its link (the wiki's, or the portal's when only it has
    one) and the hosting account's name, plus counts."""
    accounts = wiki_accounts(inst)
    by_user = {row["wiki_user_id"]: row for row in portal_links(inst)}
    names = _accounts_by("id", [link["account_id"] for link in [a.link for a in accounts] if link]
                         + [row["account_id"] for row in by_user.values()])
    items, counts = [], {"total": len(accounts), "linked": 0, "admins_unlinked": 0}
    for user in accounts:
        account_id = (user.link or {}).get("account_id") or (by_user.get(user.id) or {}).get("account_id")
        account = names.get(account_id) if account_id else None
        linked = bool(account_id)
        counts["linked"] += linked
        if not linked and user.role in ADMIN_ROLES and user.status == "active":
            counts["admins_unlinked"] += 1
        items.append({"user": user, "account_id": account_id, "account": account[0] if account else None,
                      "linked_at": (user.link or {}).get("linked_at") or (by_user.get(user.id) or {}).get("linked_at"),
                      "only_portal": linked and not user.link, "only_wiki": linked and user.id not in by_user})
    return items, counts


def _accounts_by(column: str, values: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Hosting accounts (not deleted) whose *column* (``id``, ``username`` any case, ``email`` verified only)
    is one of *values*, by the lower-cased value."""
    wanted = list(dict.fromkeys(value for value in values if value))
    found: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        collate = "" if column == "id" else " COLLATE NOCASE"
        extra = (" AND email <> '' AND email_verified_at IS NOT NULL AND COALESCE(email_flagged_invalid, 0) = 0"
                 if column == "email" else "")
        for row in db.all(f"SELECT * FROM accounts WHERE {column}{collate} IN ({marks}) "  # noqa: S608
                          f"AND deleted_at IS NULL{extra} ORDER BY created_at", chunk):
            found.setdefault(str(row[column]).lower() if column != "id" else row["id"], []).append(row)
    return found


def _inactive(account: dict[str, Any]) -> bool:
    from . import accounts

    return (account["approval_status"] != "approved" or accounts.is_suspended(account)
            or bool(account.get("pending_deletion")))


def _parse_list(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in line]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if len(cells) != 2 or not all(cells):
            raise ServiceError("hosting.links.list_invalid", line=",".join(cells)[:80])
        pairs.append((cells[0], cells[1]))
    if not pairs:
        raise ServiceError("hosting.links.list_empty")
    if len(pairs) > LIST_MAX:
        raise ServiceError("hosting.links.list_too_long", max=LIST_MAX)
    return pairs


# The preview


def preview(inst: dict[str, Any], request: LinkRequest) -> tuple[list[Row], str]:
    """The rows of *request* on *inst* now, and the token :func:`apply` compares."""
    accounts = wiki_accounts(inst)
    by_name = {user.username.lower(): user for user in accounts}
    links = portal_links(inst)
    portal_by_user = {row["wiki_user_id"]: row["account_id"] for row in links}
    linked_users = {}  # hosting account id -> wiki user id, from either side
    for row in links:
        linked_users.setdefault(row["account_id"], row["wiki_user_id"])
    for user in accounts:
        if user.link:
            linked_users[user.link["account_id"]] = user.id

    rows: list[Row] = []
    if request.mode in ("username", "email", "both"):
        by_username = _accounts_by("username", [user.username for user in accounts])
        by_email = _accounts_by("email", [user.email for user in accounts if user.email])
        for user in accounts:
            named = by_username.get(user.username.lower(), [])
            mailed = by_email.get(user.email.lower(), []) if user.email else []
            if request.mode == "username":
                candidates = named
            elif request.mode == "email":
                candidates = mailed
            else:
                candidates = [account for account in named if account["id"] in {a["id"] for a in mailed}]
            rows.append(Row(user.username, user, candidates=candidates))
    else:
        if request.mode == "one":
            if not request.wiki_username or not request.account:
                raise ServiceError("hosting.links.one_missing")
            pairs = [(request.wiki_username, request.account)]
        else:
            pairs = _parse_list(request.text)
        values = [value for _name, value in pairs]
        by_id = _accounts_by("id", values)
        by_username = _accounts_by("username", values)
        by_email = _accounts_by("email", [value for value in values if "@" in value])
        for name, value in pairs:
            user = by_name.get(name.lower())
            candidates = by_id.get(value) or by_username.get(value.lower()) or by_email.get(value.lower(), [])
            rows.append(Row(user.username if user else name, user, candidates=candidates))

    for row in rows:
        _judge(row, request, portal_by_user, linked_users)
    # The same hosting account for several wiki accounts in one run: none of them is linked.
    chosen: dict[str, list[Row]] = {}
    for row in rows:
        if row.status == "will_link":
            chosen.setdefault(row.account["id"], []).append(row)  # type: ignore[index]
    for same in chosen.values():
        if len(same) > 1:
            for row in same:
                row.status, row.reason = "conflict", "several_candidates"
    rows.sort(key=lambda row: (STATUSES.index(row.status), row.name.lower()))
    return rows, _token(inst, request, rows)


def _judge(row: Row, request: LinkRequest, portal_by_user: dict[str, str], linked_users: dict[str, str]) -> None:
    user = row.user
    if user is None:
        row.status, row.reason = "not_found", "wiki_account"
        return
    current = (user.link or {}).get("account_id") or portal_by_user.get(user.id)
    if not row.candidates:
        row.status, row.reason = ("already_linked", "") if current else ("not_found", "hosting_account")
        return
    if len(row.candidates) > 1:
        row.status, row.reason = "conflict", "several_candidates"
        return
    account = row.account = row.candidates[0]
    if current == account["id"]:
        row.status = "already_linked"
    elif current:
        row.status, row.reason = "conflict", "wiki_account_linked"
    elif linked_users.get(account["id"], user.id) != user.id:
        row.status, row.reason = "conflict", "hosting_account_linked"
    elif _inactive(account):
        row.status, row.reason = "conflict", "hosting_account_inactive"
    elif user.status != "active":
        row.status, row.reason = "skipped", "wiki_account_disabled"
    elif user.role in ADMIN_ROLES and not request.include_admins:
        row.status, row.reason = "skipped", "administrator"
    else:
        row.status = "will_link"


def _token(inst: dict[str, Any], request: LinkRequest, rows: list[Row]) -> str:
    payload = json.dumps([inst["id"], request.fields(), [row.key() for row in rows]], sort_keys=True)
    key = current_app.config["HOSTING"].secret_key.encode("utf-8")
    return hmac.new(key, b"account-links:" + payload.encode("utf-8"), hashlib.sha256).hexdigest()


def counts(rows: list[Row]) -> dict[str, int]:
    return {status: sum(1 for row in rows if row.status == status) for status in STATUSES}


# Linking and unlinking


def apply(inst: dict[str, Any], request: LinkRequest, token: str, *, actor: dict[str, Any],
          notify: bool = False) -> dict[str, int]:
    """Link the "will link" rows of the preview *token* was given for; :class:`StalePreview` when anything
    changed since. Returns how many were linked, refused by the wiki and told by email."""
    instances.require_ready(inst)
    if inst["status"] == "terminated":
        raise ServiceError("hosting.instances.terminated")
    rows, fresh = preview(inst, request)
    if not token or not hmac.compare_digest(token, fresh):
        raise StalePreview(rows, fresh)
    wanted = [row for row in rows if row.status == "will_link"]
    if not wanted:
        raise ServiceError("hosting.links.nothing")
    spec = instances.spec(inst, with_policy=False)
    linked, refused = [], 0
    for start in range(0, len(wanted), LINK_CHUNK):
        chunk = wanted[start:start + LINK_CHUNK]
        done, why = instances.runtime().link_users(
            spec, [(row.user.id, row.account["id"], row.account["username"]) for row in chunk],  # type: ignore[union-attr,index]
            actor=actor["username"], rule=request.mode)
        refused += len(why)
        linked += [row for row in chunk if row.user.id in set(done)]  # type: ignore[union-attr]
    told = 0
    for row in linked:
        _record_link(inst, row, actor, request.mode)
        if notify and _notify(inst, row):
            told += 1
    return {"linked": len(linked), "refused": refused, "told": told}


def _record_link(inst: dict[str, Any], row: Row, actor: dict[str, Any], rule: str) -> None:
    """The portal's side of a link the wiki made: the row a person's link writes, and the moderation log."""
    account, user = row.account, row.user
    with db.transaction():
        # A link of this wiki account to another hosting account here is stale: the wiki has the new one.
        db.execute("DELETE FROM hosting_oauth_account_links WHERE instance_id = ? AND wiki_user_id = ?",
                   (inst["id"], user.id))  # type: ignore[union-attr]
        db.execute("INSERT OR REPLACE INTO hosting_oauth_account_links (instance_id, account_id, wiki_user_id, "
                   "wiki_username) VALUES (?, ?, ?, ?)", (inst["id"], account["id"], user.id, user.username))  # type: ignore[index,union-attr]
        events.record("account", account["id"], "oauth.linked", actor["id"], inst["subdomain"])  # type: ignore[index]
        events.record("instance", inst["id"], "oauth.admin_linked", actor["id"],
                      f"{user.username} -> {account['username']} ({rule})")  # type: ignore[index,union-attr]


def _notify(inst: dict[str, Any], row: Row) -> bool:
    return notifications.notify_account(row.account, "wiki_account_linked", wiki=urls.original_slug(
        inst["subdomain"]) or inst["subdomain"], wiki_username=row.user.username,  # type: ignore[union-attr]
        action_url=urls.instance_url(inst))


def unlink(inst: dict[str, Any], wiki_user_id: str, *, actor: dict[str, Any]) -> bool:
    """Drop a wiki account's link on both sides (the wiki ends the sessions that came through it); False when
    it had none."""
    instances.require_ready(inst)
    try:
        account_id = instances.runtime().unlink_user(instances.spec(inst, with_policy=False), wiki_user_id,
                                                     actor=actor["username"])
    except RuntimeFailure as error:
        if error.code != "user_not_found":
            raise
        account_id = None  # the wiki account is gone: only the portal's link of it is left to drop
    with db.transaction():
        rows = db.all("SELECT * FROM hosting_oauth_account_links WHERE instance_id = ? AND wiki_user_id = ?",
                      (inst["id"], wiki_user_id))
        db.execute("DELETE FROM hosting_oauth_account_links WHERE instance_id = ? AND wiki_user_id = ?",
                   (inst["id"], wiki_user_id))
        unlinked = {row["account_id"] for row in rows} | ({account_id} if account_id else set())
        for account in unlinked:
            if db.scalar("SELECT 1 FROM accounts WHERE id = ?", (account,)):
                events.record("account", account, "oauth.unlinked", actor["id"], inst["subdomain"])
        if unlinked:
            names = ", ".join(row["wiki_username"] for row in rows) or wiki_user_id
            events.record("instance", inst["id"], "oauth.admin_unlinked", actor["id"], names[:500])
    return bool(unlinked)


def admins_unlinked(inst: dict[str, Any]) -> list[str]:
    """The names of the wiki's active administrators and owners without a link to a hosting account."""
    items, _counts = overview(inst)
    return [item["user"].username for item in items
            if item["user"].role in ADMIN_ROLES and item["user"].status == "active" and not item["account_id"]]


def switch_to_hosting_only(inst: dict[str, Any], *, actor: dict[str, Any]) -> bool:
    """Members sign in with hosting accounts only (``platform``), refused while an active administrator or
    owner is not linked; returns False when the wiki could not restart (the mode is kept for its next start)."""
    if not settings.flag("platform_oauth_enabled"):
        raise ServiceError("hosting.links.oauth_off")
    missing = admins_unlinked(inst)
    if missing:
        listed = ", ".join(missing[:10]) + (" …" if len(missing) > 10 else "")
        raise ServiceError("hosting.links.admins_unlinked", names=listed)
    return instances.set_signin_mode(inst, "platform", actor_id=actor["id"])
