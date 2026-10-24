"""Settings → Email, the notifications switches and Settings → Sign-ups.

An address at sign-up and for existing accounts (required, optional, not
asked), one or several accounts per address, email verification on or off,
pausing every email, a switch per notice and per waiting-request queue, and
a sign-up mode that no longer undoes the approval box.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from bananawiki.hosting import email, notifications
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.migrations import v9_email_policy

from .hosting_support import PASSWORD

NOON = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


def _settings(query, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    query(f"UPDATE hosting_settings SET {assignments} WHERE id = 1", tuple(values.values()))


def _sign_up(portal, username, address="", ip="10.9.0.1"):
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = ip
    data = {"username": username, "password": PASSWORD, "confirm_password": PASSWORD, "accept_terms": "1"}
    if address is not None:
        data["email"] = address
    return client, client.post("/signup", data=data)


def _signed_in(portal, login, account):
    client = portal.test_client()
    login(client, account)
    return client


def _in(portal, call):
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        return call()


def _save(client, action, **fields):
    return client.post("/admin/settings", data={"action": action, **fields})


EMAIL_DEFAULTS = {"signup_email": "optional", "existing_email": "off", "email_shared": "0",
                  "email_verification_cooldown_seconds": "60"}


@pytest.fixture
def admin(make_account):
    return make_account("chief", admin=True, email="chief@example.org", email_verified_at="2026-01-01 00:00:00")


@pytest.fixture
def outbox():
    with email.capture_outbox() as messages:
        yield messages


# The address at sign-up


@pytest.mark.parametrize(("policy", "without", "field_shown"), [
    ("required", 400, True), ("optional", 302, True), ("off", 302, False)])
def test_the_sign_up_form_asks_for_an_address_as_chosen(portal, admin, query, policy, without, field_shown):
    _settings(query, signup_email=policy)
    page = portal.test_client().get("/signup").get_data(as_text=True)
    assert ('name="email"' in page) is field_shown
    assert _sign_up(portal, "noaddress", "", ip="10.9.1.1")[1].status_code == without
    _sign_up(portal, "withaddress", "given@example.org", ip="10.9.1.2")
    stored = query("SELECT email FROM accounts WHERE username = 'withaddress'", one=True)["email"]
    assert stored == ("" if policy == "off" else "given@example.org"), "a form that does not ask keeps nothing"


# Accounts without an address


def test_accounts_without_an_address_must_add_one_when_required(portal, query, make_account, login, admin):
    _settings(query, existing_email="required")
    member = make_account("member")
    client = _signed_in(portal, login, member)
    assert "/account/contact-email" in client.get("/dashboard").headers["Location"]
    page = client.get("/account/contact-email").get_data(as_text=True)
    assert 'value="skip"' not in page, "a required address cannot be skipped"
    client.post("/account/contact-email", data={"action": "skip"})
    assert "/account/contact-email" in client.get("/dashboard").headers["Location"]
    client.post("/account/contact-email", data={"email": "member@example.org", "current_password": PASSWORD})
    assert client.get("/dashboard").status_code == 200


def test_administrators_always_reach_the_setting_that_asks_for_addresses(portal, query, make_account, login):
    _settings(query, existing_email="required")
    chief = make_account("addressless", admin=True)
    client = _signed_in(portal, login, chief)
    assert client.get("/admin").status_code == 302, "the rest of the portal waits for an address"
    assert client.get("/admin/settings").status_code == 200
    assert _save(client, "save_email", **{**EMAIL_DEFAULTS, "existing_email": "off"}).status_code == 302
    assert client.get("/admin").status_code == 200


def test_an_optional_address_is_asked_once_and_may_be_skipped(portal, query, make_account, login, admin):
    _settings(query, existing_email="optional")
    member = make_account("member")
    client = portal.test_client()
    response = login(client, member)
    assert "/account/contact-email" in response.headers["Location"]
    assert client.get("/dashboard").status_code == 200, "never a gate"
    assert 'value="skip"' in client.get("/account/contact-email").get_data(as_text=True)
    assert "/dashboard" in client.post("/account/contact-email", data={"action": "skip"}).headers["Location"]
    again = login(portal.test_client(), member)
    assert "/account/contact-email" not in again.headers["Location"], "asked once"


def test_an_account_that_has_an_address_keeps_one_when_sign_up_requires_it(portal, query, make_account, login):
    _settings(query, signup_email="required")
    member = make_account("member", email="member@example.org")
    client = _signed_in(portal, login, member)
    client.post("/account", data={"username": "member", "email": "", "current_password": PASSWORD})
    assert query("SELECT email FROM accounts WHERE username = 'member'", one=True)["email"] == "member@example.org"
    loose = make_account("loose")
    _signed_in(portal, login, loose).post("/account", data={"username": "renamed", "email": "",
                                                            "current_password": PASSWORD})
    assert query("SELECT username FROM accounts WHERE id = ?", (loose["id"],), one=True)["username"] == "renamed"


# One or several accounts per address


def test_one_address_per_account_unless_allowed(portal, query, make_account, login, admin):
    _settings(query, signup_email="optional")
    make_account("first", email="Same@Example.org")
    assert _sign_up(portal, "second", "same@example.ORG", ip="10.9.2.1")[1].status_code == 400
    other = make_account("other")
    _signed_in(portal, login, other).post("/account", data={"username": "other", "email": "SAME@example.org",
                                                            "current_password": PASSWORD})
    assert query("SELECT email FROM accounts WHERE id = ?", (other["id"],), one=True)["email"] == ""
    _settings(query, email_shared=1)
    assert _sign_up(portal, "second", "same@example.org", ip="10.9.2.2")[1].status_code == 302
    rows = query("SELECT COUNT(*) AS n FROM accounts WHERE email = 'same@example.org' COLLATE NOCASE", one=True)
    assert rows["n"] == 2


def test_addresses_shared_before_the_switch_keep_working(portal, query, make_account, login, admin):
    _settings(query, email_shared=1, signup_email="optional")
    first = make_account("first", email="team@example.org")
    make_account("second", email="team@example.org")
    _settings(query, email_shared=0)
    page = _signed_in(portal, login, admin).get("/admin/settings").get_data(as_text=True)
    assert "1 address is used by more than one account" in page
    client = _signed_in(portal, login, first)
    client.post("/account", data={"username": "first-renamed", "email": "team@example.org",
                                  "current_password": PASSWORD})
    assert query("SELECT username FROM accounts WHERE id = ?", (first["id"],), one=True)["username"] == "first-renamed"
    assert _sign_up(portal, "third", "team@example.org", ip="10.9.3.1")[1].status_code == 400


def test_two_sign_ups_racing_for_one_address_never_both_get_it(portal, admin, query):
    _settings(query, signup_email="required")
    results = []

    def attempt(number):
        results.append(_sign_up(portal, f"racer{number}", "race@example.org", ip=f"10.9.4.{number}")[1].status_code)

    threads = [threading.Thread(target=attempt, args=(number,)) for number in range(1, 7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    holders = query("SELECT COUNT(*) AS n FROM accounts WHERE email = 'race@example.org'", one=True)["n"]
    assert holders == 1 and sorted(results).count(302) == 1


def test_a_username_reminder_names_every_account_on_a_shared_address(portal, query, make_account, outbox):
    _settings(query, email_shared=1)
    verified = "2026-01-01 00:00:00"
    make_account("alex", email="family@example.org", email_verified_at=verified)
    make_account("alexbanana", email="family@example.org", email_verified_at=verified)
    make_account("unconfirmed", email="family@example.org")
    client = portal.test_client()
    client.post("/forgot-username", data={"email": "Family@example.org"})
    assert len(outbox) == 1 and outbox[0].to == "family@example.org"
    assert outbox[0].detail_value == "alex, alexbanana"
    client.post("/forgot-username", data={"email": "nobody@example.org"})
    assert len(outbox) == 1, "an unknown address gets nothing, and the page says the same"


# Verification


def test_with_verification_off_nobody_waits_for_a_confirmed_address(portal, query, make_account, login, outbox):
    member = make_account("member", email="member@example.org")
    _settings(query, email_verification_required=1)
    client = _signed_in(portal, login, member)
    assert "/account/verify-email" in client.get("/dashboard").headers["Location"]
    _settings(query, email_verification_required=0)
    assert client.get("/dashboard").status_code == 200


# Pausing every email


def test_pausing_stops_every_message_and_counts_them(portal, query, make_account, login, admin, outbox):
    client = _signed_in(portal, login, admin)
    _save(client, "save_email", **{**EMAIL_DEFAULTS, "email_verification_required": "1", "email_paused": "1"})
    assert query("SELECT email_paused FROM hosting_settings", one=True)["email_paused"] == 1
    page = client.get("/admin").get_data(as_text=True)
    assert "All email is paused" in page
    flashed = client.post("/admin/notifications/test", follow_redirects=True).get_data(as_text=True)
    assert "All email is paused, so nothing was sent." in flashed
    # Sign-up with an address while verification is on: nothing to confirm while nothing can arrive.
    newcomer, response = _sign_up(portal, "newcomer", "new@example.org", ip="10.9.5.1")
    assert response.status_code == 302 and newcomer.get("/dashboard").status_code == 200
    make_account("owner", email="owner@example.org", email_verified_at="2026-01-01 00:00:00")
    recovery = portal.test_client().post("/forgot-password", data={"username": "owner", "email": "owner@example.org"})
    assert "Email is paused" in recovery.get_data(as_text=True)
    make_account("waiting", approval_status="pending")
    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate")
    assert _in(portal, lambda: notifications.send_attention(NOON)) == 0
    assert _in(portal, lambda: notifications.send("x@example.org", "instance_created", slug="w", url="u")) == (
        False, "paused")
    assert outbox == []
    held = query("SELECT email_paused_held FROM hosting_settings", one=True)["email_paused_held"]
    assert held >= 2
    assert f"{held} messages not sent" in client.get("/admin/settings").get_data(as_text=True)
    _save(client, "save_email", **{**EMAIL_DEFAULTS, "email_verification_required": "1"})
    assert "/account/verify-email" in newcomer.get("/dashboard").headers["Location"], "verification is back"
    assert _in(portal, lambda: notifications.send_attention(NOON)) == 1


# Notifications to account holders


def test_every_notice_has_its_texts_and_label(portal):
    from bananawiki.hosting.i18n import catalog

    for lang in ("en", "it", "de"):
        with portal.app_context():
            strings = catalog().strings(lang)
        for notice in notifications.NOTICES:
            assert f"email.{notice.kind}.subject" in strings and f"email.{notice.kind}.body" in strings, notice
            assert f"hosting.notices.{notice.kind}" in strings
        for group in notifications.NOTICE_GROUPS:
            assert f"hosting.notices.group.{group}" in strings


def test_a_notice_switched_off_is_not_sent(portal, make_account, make_wiki, login, admin, query, outbox):
    client = _signed_in(portal, login, admin)
    kept_on = {f"notice_{notice.kind}": "1" for notice in notifications.NOTICES if notice.kind != "instance_created"}
    _save(client, "save_notices", **kept_on)
    switches = json.loads(query("SELECT email_notices FROM hosting_settings", one=True)["email_notices"])
    assert switches["instance_created"] == 0 and switches["instance_suspended"] == 1
    owner = make_account("owner", email="owner@example.org")
    created = _signed_in(portal, login, owner).post("/instances/create", data={
        "subdomain": "quiet", "declared_use_case": "Notes for our reading group.", "compliance_declared": "1"})
    assert created.status_code == 200 and query("SELECT 1 AS made FROM instances", one=True)
    assert outbox == [], "the wiki was created, the notice stayed off"
    assert _in(portal, lambda: notifications.send("o@example.org", "instance_created")) == (False, "disabled")
    assert _in(portal, lambda: notifications.send("o@example.org", "instance_suspended", slug="w"))[0]
    assert _in(portal, lambda: notifications.send("o@example.org", "verify", action_url="x"))[0], "always sent"


def test_new_notices_reach_the_right_people(portal, make_account, make_wiki, login, admin, query, outbox):
    owner = make_account("owner", email="owner@example.org", email_verified_at="2026-01-01 00:00:00")
    helper = make_account("helper", email="helper@example.org")
    wiki = make_wiki(owner, "shared")
    client = _signed_in(portal, login, owner)
    client.post(f"/instances/{wiki['id']}/collaborators/add", data={"username": "helper", "role": "full_access"})
    assert any(message.to == "helper@example.org" and "shared" in message.subject for message in outbox)
    client.post("/account/change-password", data={"current_password": PASSWORD, "new_password": "another long pass 9",
                                                  "confirm_new_password": "another long pass 9"})
    assert outbox[-1].to == "owner@example.org" and "password" in outbox[-1].subject.lower()
    client.post("/account", data={"username": "owner", "email": "moved@example.org",
                                  "current_password": "another long pass 9"})
    assert outbox[-1].to == "owner@example.org" and "address" in outbox[-1].subject.lower()
    admin_client = _signed_in(portal, login, admin)
    admin_client.post(f"/admin/instances/{wiki['id']}/suspend", data={"suspend_duration": "permanent"})
    admin_client.post(f"/admin/instances/{wiki['id']}/unsuspend")
    assert outbox[-1].to == "moved@example.org" and "back online" in outbox[-1].subject
    assert helper  # the collaborator was told above


# Administrators: waiting requests, queue by queue


def test_attention_emails_cover_only_the_queues_switched_on(portal, make_account, query, admin, login, outbox):
    _settings(query, approval_notify_admins=1, approval_notify_mode="immediate")
    client = _signed_in(portal, login, admin)
    _save(client, "save_notifications", approval_notify_admins="1", approval_notify_mode="immediate",
          approval_notify_interval_minutes="360", approval_notify_daily_hour="8",
          **{"attention_source_features.requests": "1", "attention_source_merges.awaiting": "1"})
    make_account("waiting", approval_status="pending")
    assert _in(portal, lambda: notifications.send_attention(NOON)) == 0, "sign-ups are not emailed about"
    _save(client, "save_notifications", approval_notify_admins="1", approval_notify_mode="immediate",
          approval_notify_interval_minutes="360", approval_notify_daily_hour="8",
          **{"attention_source_accounts.pending": "1"})
    # Switched on again, the queue is in the next catch-up poll (the maintenance service runs often).
    assert _in(portal, lambda: notifications.send_attention(NOON + timedelta(hours=1))) == 1
    assert "Accounts waiting for approval: 1" in outbox[-1].text


# Sign-ups: the mode and the approval box are one control


def test_the_sign_up_mode_no_longer_undoes_the_approval_box(portal, query, make_account, login, admin):
    client = _signed_in(portal, login, admin)
    pending = make_account("pending-one", approval_status="pending")
    _save(client, "save_signups", signup_mode="invite", hosting_activation_required="1")
    assert query("SELECT signup_mode, hosting_activation_required FROM hosting_settings", one=True) == {
        "signup_mode": "invite", "hosting_activation_required": 1}
    client.post("/admin/signup-mode", data={"signup_mode": "open"})
    row = query("SELECT signup_mode, hosting_activation_required FROM hosting_settings", one=True)
    assert row == {"signup_mode": "open", "hosting_activation_required": 1}, "the box stays as it was"
    assert query("SELECT approval_status FROM accounts WHERE id = ?", (pending["id"],), one=True)[
        "approval_status"] == "pending"
    page = client.get("/admin/settings").get_data(as_text=True)
    assert 'name="hosting_activation_required" value="1" checked' in page


@pytest.mark.parametrize(("choice", "status"), [("keep", "pending"), ("approve", "approved")])
def test_ending_approval_asks_what_happens_to_waiting_accounts(portal, query, make_account, login, admin,
                                                               choice, status):
    _settings(query, signup_mode="approval", hosting_activation_required=1)
    waiting = make_account("waiting", approval_status="pending")
    client = _signed_in(portal, login, admin)
    form = {"signup_mode": "open", "act_timeout_days": "1", "hosting_activation_denied_deletion_enabled": "1"}
    asked = _save(client, "save_signups", **form)
    assert asked.status_code == 200 and "1 account is waiting for approval" in asked.get_data(as_text=True)
    assert query("SELECT signup_mode FROM hosting_settings", one=True)["signup_mode"] == "approval", "nothing yet"
    _save(client, "save_signups", **form, pending_accounts=choice)
    assert query("SELECT signup_mode FROM hosting_settings", one=True)["signup_mode"] == "open"
    assert query("SELECT approval_status FROM accounts WHERE id = ?", (waiting["id"],), one=True)[
        "approval_status"] == status


def test_settings_changes_are_logged_by_name(portal, query, login, admin):
    client = _signed_in(portal, login, admin)
    _save(client, "save_easter_eggs", easter_shades="1", easter_peek="1")
    entry = query("SELECT actor_id, reason FROM hosting_events WHERE action = 'settings.changed'", one=True)
    assert entry == {"actor_id": admin["id"], "reason": "easter_konami"}


def test_misplaced_switches_moved_to_their_sections(portal, login, admin):
    page = _signed_in(portal, login, admin).get("/admin/settings").get_data(as_text=True)

    def section(action):
        start = page.index(f'value="{action}"')
        return page[start:page.index("</form>", start)]

    assert 'name="email_flag_block_reentry"' in section("save_email")
    assert 'name="owner_renewal_enabled"' in section("save_lifetime")
    assert 'name="forbid_non_admin_public_wikis"' in section("save_wiki_features")
    assert 'name="auto_approve_page_builder_requests"' in section("save_wiki_features")
    assert 'name="signup_mode"' in section("save_signups")


# Upgrading


def _old_database(**flags):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hosting_settings (id INTEGER PRIMARY KEY, ask_email_new_signup INTEGER, "
                 "ask_email_existing_users INTEGER, email_required INTEGER)")
    conn.execute("INSERT INTO hosting_settings VALUES (1, :new, :existing, :required)", flags)
    conn.execute("CREATE TABLE accounts (id TEXT, email TEXT, deleted_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX ix_accounts_active_email ON accounts(email COLLATE NOCASE) "
                 "WHERE email <> '' AND deleted_at IS NULL")
    return conn


@pytest.mark.parametrize(("flags", "expected"), [
    ({"new": 0, "existing": 0, "required": 0}, ("off", "off")),
    ({"new": 1, "existing": 0, "required": 0}, ("optional", "off")),
    ({"new": 1, "existing": 1, "required": 1}, ("required", "required")),
    ({"new": 1, "existing": 1, "required": 0}, ("optional", "optional")),
])
def test_upgrading_keeps_the_email_choices_and_lets_addresses_be_shared(flags, expected):
    conn = _old_database(**flags)
    v9_email_policy.upgrade(conn)
    row = conn.execute("SELECT signup_email, existing_email, email_shared, easter_konami FROM hosting_settings").fetchone()
    assert row == (*expected, 0, 1)
    indexes = {name: unique for _seq, name, unique, *_rest in conn.execute("PRAGMA index_list('accounts')")}
    assert indexes == {"ix_accounts_email": 0}


def test_operators_can_pause_email_from_the_command_line(portal, query, capsys):
    from bananawiki.hosting import admin as cli

    assert cli.main(["settings", "email-pause", "on"], app=portal) == 0
    assert query("SELECT email_paused FROM hosting_settings", one=True)["email_paused"] == 1
    assert cli.main(["settings", "email-pause", "off"], app=portal) == 0
    assert query("SELECT email_paused FROM hosting_settings", one=True)["email_paused"] == 0
    assert "Email is sent again." in capsys.readouterr().out
