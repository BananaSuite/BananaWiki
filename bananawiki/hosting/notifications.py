"""Emails the portal sends: verification, recovery, account notices and notifications.

Every message is built from translation keys ``email.<kind>.subject`` and
``email.<kind>.body`` (``{username}``, ``{contact}``, ``{portal}`` plus the
values passed in), in the language of the current request or the
recipient's own language. Sending is best effort: a failure is logged and
reported to the caller, never raised.

Administrators hear about waiting requests (:mod:`.attention`) from the
maintenance service, never from the request that created them: immediately,
as a digest at most every N minutes, or in a daily summary
(``approval_notify_*`` settings; throttling state in
``hosting_attention_recipients``), and only about the queues switched on in
``attention_email_sources``. Owners get one email per decision on their own
requests (``hosting_account_notices``).

Administrators switch every notice in :data:`NOTICES` on or off for the whole
platform (``email_notices``); a notice switched off is not sent and not kept
for later (:func:`send` answers ``disabled``). Messages that prove an address
or recover an account (:data:`ALWAYS_SENT`) have no switch: only pausing all
email (:mod:`.email`) stops them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask import current_app

from ..core import attention as engine
from ..core.i18n import FALLBACK
from ..core.timeutil import now_sql, sql_in
from . import attention, email, settings, urls
from .db import db
from .i18n import t, t_lang

log = logging.getLogger("bananawiki.hosting.notifications")

DECISION_WINDOW_DAYS = 2


@dataclass(frozen=True)
class Notice:
    """A message account holders get when something happens; labelled ``hosting.notices.<kind>``."""

    kind: str  # its email.<kind>.* texts
    group: str  # hosting.notices.group.<group>


NOTICE_GROUPS = ("wikis", "account", "requests")
NOTICES = (
    Notice("instance_created", "wikis"),
    Notice("instance_suspended", "wikis"),
    Notice("instance_unsuspended", "wikis"),
    Notice("instance_terminated", "wikis"),
    Notice("expiry_warning", "wikis"),
    Notice("instance_expired", "wikis"),
    Notice("instance_storage_over_plan", "wikis"),
    Notice("instance_storage_suspended", "wikis"),
    Notice("instance_storage_reopened", "wikis"),
    Notice("collaborator_added", "wikis"),
    Notice("transfer_offered", "wikis"),
    Notice("account_approved", "account"),
    Notice("account_denied", "account"),
    Notice("account_suspended", "account"),
    Notice("account_restored", "account"),
    Notice("deletion_scheduled", "account"),
    Notice("account_deleted", "account"),
    Notice("password_changed", "account"),
    Notice("email_changed", "account"),
    Notice("decision", "requests"),
)
NOTICE_KINDS = frozenset(notice.kind for notice in NOTICES)
# Address proofs and account recovery: no switch, since turning them off would lock people out.
ALWAYS_SENT = ("verify", "reset", "username")
# Why a message was not sent on purpose: nobody is told it failed, and callers do not try again.
NOT_SENT_ON_PURPOSE = frozenset({"paused", "disabled"})


def notice_enabled(kind: str) -> bool:
    """Whether the notice *kind* is switched on (anything that is not in :data:`NOTICES` always is)."""
    return kind not in NOTICE_KINDS or settings.switched_on("email_notices", kind)


def attention_source_emailed(source_id: str) -> bool:
    """Whether administrators are emailed about the waiting-request queue *source_id*."""
    return settings.switched_on("attention_email_sources", source_id)


def configured() -> bool:
    return email.is_configured(current_app.config["HOSTING"].email)


def send(to: str, kind: str, *, username: str = "", action_url: str = "", detail_value: str = "",
         lang: str | None = None, account_id: str | None = None, **values: Any) -> tuple[bool, str]:
    """Send one notice built from ``email.<kind>.*`` in *lang* (default: the request's language).

    *account_id* is the account whose own action caused the message: it is
    charged to that account's daily email budget (see :mod:`.email`).
    A notice an administrator switched off answers ``(False, "disabled")``.
    """
    if not notice_enabled(kind):
        return False, "disabled"
    cfg = current_app.config["HOSTING"]

    def tr(key: str, default: str | None = None, /, **params: Any) -> str:
        return t_lang(lang, key, default, **params) if lang else t(key, default, **params)

    context = {"username": username or tr("email.there"), "contact": cfg.contact_email,
               "portal": urls.portal_base_url(), **values}
    message = email.Message(
        to=to,
        subject=tr(f"email.{kind}.subject", **context),
        text=tr(f"email.{kind}.body", **context),
        title=tr(f"email.{kind}.title", tr(f"email.{kind}.subject", **context), **context),
        eyebrow=tr(f"email.{kind}.eyebrow", "", **context),  # default: the platform's name (email.send)
        action_url=action_url,
        action_label=tr(f"email.{kind}.action", "", **context) if action_url else "",
        detail_label=tr(f"email.{kind}.detail", "", **context) if detail_value else "",
        detail_value=detail_value,
    )
    try:
        ok, reason = email.send(cfg, message, tr("email.footer", contact=cfg.contact_email), kind=kind,
                                account_id=account_id)
    except Exception:  # noqa: BLE001 - a notice must never break the action that triggered it
        log.exception("Email %s to %s failed", kind, to)
        return False, "failed"
    if not ok and reason not in NOT_SENT_ON_PURPOSE:
        log.warning("Email %s to %s not sent: %s", kind, to, reason)
    return ok, reason


def notify_account(account: dict[str, Any] | None, kind: str, *, by_account: bool = False,
                   charge: str | None = None, **values: Any) -> bool:
    """Send an account notice to the account's contact address, if it has one.

    *by_account*: the account's own action caused it, so it counts toward
    that account's daily email budget and speaks the language of the page.
    Otherwise (an administrator, another account or the platform acted) it
    speaks the account's own language. *charge*: the id of another account
    whose action caused it (adding a collaborator, offering a wiki), charged
    instead, so nobody can spend the platform's budget on other people's
    inboxes.
    """
    if not account or not account.get("email") or not configured():
        return False
    if not by_account and "lang" not in values and account.get("language"):
        values["lang"] = account["language"]
    return send(account["email"], kind, username=account.get("username", ""),
                account_id=account["id"] if by_account else charge, **values)[0]


def password_changed(account_id: str, *, by_account: bool) -> bool:
    """Tell the account's address that its password changed (by itself or an administrator)."""
    from . import accounts

    return notify_account(accounts.get(account_id), "password_changed", by_account=by_account,
                          action_url=urls.external_url("account.account"))


def email_changed(account: dict[str, Any], old_address: str, *, by_account: bool) -> bool:
    """Tell the address an account used until now (*account* as it was before) that it moved to another one.

    Sent after the new address's confirmation link and not charged to the
    account's own budget, so it never costs that link. Only an address that
    was confirmed hears about it, whether or not verification is enforced: a
    typo corrected at once mails no stranger, and nobody can mail any address
    they type by changing their own address again and again.
    """
    if not old_address or not configured() or not account.get("email_verified_at"):
        return False
    lang = None if by_account else account.get("language") or None
    return send(old_address, "email_changed", username=account.get("username", ""), lang=lang)[0]


def verification_required() -> bool:
    """Email verification is enforced whenever it is on and the portal can send email.

    It fails closed: a spent daily email budget does not switch it off
    (an account could spend the budget on purpose to skip verification);
    accounts then wait for the next day's budget to receive their link.
    While an administrator has paused all email nothing could arrive, so
    nobody is held back for an unverified address until email resumes.
    """
    cfg = current_app.config["HOSTING"]
    return (settings.flag("email_verification_required") and email.is_configured(cfg.email)
            and not settings.email_paused())


# Administrators: requests waiting for them


def attention_recipients() -> list[dict[str, Any]]:
    """Administrators with an address who did not opt out, plus the extra approval address."""
    people: list[dict[str, Any]] = []
    if settings.flag("approval_notify_admins"):
        rows = db.all(
            "SELECT id, username, email, language FROM accounts WHERE is_admin = 1 AND email != '' "
            "AND attention_emails = 1 AND suspended = 0 AND deleted_at IS NULL AND approval_status = 'approved' "
            "ORDER BY id"
        )
        people.extend({"key": f"account:{row['id']}", "email": row["email"], "username": row["username"],
                       "language": row["language"] or FALLBACK, "account": True} for row in rows)
    extra = (settings.get("approval_notify_email") or "").strip()
    if extra and extra.lower() not in {person["email"].lower() for person in people}:
        people.append({"key": f"address:{extra.lower()}", "email": extra, "username": "", "language": FALLBACK,
                       "account": False})
    return people


def attention_policy() -> engine.Policy:
    return engine.Policy(mode=settings.approval_notify_mode(),
                         interval_minutes=settings.approval_notify_interval_minutes(),
                         daily_hour=settings.approval_notify_daily_hour())


def attention_email(person: dict[str, Any], counts: dict[str, int], fresh: tuple[str, ...], mode: str) -> bool:
    lang = person["language"]
    labels = {source.id: source.label for source in attention.SOURCES}
    lines = []
    for source_id, count in counts.items():
        if count:
            new = f" ({t_lang(lang, 'email.admin_attention.new')})" if source_id in fresh and mode != "daily" else ""
            lines.append(f"• {t_lang(lang, labels[source_id])}: {count}{new}")
    kind = "admin_attention_daily" if mode == "daily" else "admin_attention"
    manage = (t_lang(lang, "email.admin_attention.manage", url=urls.external_url("account.account"))
              if person["account"] else t_lang(lang, "email.admin_attention.manage_address"))
    return send(person["email"], kind, username=person["username"], lang=lang, count=sum(counts.values()),
                lines="\n".join(lines), manage=manage, detail_value=str(sum(counts.values())),
                action_url=urls.external_url("admin.attention"))[0]


def send_attention(now: datetime | None = None) -> int:
    """Email administrators about waiting requests when the chosen mode says so (maintenance step).

    Only the queues switched on in ``attention_email_sources`` count; while
    email is paused nothing is looked at, so nothing is owed afterwards
    beyond what is still waiting then.
    """
    if not configured() or settings.email_paused():
        return 0
    emailed = [source.id for source in attention.SOURCES if attention_source_emailed(source.id)]
    people = attention_recipients() if emailed else []
    if not people:
        return 0
    now = now or datetime.now(UTC)
    policy = attention_policy()
    store = attention.store()
    if not store.poll_due(now, always=policy.mode == "daily"):
        return 0
    current = {source_id: count for source_id, count in attention.counts().items() if source_id in emailed}
    by_key = {person["key"]: person for person in people}
    return engine.run(store, policy, [(person["key"], current) for person in people],
                      lambda key, counts, fresh: attention_email(by_key[key], counts, fresh, policy.mode), now=now)


def send_test(account: dict[str, Any]) -> tuple[bool, str]:
    """The settings page's test button: a sample notification to the administrator's own address."""
    if not account.get("email"):
        return False, "no_address"
    if not configured():
        return False, "not_configured"
    return send(account["email"], "admin_test", username=account["username"],
                action_url=urls.external_url("admin.attention"))


# Owners: decisions on their own requests


def notice_text(notice: dict[str, Any], lang: str | None = None) -> str:
    """"Your request for public access on wiki1 was approved." in *lang* (default: the request's)."""
    def tr(key: str, default: str | None = None, /, **params: Any) -> str:
        return t_lang(lang, key, default, **params) if lang else t(key, default, **params)

    feature = tr(f"hosting.features.{notice['object_id']}", notice["object_id"]) \
        if notice["source_id"] == "features.requests" else ""
    generic = tr(f"hosting.notice.generic.{notice['outcome']}")
    return tr(f"hosting.notice.{notice['source_id']}.{notice['outcome']}", generic, feature=feature,
              wiki=notice.get("detail") or "")


def send_decisions() -> int:
    """Email each new decision notice once (best effort; maintenance step)."""
    if not configured():
        return 0
    rows = db.all(
        "SELECT n.*, a.username, a.email, a.language FROM hosting_account_notices n "
        "JOIN accounts a ON a.id = n.account_id WHERE n.emailed_at IS NULL AND n.created_at > ? "
        "AND a.deleted_at IS NULL ORDER BY n.id LIMIT 200", (sql_in(days=-DECISION_WINDOW_DAYS),),
    )
    sent = 0
    for row in rows:
        claimed = db.execute("UPDATE hosting_account_notices SET emailed_at = ? WHERE id = ? "
                             "AND emailed_at IS NULL", (now_sql(), row["id"])).rowcount
        if not claimed or not row["email"]:  # claimed by a run next to this one: it sends that one
            continue
        lang = row["language"] or FALLBACK
        ok, _reason = send(row["email"], "decision", username=row["username"], lang=lang,
                           notice=notice_text(row, lang), action_url=urls.external_url("dashboard.dashboard"))
        sent += int(ok)
    return sent


def send_verification(account: dict[str, Any], *, ignore_cooldown: bool = False) -> tuple[bool, str]:
    """Issue and email a fresh verification link. Returns ``(ok, message_key)``."""
    from . import accounts

    if not account.get("email"):
        return False, "hosting.verify.add_email_first"
    remaining = accounts.cooldown_remaining(account)
    if remaining and not ignore_cooldown:
        return False, "hosting.verify.wait"
    if not configured():
        return False, "hosting.email.not_configured"
    token = accounts.issue_verification(account)
    if not token:
        return False, "hosting.verify.send_failed"  # the address changed meanwhile: that change sends its own
    ok, reason = send(account["email"], "verify", username=account["username"], account_id=account["id"],
                      action_url=urls.external_url("auth.verify_email", token=token))
    if not ok:
        accounts.clear_verification(account["id"])
        if reason in ("daily_limit", "recipient_limit", "account_limit"):
            return False, "hosting.verify.limit_reached"
        if reason == "paused":
            return False, "hosting.verify.paused"
        return False, "hosting.verify.send_failed"
    return True, "hosting.verify.sent"
