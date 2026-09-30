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
``hosting_attention_recipients``). Owners get one email per decision on
their own requests (``hosting_account_notices``).
"""

from __future__ import annotations

import logging
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


def configured() -> bool:
    return email.is_configured(current_app.config["HOSTING"].email)


def send(to: str, kind: str, *, username: str = "", action_url: str = "", detail_value: str = "",
         lang: str | None = None, **values: Any) -> tuple[bool, str]:
    """Send one notice built from ``email.<kind>.*`` in *lang* (default: the request's language)."""
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
        eyebrow=tr(f"email.{kind}.eyebrow", tr("email.eyebrow"), **context),
        action_url=action_url,
        action_label=tr(f"email.{kind}.action", "", **context) if action_url else "",
        detail_label=tr(f"email.{kind}.detail", "", **context) if detail_value else "",
        detail_value=detail_value,
    )
    try:
        ok, reason = email.send(cfg, message, tr("email.footer", contact=cfg.contact_email))
    except Exception:  # noqa: BLE001 - a notice must never break the action that triggered it
        log.exception("Email %s to %s failed", kind, to)
        return False, "failed"
    if not ok:
        log.warning("Email %s to %s not sent: %s", kind, to, reason)
    return ok, reason


def notify_account(account: dict[str, Any] | None, kind: str, **values: Any) -> bool:
    """Send an account notice to the account's contact address, if it has one."""
    if not account or not account.get("email") or not configured():
        return False
    return send(account["email"], kind, username=account.get("username", ""), **values)[0]


def verification_required() -> bool:
    """Email verification is enforced only while email can actually be delivered."""
    cfg = current_app.config["HOSTING"]
    return (settings.flag("email_verification_required") and email.is_configured(cfg.email)
            and not email.quota_exhausted(cfg.email))


# ── Administrators: requests waiting for them ────────────────────────────────


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
    """Email administrators about waiting requests when the chosen mode says so (maintenance step)."""
    if not configured():
        return 0
    people = attention_recipients()
    if not people:
        return 0
    now = now or datetime.now(UTC)
    policy = attention_policy()
    store = attention.store()
    if not store.poll_due(now, always=policy.mode == "daily"):
        return 0
    current = attention.counts()
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


# ── Owners: decisions on their own requests ──────────────────────────────────


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
        db.execute("UPDATE hosting_account_notices SET emailed_at = ? WHERE id = ? AND emailed_at IS NULL",
                   (now_sql(), row["id"]))
        if not row["email"]:
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
    ok, _reason = send(account["email"], "verify", username=account["username"],
                       action_url=urls.external_url("auth.verify_email", token=token))
    if not ok:
        accounts.clear_verification(account["id"])
        return False, "hosting.verify.send_failed"
    return True, "hosting.verify.sent"
