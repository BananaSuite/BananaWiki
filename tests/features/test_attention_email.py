"""The attention.notify job: modes, throttling, idempotency, opt-out, languages and decision emails."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bananawiki.core import mail
from bananawiki.wiki import attention
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.attention import mailer
from bananawiki.wiki.features.contributions import quota as contribution_quota
from bananawiki.wiki.features.contributions import service as contributions
from bananawiki.wiki.features.pages import service as pages

from .governance_support import as_user, build_app, set_settings

NOON = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def app(app_factory):
    app = build_app(app_factory, "contributions")
    set_settings(app, attention_email_enabled=1, attention_email_mode="immediate", decision_email_enabled=1,
                 public_base_url="https://wiki.example.org")
    return app


@pytest.fixture
def admin(make_user, db):
    user = make_user("boss", role="admin")
    db.execute("UPDATE users SET email = 'boss@example.org' WHERE id = ?", (user["id"],))
    return user


@pytest.fixture
def outbox():
    with mail.capture_outbox() as messages:
        yield messages


def run(app, now=NOON):
    with app.test_request_context(), connection_scope():
        return mailer.notify_reviewers(now)


def pending(make_user, name):
    return make_user(name, approval_status="pending")


def announce(app, source="admin.signups", object_id="1"):
    with app.test_request_context(), connection_scope():
        attention.created(source, object_id)


# ── Immediate mode ────────────────────────────────────────────────────────────


def test_first_request_sends_one_email_and_runs_are_idempotent(app, admin, make_user, outbox):
    pending(make_user, "waiting")
    assert run(app) == 1
    assert run(app, NOON + timedelta(minutes=10)) == 0
    message = outbox[0]
    assert message.to == "boss@example.org"
    assert "requests waiting for your review (1)" in message.subject
    assert "Sign-ups waiting for approval: 1 (new)" in message.text
    assert message.action_url == "https://wiki.example.org/attention"
    assert message.unsubscribe_url.startswith("https://wiki.example.org/attention/unsubscribe/")


def test_each_new_request_triggers_one_more_email(app, admin, make_user, outbox):
    pending(make_user, "first")
    run(app)
    pending(make_user, "second")
    announce(app)
    assert run(app, NOON + timedelta(minutes=1)) == 1
    assert "Sign-ups waiting for approval: 2 (new)" in outbox[-1].text
    assert run(app, NOON + timedelta(minutes=2)) == 0


def test_event_catches_a_replacement_the_count_would_miss(app, admin, make_user, db, outbox):
    first = pending(make_user, "first")
    run(app)
    db.execute("UPDATE users SET approval_status = 'approved' WHERE id = ?", (first["id"],))
    pending(make_user, "second")
    assert run(app, NOON + timedelta(minutes=1)) == 0  # same count, no event: nothing new is visible
    announce(app)
    assert run(app, NOON + timedelta(minutes=2)) == 1


def test_polling_catches_up_without_events(app, admin, make_user, outbox):
    pending(make_user, "first")
    run(app)
    pending(make_user, "second")  # created without an attention.created event
    assert run(app, NOON + timedelta(minutes=1)) == 0  # no event: the next catch-up poll finds it
    assert run(app, NOON + timedelta(minutes=10)) == 1


def test_nothing_is_sent_when_disabled_or_unconfigured(app, admin, make_user):
    pending(make_user, "waiting")
    set_settings(app, attention_email_enabled=0)
    with mail.capture_outbox() as outbox:
        assert run(app) == 0
    assert outbox == []
    set_settings(app, attention_email_enabled=1)
    assert run(app) == 0  # no outbox and no mail server: not configured


def test_opt_out_and_missing_address(app, admin, make_user, db, outbox):
    pending(make_user, "waiting")
    db.execute("UPDATE users SET attention_emails = 0 WHERE id = ?", (admin["id"],))
    assert run(app) == 0
    other = make_user("second_admin", role="admin")  # no address
    assert other and run(app) == 0 and outbox == []


def test_suspended_administrators_get_nothing(app, admin, make_user, db, outbox):
    pending(make_user, "waiting")
    db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (admin["id"],))
    assert run(app) == 0


# ── Digest and daily modes ────────────────────────────────────────────────────


def test_digest_waits_for_the_interval(app, admin, make_user, outbox):
    set_settings(app, attention_email_mode="digest", attention_email_interval_minutes=60)
    pending(make_user, "first")
    assert run(app) == 1
    pending(make_user, "second")
    announce(app)
    assert run(app, NOON + timedelta(minutes=30)) == 0
    assert run(app, NOON + timedelta(minutes=45)) == 0
    assert run(app, NOON + timedelta(minutes=61)) == 1
    assert "Sign-ups waiting for approval: 2 (new)" in outbox[-1].text
    assert run(app, NOON + timedelta(minutes=200)) == 0


def test_daily_summary_once_a_day_after_the_hour(app, admin, make_user, outbox):
    set_settings(app, attention_email_mode="daily", attention_email_daily_hour=9, timezone="UTC")
    pending(make_user, "waiting")
    assert run(app, NOON.replace(hour=8)) == 0
    assert run(app, NOON.replace(hour=9, minute=1)) == 1
    assert "daily summary" in outbox[-1].subject
    assert run(app, NOON.replace(hour=18)) == 0
    assert run(app, NOON.replace(hour=9) + timedelta(days=1)) == 1


def test_daily_summary_skips_empty_days(app, admin, outbox):
    set_settings(app, attention_email_mode="daily", attention_email_daily_hour=0)
    assert run(app) == 0 and outbox == []


def test_failed_delivery_is_retried_later_not_in_a_loop(app, admin, make_user, monkeypatch):
    pending(make_user, "waiting")
    set_settings(app, mail_provider="smtp", mail_from="wiki@example.org", mail_smtp_host="smtp.invalid")
    calls = []
    monkeypatch.setattr(mail, "_smtp", lambda *args: calls.append(args) or (False, "smtp_failed"))
    assert run(app) == 0 and len(calls) == 1
    assert run(app, NOON + timedelta(minutes=5)) == 0 and len(calls) == 1
    monkeypatch.setattr(mail, "_smtp", lambda *args: calls.append(args) or (True, ""))
    assert run(app, NOON + timedelta(minutes=16)) == 1 and len(calls) == 2


# ── Who hears about what, and in which language ─────────────────────────────


def test_reviewers_hear_only_about_reviews(app, admin, make_user, db, outbox):
    editor = make_user("reviewer", role="editor")
    db.execute("UPDATE users SET email = 'rev@example.org' WHERE id = ?", (editor["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (editor["id"],))
    for key in ("page.view_all", "page.edit_all", "contribution.review"):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (editor["id"], key))
    reader = make_user("reader")
    pending(make_user, "waiting")
    with app.test_request_context(), connection_scope():
        page = pages.create("Secret plans", "top secret body\n", author_id=None)
        as_user(reader)
        contributions.propose(page, reader, title=None, content="leaked words", reason="typo")
    run(app)
    by_address = {message.to: message for message in outbox}
    assert "Proposed edits to review: 1" in by_address["rev@example.org"].text
    assert "Sign-ups" not in by_address["rev@example.org"].text
    for message in outbox:  # counts and links only, never page content
        assert "leaked words" not in message.text and "top secret" not in message.text
        assert "Secret plans" not in message.text


def test_email_uses_the_recipients_language(app, admin, make_user, db, outbox):
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
               (json.dumps({"interface_language": "it"}), admin["id"]))
    pending(make_user, "waiting")
    run(app)
    assert "richieste in attesa della tua revisione (1)" in outbox[0].subject
    assert "Iscrizioni in attesa di approvazione: 1 (nuove)" in outbox[0].text


def test_no_links_without_a_public_address(app, admin, make_user, outbox):
    set_settings(app, public_base_url="")
    pending(make_user, "waiting")
    run(app)
    assert outbox[0].action_url == "" and outbox[0].unsubscribe_url == ""


# ── Decisions ─────────────────────────────────────────────────────────────────


def decide_quota(app, user, approve=True):
    with app.test_request_context(), connection_scope():
        row = contribution_quota.create_request(user["id"], 9, "more")
        contribution_quota.review(row["id"], user["id"], approve=approve)


def send_decisions(app):
    with app.test_request_context(), connection_scope():
        return mailer.send_decisions()


def test_decision_emails_are_sent_once(app, make_user, db, outbox):
    user = make_user("asker")
    db.execute("UPDATE users SET email = 'asker@example.org' WHERE id = ?", (user["id"],))
    decide_quota(app, user)
    assert send_decisions(app) == 1
    assert send_decisions(app) == 0
    assert outbox[0].to == "asker@example.org"
    assert "larger contribution quota was approved" in outbox[0].text
    assert outbox[0].action_url == "https://wiki.example.org/my-contributions"


def test_decision_emails_respect_choices(app, make_user, db, outbox):
    silent = make_user("silent")
    db.execute("UPDATE users SET email = 'silent@example.org', decision_emails = 0 WHERE id = ?", (silent["id"],))
    decide_quota(app, silent)
    no_address = make_user("no_address")
    decide_quota(app, no_address, approve=False)
    assert send_decisions(app) == 0 and outbox == []
    assert db.scalar("SELECT COUNT(*) FROM user_notices WHERE emailed_at IS NULL") == 0


def test_decision_emails_off_by_default(app, make_user, db, outbox):
    set_settings(app, decision_email_enabled=0)
    user = make_user("asker")
    db.execute("UPDATE users SET email = 'asker@example.org' WHERE id = ?", (user["id"],))
    decide_quota(app, user)
    assert send_decisions(app) == 0
    assert db.scalar("SELECT COUNT(*) FROM user_notices WHERE emailed_at IS NULL") == 1


def test_the_job_runs_everything(app, admin, make_user, outbox):
    pending(make_user, "waiting")
    with app.test_request_context(), connection_scope():
        mailer.run()
    assert len(outbox) == 1


def test_scheduler_runs_the_job(app, admin, make_user, outbox):
    pending(make_user, "waiting")
    scheduler = app.extensions.get("bananawiki.scheduler")
    if scheduler is None:
        from bananawiki.wiki.registry import Scheduler

        scheduler = Scheduler(app)
    assert "attention.notify" in scheduler.run_due(force=True, only="attention.notify")
    assert len(outbox) == 1
