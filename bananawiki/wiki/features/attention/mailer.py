"""The ``attention.notify`` job: emails about waiting requests and about decisions.

Nothing here runs inside a web request. Each run

1. tells administrators and reviewers about their queues, following the
   chosen mode (see :mod:`bananawiki.core.attention` for immediate / digest /
   daily and the throttling state), and
2. emails people whose own request was decided (the notices created by
   ``attention.decided``), once per notice.

Emails carry counts and links only, never page content, in the recipient's
own interface language, with an unsubscribe link.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from flask import current_app, url_for

from ....core import attention as engine
from ....core import mail
from ....core.i18n import valid_code
from ....core.timeutil import now_sql, sql_in
from ... import attention, i18n, settings
from ...db import db
from . import service

log = logging.getLogger("bananawiki.attention.mail")

DECISION_WINDOW_DAYS = 2
DECISION_BATCH = 200


def language_of(user: dict[str, Any]) -> str:
    try:
        prefs = json.loads(user.get("accessibility") or "{}")
    except (TypeError, ValueError):
        prefs = {}
    code = valid_code(prefs.get("interface_language")) if isinstance(prefs, dict) else ""
    return code if code and code in i18n.enabled_languages() else i18n.default_language()


@contextmanager
def link_context() -> Iterator[bool]:
    """A request context whose ``url_for(..., _external=True)`` points at the public address.

    Yields whether the address is known; without it emails carry no links.
    """
    base = service.base_url()
    with current_app.test_request_context("/", base_url=base or "http://localhost"):
        yield bool(base)


def _external(endpoint: str, links: bool, **values: Any) -> str:
    if not links:
        return ""
    try:
        return url_for(endpoint, _external=True, **values)
    except Exception:  # noqa: BLE001 - an endpoint of a feature that is gone
        return url_for("attention.index", _external=True)


def _message(user: dict[str, Any], lang: str, *, subject: str, text: str, links: bool, action_endpoint: str,
             action_label: str, kind: str, detail_label: str = "", detail_value: str = "",
             **action_values: Any) -> mail.Message:
    site = settings.site_name()
    unsubscribe = _external("attention.unsubscribe", links, token=service.unsubscribe_token(user["id"], kind))
    footer = i18n.t_lang(lang, "attention.email.footer", site=site)
    if unsubscribe:
        text = f"{text}\n\n{i18n.t_lang(lang, 'attention.email.unsubscribe')} {unsubscribe}"
    return mail.Message(
        to=user["email"], subject=subject, text=text, title=subject, eyebrow=site,
        action_url=_external(action_endpoint, links, **action_values), action_label=action_label,
        detail_label=detail_label, detail_value=detail_value, footer=footer, brand=site,
        unsubscribe_url=unsubscribe,
    )


# ── Waiting requests ──────────────────────────────────────────────────────────


def candidates() -> list[dict[str, Any]]:
    """Accounts that may receive queue emails: an address, not opted out, able to review something."""
    return db.all(
        "SELECT * FROM users WHERE email IS NOT NULL AND email != '' AND attention_emails = 1 AND suspended = 0 "
        "AND COALESCE(approval_status, 'approved') = 'approved' AND role IN ('editor', 'admin', 'owner') "
        "ORDER BY id"
    )


def attention_message(user: dict[str, Any], counts: dict[str, int], fresh: tuple[str, ...], *, mode: str,
                      links: bool) -> mail.Message:
    lang = language_of(user)
    labels = {source.id: source.label for source in attention.sources()}
    total = sum(counts.values())
    site = settings.site_name()
    lines = []
    for source_id, count in counts.items():
        if not count:
            continue
        line = f"• {i18n.t_lang(lang, labels.get(source_id, source_id))}: {count}"
        if source_id in fresh and mode != "daily":
            line += f" ({i18n.t_lang(lang, 'attention.email.new')})"
        lines.append(line)
    intro = i18n.t_lang(lang, "attention.email.intro_daily" if mode == "daily" else "attention.email.intro",
                        username=user["username"], site=site)
    subject_key = "attention.email.subject_daily" if mode == "daily" else "attention.email.subject"
    return _message(
        user, lang, subject=i18n.t_lang(lang, subject_key, count=total, site=site),
        text=intro + "\n\n" + "\n".join(lines), links=links, action_endpoint="attention.index",
        action_label=i18n.t_lang(lang, "attention.email.action"), kind="attention",
        detail_label=i18n.t_lang(lang, "attention.email.detail"), detail_value=str(total),
    )


def notify_reviewers(now: datetime | None = None) -> int:
    """Send the queue emails that are due; returns how many went out."""
    if not settings.get("attention_email_enabled"):
        return 0
    config = service.mail_settings()
    if not mail.is_configured(config):
        return 0
    now = now or datetime.now(UTC)
    policy = service.policy()
    store = service.store()
    if not store.poll_due(now, always=policy.mode == "daily"):
        return 0
    with link_context() as links:
        people: dict[str, dict[str, Any]] = {}
        recipients = []
        for user in candidates():
            counts = attention.counts(user)
            if counts:
                people[user["id"]] = user
                recipients.append((user["id"], counts))

        def deliver(user_id: str, counts: dict[str, int], fresh: tuple[str, ...]) -> bool:
            message = attention_message(people[user_id], counts, fresh, mode=policy.mode, links=links)
            ok, reason = mail.send(config, message)
            if not ok:
                log.warning("Attention email to user %s not sent: %s", user_id, reason)
            return ok

        return engine.run(store, policy, recipients, deliver, now=now)


# ── Decisions ─────────────────────────────────────────────────────────────────


def decision_message(user: dict[str, Any], notice: dict[str, Any], *, links: bool) -> mail.Message:
    lang = language_of(user)
    site = settings.site_name()
    text = service.notice_text(notice, lang)
    return _message(
        user, lang, subject=i18n.t_lang(lang, "attention.email.decision_subject", site=site),
        text=i18n.t_lang(lang, "attention.email.decision_intro", username=user["username"], site=site) + "\n\n" + text,
        links=links, action_endpoint=service.notice_url_endpoint(notice),
        action_label=i18n.t_lang(lang, "attention.email.decision_action"), kind="decisions",
    )


def send_decisions() -> int:
    """Email each new notice once (best effort: a failed delivery is logged, not retried)."""
    if not settings.get("decision_email_enabled"):
        return 0
    config = service.mail_settings()
    if not mail.is_configured(config):
        return 0
    rows = db.all(
        "SELECT n.*, u.username, u.email, u.decision_emails, u.accessibility FROM user_notices n "
        "JOIN users u ON u.id = n.user_id WHERE n.emailed_at IS NULL AND n.created_at > ? ORDER BY n.id LIMIT ?",
        (sql_in(days=-DECISION_WINDOW_DAYS), DECISION_BATCH),
    )
    sent = 0
    with link_context() as links:
        for row in rows:
            db.execute("UPDATE user_notices SET emailed_at = ? WHERE id = ? AND emailed_at IS NULL",
                       (now_sql(), row["id"]))
            if not row["email"] or not row["decision_emails"]:
                continue
            ok, reason = mail.send(config, decision_message(row | {"id": row["user_id"]}, row, links=links))
            if ok:
                sent += 1
            else:
                log.warning("Decision email for notice %s not sent: %s", row["id"], reason)
    return sent


def send_test(user: dict[str, Any]) -> tuple[bool, str]:
    """Send a test message to *user*'s own address, now (the administrator is waiting for the answer)."""
    config = service.mail_settings()
    if not mail.is_configured(config):
        return False, "not_configured"
    if not user.get("email"):
        return False, "no_address"
    lang = language_of(user)
    site = settings.site_name()
    with link_context() as links:
        message = _message(
            user, lang, subject=i18n.t_lang(lang, "attention.email.test_subject", site=site),
            text=i18n.t_lang(lang, "attention.email.test_body", site=site), links=links,
            action_endpoint="attention.admin_settings", action_label=i18n.t_lang(lang, "attention.email.action"),
            kind="attention",
        )
    return mail.send(config, message)


def run() -> None:
    """The job."""
    notify_reviewers()
    send_decisions()
    service.store().prune()
    service.prune_notices()
