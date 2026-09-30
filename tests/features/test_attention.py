"""Needs your attention: queue counts per role, the red dot, menus, the login banner, notices and settings."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.core import mail
from bananawiki.wiki import attention
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.attention import service
from bananawiki.wiki.features.contributions import quota as contribution_quota
from bananawiki.wiki.features.contributions import service as contributions
from bananawiki.wiki.features.page_governance import quota as reservation_quota
from bananawiki.wiki.features.pages import service as pages

from .governance_support import as_user, build_app, set_settings


@pytest.fixture
def app(app_factory):
    return build_app(app_factory, "contributions", "page_governance", "deletion_slowdown")


@pytest.fixture
def people(make_user, db):
    people = {"admin": make_user("boss", role="admin"), "reader": make_user("reader"),
              "editor": make_user("reviewer", role="editor"), "plain_editor": make_user("plain", role="editor")}
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (people["editor"]["id"],))
    for key in ("page.view_all", "page.edit_all", "contribution.review"):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                   (people["editor"]["id"], key))
    return people


@pytest.fixture
def page(app):
    with app.test_request_context(), connection_scope():
        return pages.create("Article", "secret body text\n", author_id=None)


def _counts(app, user):
    with app.test_request_context(), connection_scope():
        as_user(user)
        return attention.counts(user)


def _propose(app, page, user):
    with app.test_request_context(), connection_scope():
        as_user(user)
        return contributions.propose(pages.get(page["id"]), user, title=None, content="changed", reason="please")


# ── Counts ────────────────────────────────────────────────────────────────────


def test_counts_follow_roles_and_permissions(app, people, page, make_user):
    make_user("waiting", approval_status="pending")
    _propose(app, page, people["reader"])
    admin = _counts(app, people["admin"])
    assert admin["admin.signups"] == 1 and admin["contributions.reviews"] == 1
    reviewer = _counts(app, people["editor"])
    assert reviewer == {"contributions.reviews": 1}  # administrators' queues are never counted for reviewers
    assert _counts(app, people["plain_editor"]).get("contributions.reviews", 0) == 0
    assert _counts(app, people["reader"]) == {}


def test_every_known_queue_is_registered(app, people):
    with app.test_request_context(), connection_scope():
        ids = {source.id for source in attention.sources()}
    assert {"admin.signups", "page_governance.quota_requests", "contributions.reviews",
            "contributions.quota_requests", "deletion_slowdown.pending", "users.merge_requests",
            "plugin_manager.restarts"} <= ids


def test_disabled_feature_queue_disappears(app, people):
    set_settings(app, contribution_approval_enabled=0)
    with app.test_request_context(), connection_scope():
        assert "contributions.reviews" not in {source.id for source in attention.sources()}


def test_quota_requests_and_deletions_are_counted(app, people, page):
    with app.test_request_context(), connection_scope():
        reservation_quota.create_request(people["editor"]["id"], 20, "more pages")
        contribution_quota.create_request(people["reader"]["id"], 9, "more edits")
        from bananawiki.wiki.features.deletion_slowdown import service as deletions

        deletions.schedule(pages.get(page["id"]), people["admin"]["id"])
    counts = _counts(app, people["admin"])
    assert counts["page_governance.quota_requests"] == 1
    assert counts["contributions.quota_requests"] == 1
    assert counts["deletion_slowdown.pending"] == 1


def test_counts_are_cached_per_request(app, people, make_user, db):
    make_user("waiting", approval_status="pending")
    with app.test_request_context(), connection_scope():
        as_user(people["admin"])
        assert attention.total(people["admin"]) == 1
        db.execute("UPDATE users SET approval_status = 'approved' WHERE username = 'waiting'")
        assert attention.total(people["admin"]) == 1  # same request: cached
    with app.test_request_context(), connection_scope():
        assert attention.total(people["admin"]) == 0


# ── Events ────────────────────────────────────────────────────────────────────


def test_new_items_emit_attention_created(app, people, page, db):
    _propose(app, page, people["reader"])
    with app.test_request_context(), connection_scope():
        reservation_quota.create_request(people["editor"]["id"], 20, "more pages")
        set_settings(app, open_signup=1, approval_required=1)
    with app.test_request_context(), connection_scope():
        from bananawiki.wiki.features.auth import signup

        signup.register("newcomer", "a long password 1")
    sources = db.column("SELECT source_id FROM attention_events ORDER BY id")
    assert sources == ["contributions.reviews", "page_governance.quota_requests", "admin.signups"]


def test_automatic_quota_approval_is_not_a_request(app, people, db):
    set_settings(app, reservation_quota_auto_approve_max=50)
    with app.test_request_context(), connection_scope():
        row = reservation_quota.create_request(people["editor"]["id"], 20, "more pages")
    assert row["status"] == "approved"
    assert db.scalar("SELECT COUNT(*) FROM attention_events") == 0


# ── The red dot, the menu, the dashboard ─────────────────────────────────────


def test_avatar_dot_and_menu_section(app, client, login, people, make_user, page):
    make_user("waiting", approval_status="pending")
    _propose(app, page, people["reader"])
    login(client, people["admin"])
    html = client.get("/attention").data.decode()
    dot = re.search(r'<span class="attention-dot" data-attention-count="(\d+)">', html)
    assert dot and dot.group(1) == "2"
    assert 'aria-label="Account menu, 2 requests need your attention"' in html
    assert "Sign-ups waiting for approval" in html and "Proposed edits to review" in html
    assert 'href="/admin/users?approval=pending"' in html


def test_no_dot_without_pending_work(client, login, people):
    login(client, people["reader"])
    html = client.get("/attention").data.decode()
    assert "attention-dot" not in html
    assert "All caught up" in html


def test_reviewer_sees_only_reviews(app, client, login, people, page, make_user):
    make_user("waiting", approval_status="pending")
    _propose(app, page, people["reader"])
    login(client, people["editor"])
    html = client.get("/attention").data.decode()
    assert "Proposed edits to review" in html and "Sign-ups waiting" not in html
    assert 'data-attention-count="1"' in html


def test_admin_dashboard_lists_the_queues(admin_client, make_user):
    make_user("waiting", approval_status="pending")
    html = admin_client.get("/admin/dashboard").data.decode()
    assert 'id="attention-card-title"' in html and "Sign-ups waiting for approval" in html


def test_attention_page_shows_oldest_waiting(app, client, login, people, make_user, db):
    user = make_user("waiting", approval_status="pending")
    db.execute("UPDATE users SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (user["id"],))
    login(client, people["admin"])
    html = client.get("/attention").data.decode()
    assert "Oldest waiting since" in html and "years ago" in html


def test_menu_counts_are_in_italian(app, client, login, people, make_user, db):
    make_user("waiting", approval_status="pending")
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
               (json.dumps({"interface_language": "it"}), people["admin"]["id"]))
    login(client, people["admin"])
    html = client.get("/attention").data.decode()
    assert "Menu dell&#39;account, 1 richiesta richiede la tua attenzione" in html


# ── The banner after signing in ──────────────────────────────────────────────


def test_login_banner_is_shown_once(client, login, people, make_user):
    make_user("waiting", approval_status="pending")
    login(client, people["admin"])
    first = client.get("/attention").data.decode()
    assert "1 request needs your review." in first and "banner--attention" in first
    assert "banner--attention" not in client.get("/attention").data.decode()


def test_no_login_banner_when_nothing_waits(client, login, people):
    login(client, people["admin"])
    assert "banner--attention" not in client.get("/attention").data.decode()


# ── Decisions and notices ────────────────────────────────────────────────────


def test_decisions_leave_a_notice(app, client, login, people, page, db):
    contribution_id = _propose(app, page, people["reader"])
    with app.test_request_context(), connection_scope():
        as_user(people["admin"])
        contributions.deny(contribution_id, people["admin"], "no")
    notice = db.one("SELECT * FROM user_notices WHERE user_id = ?", (people["reader"]["id"],))
    assert notice["source_id"] == "contributions.reviews" and notice["outcome"] == "denied"
    login(client, people["reader"])
    html = client.get("/attention").data.decode()
    assert "Your proposed edit was not accepted." in html


def test_quota_and_signup_decisions(app, people, make_user, db):
    waiting = make_user("waiting", approval_status="pending")
    with app.test_request_context(), connection_scope():
        row = contribution_quota.create_request(people["reader"]["id"], 9, "more")
        contribution_quota.review(row["id"], people["admin"]["id"], approve=True)
        from bananawiki.wiki.features.admin import service as admin_service

        admin_service.approve(people["admin"], waiting)
    rows = db.all("SELECT user_id, source_id, outcome FROM user_notices ORDER BY id")
    assert rows == [
        {"user_id": people["reader"]["id"], "source_id": "contributions.quota_requests", "outcome": "approved"},
        {"user_id": waiting["id"], "source_id": "admin.signups", "outcome": "approved"},
    ]


def test_notices_are_dismissed_by_their_owner_only(app, client, login, people, db):
    with app.test_request_context(), connection_scope():
        service.on_decided(people["reader"]["id"], "contributions.reviews", "approved")
    notice_id = db.scalar("SELECT id FROM user_notices")
    login(client, people["editor"])
    assert client.post(f"/attention/notices/{notice_id}/dismiss").status_code == 404
    client.post("/logout")
    login(client, people["reader"])
    assert "Your proposed edit was approved" in client.get("/attention").data.decode()
    assert client.post(f"/attention/notices/{notice_id}/dismiss", data={"next": "/attention"}).status_code == 302
    assert db.scalar("SELECT dismissed_at FROM user_notices WHERE id = ?", (notice_id,))


def test_dismiss_needs_csrf(app, client, login, people, db):
    with app.test_request_context(), connection_scope():
        service.on_decided(people["reader"]["id"], "contributions.reviews", "approved")
    login(client, people["reader"])
    app.config["CSRF_DISABLED"] = False
    notice_id = db.scalar("SELECT id FROM user_notices")
    assert client.post(f"/attention/notices/{notice_id}/dismiss").status_code == 400
    assert db.scalar("SELECT dismissed_at FROM user_notices WHERE id = ?", (notice_id,)) is None


# ── The account's address and choices ───────────────────────────────────────


def test_account_settings_store_a_valid_address(client, login, people, db):
    login(client, people["editor"])
    assert b'id="notifications"' in client.get("/settings").data
    client.post("/settings/notifications", data={"email": " Rev@Example.ORG ", "attention_choice": "1",
                                                 "decision_emails": "1"})
    row = db.one("SELECT email, attention_emails, decision_emails FROM users WHERE id = ?", (people["editor"]["id"],))
    assert row == {"email": "Rev@example.org", "attention_emails": 0, "decision_emails": 1}


@pytest.mark.parametrize("address", ["not-an-address", "a@b.c\r\nBcc: victim@example.org", "x" * 250 + "@a.io"])
def test_account_settings_refuse_bad_addresses(client, login, people, db, address):
    login(client, people["reader"])
    client.post("/settings/notifications", data={"email": address})
    assert db.scalar("SELECT email FROM users WHERE id = ?", (people["reader"]["id"],)) is None


def test_hidden_choice_is_kept_for_non_reviewers(client, login, people, db):
    login(client, people["reader"])
    client.post("/settings/notifications", data={"email": "r@example.org"})
    row = db.one("SELECT attention_emails, decision_emails FROM users WHERE id = ?", (people["reader"]["id"],))
    assert row == {"attention_emails": 1, "decision_emails": 0}


def test_pending_account_can_leave_an_address(app, client, login, make_user, db):
    user = make_user("waiting", approval_status="pending")
    login(client, user)
    assert b"/account-status/notify-email" not in client.get("/account-status").data  # email off: not offered
    set_settings(app, decision_email_enabled=1, mail_provider="smtp", mail_from="wiki@example.org",
                 mail_smtp_host="smtp.example.org")
    assert b"/account-status/notify-email" in client.get("/account-status").data
    client.post("/account-status/notify-email", data={"email": "me@example.org"})
    assert db.scalar("SELECT email FROM users WHERE id = ?", (user["id"],)) == "me@example.org"


def test_only_pending_accounts_use_the_status_form(client, login, people):
    login(client, people["reader"])
    assert client.post("/account-status/notify-email", data={"email": "me@example.org"}).status_code == 403


def test_unsubscribe_link(app, client, people, db):
    with app.test_request_context(), connection_scope():
        token = service.unsubscribe_token(people["admin"]["id"], "attention")
    page = client.get(f"/attention/unsubscribe/{token}")
    assert page.status_code == 200 and b"Unsubscribe" in page.data
    assert db.scalar("SELECT attention_emails FROM users WHERE id = ?", (people["admin"]["id"],)) == 1
    client.post(f"/attention/unsubscribe/{token}")
    assert db.scalar("SELECT attention_emails FROM users WHERE id = ?", (people["admin"]["id"],)) == 0
    assert client.get("/attention/unsubscribe/forged-token").status_code == 404
    assert client.get(f"/attention/unsubscribe/{token[:-2]}xx").status_code == 404


# ── Administration ────────────────────────────────────────────────────────────


def test_admin_settings_need_an_administrator(client, login, people):
    login(client, people["editor"])
    assert client.get("/admin/notifications").status_code == 403
    assert client.post("/admin/notifications/test").status_code == 403


def test_admin_saves_notification_settings(admin_client, db):
    assert admin_client.get("/admin/notifications").status_code == 200
    admin_client.post("/admin/notifications", data={
        "section": "notifications", "attention_email_enabled": "1", "attention_email_mode": "daily",
        "attention_email_interval_minutes": "30", "attention_email_daily_hour": "7",
        "decision_email_enabled": "1", "public_base_url": "https://wiki.example.org/",
    })
    row = db.one("SELECT attention_email_enabled, attention_email_mode, attention_email_interval_minutes, "
                 "attention_email_daily_hour, decision_email_enabled, public_base_url FROM site_settings")
    assert row == {"attention_email_enabled": 1, "attention_email_mode": "daily",
                   "attention_email_interval_minutes": 30, "attention_email_daily_hour": 7,
                   "decision_email_enabled": 1, "public_base_url": "https://wiki.example.org"}


@pytest.mark.parametrize("field,value", [("attention_email_mode", "hourly"), ("attention_email_interval_minutes", "1"),
                                         ("public_base_url", "javascript:alert(1)")])
def test_admin_settings_validation(admin_client, db, field, value):
    data = {"section": "notifications", "attention_email_mode": "digest", "attention_email_interval_minutes": "60",
            "attention_email_daily_hour": "8", "attention_email_enabled": "1", field: value}
    admin_client.post("/admin/notifications", data=data)
    assert db.scalar("SELECT attention_email_enabled FROM site_settings") == 0


def test_mail_secrets_are_encrypted_and_kept(admin_client, db):
    form = {"section": "mail", "mail_provider": "smtp", "mail_from": "wiki@example.org", "mail_reply_to": "",
            "mail_smtp_host": "smtp.example.org", "mail_smtp_port": "587", "mail_smtp_security": "starttls",
            "mail_smtp_username": "wiki", "mail_smtp_password": "hunter2-secret"}
    admin_client.post("/admin/notifications", data=form)
    stored = db.scalar("SELECT mail_smtp_password FROM site_settings")
    assert stored.startswith("fernet:") and "hunter2" not in stored
    admin_client.post("/admin/notifications", data={**form, "mail_smtp_password": "", "mail_smtp_port": "2525"})
    assert db.scalar("SELECT mail_smtp_password FROM site_settings") == stored
    assert b"hunter2-secret" not in admin_client.get("/admin/notifications").data
    admin_client.post("/admin/notifications", data={**form, "mail_smtp_password": "", "clear_mail_smtp_password": "1"})
    assert db.scalar("SELECT mail_smtp_password FROM site_settings") == ""


def test_mail_settings_refuse_header_injection(admin_client, db):
    admin_client.post("/admin/notifications", data={
        "section": "mail", "mail_provider": "smtp", "mail_from": "wiki@example.org\r\nBcc: x@example.org",
        "mail_smtp_host": "smtp.example.org", "mail_smtp_port": "587", "mail_smtp_security": "starttls"})
    assert db.scalar("SELECT mail_from FROM site_settings") == ""


def test_environment_mail_wins_and_locks_the_form(app_factory, login, make_user):
    app = app_factory(environ={"BW_SMTP_HOST": "mail.host.example", "BW_MAIL_FROM": "Wiki <wiki@host.example>",
                               "BW_BASE_URL": "https://wiki.host.example"})
    client = app.test_client()
    admin = make_user("boss", role="admin")
    login(client, admin)
    with app.test_request_context(), connection_scope():
        assert service.mail_source() == "env" and service.mail_configured()
        assert service.base_url() == "https://wiki.host.example"
    html = client.get("/admin/notifications").data.decode()
    assert "configured by the host" in html and 'name="mail_smtp_password"' not in html
    assert client.post("/admin/notifications", data={"section": "mail", "mail_provider": "smtp"}).status_code == 403


def test_managed_hosting_without_mail_env_hides_the_server_form(app_factory, login, make_user):
    app = app_factory(environ={"BW_MANAGED_HOSTING": "1"})
    client = app.test_client()
    login(client, make_user("boss", role="admin"))
    with app.test_request_context(), connection_scope():
        assert service.mail_source() == "host_only" and service.mail_settings() is None
    assert "provided by the host" in client.get("/admin/notifications").data.decode()


def test_test_email_is_sent_and_rate_limited(admin_client, admin, db):
    db.execute("UPDATE users SET email = 'boss@example.org' WHERE id = ?", (admin["id"],))
    with mail.capture_outbox() as outbox:
        response = admin_client.post("/admin/notifications/test", follow_redirects=True)
        assert b"Test email sent to boss@example.org" in response.data
        for _ in range(4):
            admin_client.post("/admin/notifications/test")
        assert len(outbox) == 5
        response = admin_client.post("/admin/notifications/test", follow_redirects=True)
        assert b"Too many test emails" in response.data and len(outbox) == 5
    assert outbox[0].to == "boss@example.org" and "test email" in outbox[0].subject


def test_test_email_explains_missing_configuration(admin_client, admin, db):
    db.execute("UPDATE users SET email = 'boss@example.org' WHERE id = ?", (admin["id"],))
    response = admin_client.post("/admin/notifications/test", follow_redirects=True)
    assert b"The mail server is not configured." in response.data


def test_user_email_is_not_exposed_on_profiles(client, login, people, db):
    db.execute("UPDATE users SET email = 'hidden@example.org' WHERE id = ?", (people["editor"]["id"],))
    login(client, people["reader"])
    assert b"hidden@example.org" not in client.get(f"/users/{people['editor']['username']}").data


def test_translations_have_the_same_keys():
    folder = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/attention/translations"
    english = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((folder / "it.json").read_text(encoding="utf-8"))
    assert english.keys() == italian.keys()
    for key, text in english.items():
        assert set(re.findall(r"\{(\w+)\}", text)) == set(re.findall(r"\{(\w+)\}", italian[key])), key


def test_unsubscribe_works_signed_out_with_csrf(app, csrf_client, people, db):
    from tests.conftest import csrf_token_from

    with app.test_request_context(), connection_scope():
        token = service.unsubscribe_token(people["reader"]["id"], "decisions")
    page = csrf_client.get(f"/attention/unsubscribe/{token}")
    assert csrf_client.post(f"/attention/unsubscribe/{token}").status_code == 400
    response = csrf_client.post(f"/attention/unsubscribe/{token}", data={"csrf_token": csrf_token_from(page)})
    assert response.status_code == 200 and b"no longer receive" in response.data
    assert db.scalar("SELECT decision_emails FROM users WHERE id = ?", (people["reader"]["id"],)) == 0
