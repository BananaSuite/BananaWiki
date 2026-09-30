"""Portal "needs your attention": queues, red dot, login banner, owner notices and administrator emails."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bananawiki.hosting import attention, email, notifications
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, csrf_token

NOON = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
REASON = "Our documentation should be open to everyone."


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


def _client(portal, login, account):
    client = portal.test_client()
    login(client, account)
    return client


@pytest.fixture
def admin(make_account):
    return make_account("chief", admin=True, email="chief@example.org")


@pytest.fixture
def outbox():
    with email.capture_outbox() as messages:
        yield messages


def run(portal, now=NOON):
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        return notifications.send_attention(now)


def send_decisions(portal):
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        return notifications.send_decisions()


def request_feature(portal, login, owner, wiki):
    client = _client(portal, login, owner)
    client.post(f"/instances/{wiki['id']}/features/public_access/requests", data={"reason": REASON})
    return client


# ── Queues and the red dot ───────────────────────────────────────────────────


def test_counts_cover_every_queue(portal, ctx, make_account, make_wiki, query, admin, login):
    make_account(approval_status="pending")
    owner = make_account()
    request_feature(portal, login, owner, make_wiki(owner))
    source, target = make_account(), make_account()
    query("INSERT INTO hosting_account_merge_requests (source_account_id, target_account_id, requested_by, status, "
          "source_approved, target_approved) VALUES (?, ?, ?, 'approved', 1, 1)",
          (source["id"], target["id"], source["id"]))
    assert attention.counts() == {"accounts.pending": 1, "features.requests": 1, "merges.awaiting": 1}
    assert attention.total(admin) == 3
    assert attention.items(owner) == []


def test_admin_link_shows_the_red_dot(portal, make_account, login, admin):
    make_account(approval_status="pending")
    html = _client(portal, login, admin).get("/dashboard").get_data(as_text=True)
    assert 'data-attention-count="1"' in html
    assert 'aria-label="Administration, 1 request needs your attention"' in html


def test_no_dot_for_owners_or_empty_queues(portal, make_account, login, admin):
    assert "attention-dot" not in _client(portal, login, admin).get("/dashboard").get_data(as_text=True)
    make_account(approval_status="pending")
    assert "attention-dot" not in _client(portal, login, make_account()).get("/dashboard").get_data(as_text=True)


def test_login_banner_once_for_administrators(portal, make_account, login, admin):
    make_account(approval_status="pending")
    client = _client(portal, login, admin)
    first = client.get("/dashboard").get_data(as_text=True)
    assert "1 request needs your review." in first
    assert "needs your review" not in client.get("/dashboard").get_data(as_text=True)


def test_attention_page_and_dashboard_card(portal, make_account, login, admin, query):
    user = make_account(approval_status="pending")
    query("UPDATE accounts SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (user["id"],))
    client = _client(portal, login, admin)
    page = client.get("/admin/attention").get_data(as_text=True)
    assert "Accounts waiting for approval" in page and "Oldest waiting since" in page
    assert 'href="/admin#pending-accounts"' in page
    dashboard = client.get("/admin").get_data(as_text=True)
    assert 'id="attention-card-title"' in dashboard and 'id="pending-accounts"' in dashboard


def test_attention_page_is_for_administrators(portal, make_account, login):
    response = _client(portal, login, make_account()).get("/admin/attention")
    assert response.status_code in (302, 403, 404)


def test_italian_labels(portal, make_account, login, admin):
    make_account(approval_status="pending")
    client = _client(portal, login, admin)
    client.post("/language", data={"language": "it"})
    assert "Account in attesa di approvazione" in client.get("/admin/attention").get_data(as_text=True)


# ── Events and owner notices ─────────────────────────────────────────────────


def test_new_items_are_recorded_as_events(portal, ctx, make_account, make_wiki, login, query):
    from bananawiki.hosting import accounts

    _settings(query, signup_mode="approval", hosting_activation_required=1)
    make_account("root", admin=True)
    accounts.signup("newcomer", "correct horse 42")
    owner = make_account()
    request_feature(portal, login, owner, make_wiki(owner))
    rows = query("SELECT source_id FROM hosting_attention_events ORDER BY id")
    assert [row["source_id"] for row in rows] == ["accounts.pending", "features.requests"]


def test_feature_decision_leaves_a_notice(portal, make_account, make_wiki, login, query, admin):
    owner = make_account()
    wiki = make_wiki(owner, "docs")
    owner_client = request_feature(portal, login, owner, wiki)
    request = query("SELECT id FROM instance_feature_requests", one=True)
    _client(portal, login, admin).post(f"/admin/feature-requests/{request['id']}/deny", data={"review_note": "no"})
    notice = query("SELECT * FROM hosting_account_notices", one=True)
    assert (notice["account_id"], notice["outcome"], notice["detail"]) == (owner["id"], "denied", "docs")
    html = owner_client.get("/dashboard").get_data(as_text=True)
    assert "Your request for Public access on docs was denied." in html


def test_notices_are_dismissed_by_their_owner_only(portal, make_account, login, query, ctx):
    owner, other = make_account(), make_account()
    attention.decided(owner["id"], "accounts.pending", "approved")
    notice_id = query("SELECT id FROM hosting_account_notices", one=True)["id"]
    assert _client(portal, login, other).post(f"/notices/{notice_id}/dismiss").status_code == 404
    response = _client(portal, login, owner).post(f"/notices/{notice_id}/dismiss", data={"next": "/dashboard"})
    assert response.status_code == 302
    assert query("SELECT dismissed_at FROM hosting_account_notices", one=True)["dismissed_at"]


def test_dismiss_needs_csrf(tmp_path):
    portal = build_portal(tmp_path / "csrf", csrf=True)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts

        owner = accounts.create("owner", PASSWORD)
        attention.decided(owner["id"], "accounts.pending", "approved")
    client = portal.test_client()
    client.post("/login", data={"username": "owner", "password": PASSWORD, "csrf_token": csrf_token(client)})

    def dismissed():
        with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
            from bananawiki.hosting.db import db

            return db.scalar("SELECT dismissed_at FROM hosting_account_notices WHERE id = 1")

    client.post("/notices/1/dismiss")
    assert dismissed() is None
    client.post("/notices/1/dismiss", data={"csrf_token": csrf_token(client, "/dashboard")})
    assert dismissed() is not None


def test_merge_decisions(portal, ctx, make_account, admin):
    from bananawiki.hosting import merges

    source, target = make_account(), make_account()
    merge = merges.request(source, target["username"], "same person")
    merges.approve(merge["id"], target)
    assert attention.counts()["merges.awaiting"] == 1
    merges.execute(merge["id"], admin)
    from bananawiki.hosting.db import db

    assert db.all("SELECT account_id, outcome FROM hosting_account_notices") == [
        {"account_id": target["id"], "outcome": "approved"}]
    assert db.column("SELECT source_id FROM hosting_attention_events") == ["merges.awaiting"]


# ── Administrator emails ─────────────────────────────────────────────────────


def test_admin_emails_follow_the_settings(portal, make_account, query, admin, outbox):
    make_account(approval_status="pending")
    assert run(portal) == 0  # not enabled yet
    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate")
    assert run(portal) == 1
    assert run(portal, NOON + timedelta(minutes=10)) == 0
    message = outbox[0]
    assert message.to == "chief@example.org" and "(1)" in message.subject
    assert "Accounts waiting for approval: 1 (new)" in message.text
    assert message.action_url.endswith("/admin/attention")


def test_extra_address_and_per_admin_opt_out(portal, make_account, query, admin, login, outbox):
    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate",
              approval_notify_email="team@example.org")
    _client(portal, login, admin).post("/account/notifications", data={})
    assert query("SELECT attention_emails FROM accounts WHERE id = ?", (admin["id"],), one=True)["attention_emails"] == 0
    make_account(approval_status="pending")
    run(portal)
    assert [message.to for message in outbox] == ["team@example.org"]
    assert "platform's notification settings" in outbox[0].text


def test_digest_interval(portal, make_account, query, admin, outbox, ctx):
    _settings(query, approval_notify_admins=1, approval_notify_mode="digest", approval_notify_interval_minutes=60)
    make_account(approval_status="pending")
    assert run(portal) == 1
    second = make_account(approval_status="pending")
    attention.created("accounts.pending", second["id"])
    assert run(portal, NOON + timedelta(minutes=20)) == 0
    assert run(portal, NOON + timedelta(minutes=61)) == 1
    assert "Accounts waiting for approval: 2 (new)" in outbox[-1].text


def test_daily_summary(portal, make_account, query, admin, outbox):
    _settings(query, approval_notify_admins=1, approval_notify_mode="daily", approval_notify_daily_hour=9)
    make_account(approval_status="pending")
    assert run(portal, NOON.replace(hour=8)) == 0
    assert run(portal, NOON.replace(hour=10)) == 1
    assert run(portal, NOON.replace(hour=20)) == 0
    assert run(portal, NOON.replace(hour=10) + timedelta(days=1)) == 1
    assert "Daily summary" in outbox[0].subject


def test_admin_email_language(portal, make_account, query, admin, login, outbox):
    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate")
    client = _client(portal, login, admin)
    client.post("/language", data={"language": "it"})
    make_account(approval_status="pending")
    run(portal)
    assert "Richieste in attesa della tua revisione" in outbox[0].subject


def test_maintenance_runs_the_email_steps(portal, make_account, query, admin, outbox):
    from bananawiki.hosting import maintenance

    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate")
    make_account(approval_status="pending")
    results = maintenance.run_once(portal)
    assert results["attention emails"] == 1 and results["decision emails"] == 0
    assert len(outbox) == 1


def test_decision_emails_are_sent_once_in_the_owners_language(portal, make_account, make_wiki, login, query,
                                                              admin, outbox):
    owner = make_account(email="owner@example.org")
    owner_client = request_feature(portal, login, owner, make_wiki(owner, "docs"))
    owner_client.post("/language", data={"language": "it"})
    request = query("SELECT id FROM instance_feature_requests", one=True)
    _client(portal, login, admin).post(f"/admin/feature-requests/{request['id']}/approve", data={"review_note": ""})
    assert send_decisions(portal) == 1
    assert send_decisions(portal) == 0
    assert outbox[0].to == "owner@example.org"
    assert "Accesso pubblico" in outbox[0].text and "docs" in outbox[0].text


def test_account_approval_notice_is_not_emailed_twice(portal, make_account, login, query, admin, outbox):
    waiting = make_account(approval_status="pending", email="new@example.org")
    _client(portal, login, admin).post(f"/admin/accounts/{waiting['id']}/approve", data={"decision_reason": ""})
    assert len(outbox) == 1  # the existing approval email
    assert send_decisions(portal) == 0


# ── Settings and the test button ─────────────────────────────────────────────


def test_notification_settings_are_saved_and_validated(portal, login, query, admin):
    client = _client(portal, login, admin)
    client.post("/admin/settings", data={"action": "save_notifications", "approval_notify_admins": "1",
                                         "approval_notify_mode": "daily", "approval_notify_interval_minutes": "90",
                                         "approval_notify_daily_hour": "7", "approval_notify_email": ""})
    row = query("SELECT approval_notify_admins, approval_notify_mode, approval_notify_interval_minutes, "
                "approval_notify_daily_hour FROM hosting_settings", one=True)
    assert row == {"approval_notify_admins": 1, "approval_notify_mode": "daily",
                   "approval_notify_interval_minutes": 90, "approval_notify_daily_hour": 7}
    client.post("/admin/settings", data={"action": "save_notifications", "approval_notify_mode": "hourly"})
    client.post("/admin/settings", data={"action": "save_notifications", "approval_notify_mode": "digest",
                                         "approval_notify_email": "team@example.org\r\nBcc: x@example.org"})
    assert query("SELECT approval_notify_mode FROM hosting_settings", one=True)["approval_notify_mode"] == "daily"


def test_test_email_button_is_rate_limited(portal, login, admin, outbox):
    client = _client(portal, login, admin)
    for _ in range(5):
        assert client.post("/admin/notifications/test").status_code == 302
    assert client.post("/admin/notifications/test").status_code == 429
    assert len(outbox) == 5 and outbox[0].to == "chief@example.org"


def test_test_email_is_for_administrators(portal, make_account, login, outbox):
    response = _client(portal, login, make_account(email="u@example.org")).post("/admin/notifications/test")
    assert response.status_code in (302, 403, 404) and outbox == []


def test_digest_hours_become_minutes_on_upgrade(tmp_path):
    import sqlite3

    from bananawiki.hosting.migrations import v3_takeover

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE hosting_settings (id INTEGER PRIMARY KEY, approval_notify_digest_hours INTEGER)")
    conn.execute("INSERT INTO hosting_settings VALUES (1, 3)")
    v3_takeover.add_attention(conn)
    v3_takeover.add_attention(conn)  # idempotent
    assert conn.execute("SELECT approval_notify_interval_minutes FROM hosting_settings").fetchone()[0] == 180


def test_translations_have_the_same_keys():
    import json
    from pathlib import Path

    folder = Path(__file__).resolve().parents[2] / "bananawiki/hosting/translations"
    english = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((folder / "it.json").read_text(encoding="utf-8"))
    assert english.keys() == italian.keys()
