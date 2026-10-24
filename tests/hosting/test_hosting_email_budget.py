"""Email budgets: shared by every worker, per account, with a reserve for account
recovery; verification never switches itself off; unverified addresses expire."""

from __future__ import annotations

from urllib.parse import urlsplit

from bananawiki.hosting import email
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ


def _portal(tmp_path, **environ):
    return build_portal(tmp_path, environ=portal_environ(tmp_path, **environ))


def _query(portal, sql, params=()):
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        if sql.lstrip().upper().startswith("SELECT"):
            return db.all(sql, params)
        db.execute(sql, params)
        return None


def _make_account(portal, username, **fields):
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        from bananawiki.hosting import accounts

        account = accounts.create(username, PASSWORD, email=fields.pop("email", ""))
        if fields:
            db.update("accounts", fields, "id = ?", (account["id"],))
        return accounts.get(account["id"])


def _login(portal, account, address="10.2.0.1"):
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = address
    assert client.post("/login", data={"username": account["username"], "password": PASSWORD}).status_code == 302
    return client


def _change_address(client, account, address):
    return client.post("/account", data={"username": account["username"], "email": address,
                                         "current_password": PASSWORD})


def test_verification_stays_required_when_the_daily_budget_is_used_up(tmp_path):
    portal = _portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="5")
    _query(portal, "UPDATE hosting_settings SET email_verification_required = 1 WHERE id = 1")
    user = _make_account(portal, "spender", email="start@example.org")
    client = _login(portal, user)
    with email.capture_outbox() as outbox:
        for number in range(8):
            _change_address(client, user, f"victim{number}@example.net")
        response = client.get("/dashboard")  # email is configured while the outbox captures it
    assert len(outbox) < 5, "the last messages of the day are kept for account recovery"
    assert response.status_code == 302 and "/account/verify-email" in response.headers["Location"]


def test_one_account_cannot_spend_the_whole_platform_budget(tmp_path):
    portal = _portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="300", HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT="3")
    _query(portal, "UPDATE hosting_settings SET email_verification_required = 1 WHERE id = 1")
    user = _make_account(portal, "spammer", email="start@example.org")
    client = _login(portal, user)
    with email.capture_outbox() as outbox:
        for number in range(6):
            _change_address(client, user, f"target{number}@example.net")
    assert len(outbox) == 3
    rows = _query(portal, "SELECT account_id, kind FROM hosting_email_log")
    assert rows == [{"account_id": user["id"], "kind": "verify"}] * 3


def test_password_resets_use_the_reserve_and_the_budget_is_shared_by_every_worker(tmp_path):
    portal = _portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="10")
    _make_account(portal, "owner", email="owner@example.org", email_verified_at="2026-01-01 00:00:00")
    # Another worker (or the process before a restart) already sent eight notices today.
    for number in range(8):
        _query(portal, "INSERT INTO hosting_email_log (sent_at, recipient, kind) VALUES (datetime('now'), ?, 'notice')",
               (f"someone{number}@example.org",))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import notifications

        with email.capture_outbox() as outbox:
            assert notifications.send("other@example.org", "admin_test")[1] == "daily_limit"
            assert outbox == []
    client = portal.test_client()
    with email.capture_outbox() as outbox:
        client.post("/forgot-password", data={"username": "owner", "email": "owner@example.org"})
    assert [message.to for message in outbox] == ["owner@example.org"]


def test_an_address_left_unverified_does_not_block_its_owner(tmp_path):
    portal = _portal(tmp_path)
    _query(portal, "UPDATE hosting_settings SET email_verification_required = 1, signup_email = 'optional' WHERE id = 1")
    _make_account(portal, "root")
    squatter = _make_account(portal, "squatter", email="ceo@victim.example")
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = "10.3.0.1"
    data = {"username": "ceo", "password": PASSWORD, "confirm_password": PASSWORD, "accept_terms": "1",
            "email": "ceo@victim.example"}
    with email.capture_outbox():
        assert client.post("/signup", data=data).status_code == 400, "a fresh claim still holds the address"
        _query(portal, "UPDATE accounts SET email_claimed_at = datetime('now', '-2 days') WHERE id = ?",
               (squatter["id"],))
        assert client.post("/signup", data=data).status_code == 302
    rows = _query(portal, "SELECT username, email FROM accounts WHERE username IN ('ceo', 'squatter') "
                          "ORDER BY username")
    assert rows == [{"username": "ceo", "email": "ceo@victim.example"}, {"username": "squatter", "email": ""}]


def test_a_verified_address_is_never_released(tmp_path):
    portal = _portal(tmp_path)
    _query(portal, "UPDATE hosting_settings SET email_verification_required = 1 WHERE id = 1")
    _make_account(portal, "holder", email="mine@example.org", email_verified_at="2026-01-01 00:00:00",
                  email_claimed_at="2020-01-01 00:00:00")
    other = _make_account(portal, "other")
    client = _login(portal, other)
    _change_address(client, other, "mine@example.org")
    assert _query(portal, "SELECT email FROM accounts WHERE username = 'holder'") == [{"email": "mine@example.org"}]
    assert _query(portal, "SELECT email FROM accounts WHERE username = 'other'") == [{"email": ""}]


# Holding an unverified address: no renewal by flipping it


def _verification_portal(tmp_path, **environ):
    portal = _portal(tmp_path, **environ)
    _query(portal, "UPDATE hosting_settings SET email_verification_required = 1, signup_email = 'optional' WHERE id = 1")
    _make_account(portal, "root")
    return portal


_CLAIM_COLUMNS = ("email_claimed_at", "email_claims_since", "email_verification_sent_at",
                  "email_verification_expires_at")


def _age(portal, account_id, hours):
    """Pretend *hours* passed for the account's address claim and verification link."""
    columns = {row["name"] for row in _query(portal, "SELECT name FROM pragma_table_info('accounts')")}
    for column in _CLAIM_COLUMNS:
        if column in columns:
            _query(portal, f"UPDATE accounts SET {column} = datetime({column}, ?) WHERE id = ? AND {column} IS NOT NULL",
                   (f"-{hours} hours", account_id))


def _sign_up(portal, username, address, network):
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = network
    data = {"username": username, "password": PASSWORD, "confirm_password": PASSWORD, "accept_terms": "1",
            "email": address}
    return client, client.post("/signup", data=data)


def _row(portal, username):
    return _query(portal, "SELECT * FROM accounts WHERE username = ?", (username,))[0]


def _open(client, link):
    parts = urlsplit(link)
    return client.get(f"{parts.path}?{parts.query}")


def test_flipping_an_address_away_and_back_does_not_renew_the_hold_on_it(tmp_path):
    portal = _verification_portal(tmp_path)
    squatter = _make_account(portal, "squatter")
    client = _login(portal, squatter)
    with email.capture_outbox():
        _change_address(client, squatter, "ceo@victim.example")
        # Every day, the address is changed and changed back: a change does not restart the claim.
        for _day in range(3):
            _age(portal, squatter["id"], 23)
            _change_address(client, squatter, "decoy@squatter.example")
            _change_address(client, squatter, "ceo@victim.example")
        _age(portal, squatter["id"], 1)
        _client, response = _sign_up(portal, "ceo", "ceo@victim.example", "10.4.0.1")
    assert response.status_code == 302, "the real owner gets the address"
    assert _row(portal, "ceo")["email"] == "ceo@victim.example" and _row(portal, "squatter")["email"] == ""


def test_a_typo_fixed_at_once_keeps_the_address_protected(tmp_path):
    portal = _verification_portal(tmp_path)
    holder = _make_account(portal, "holder")
    client = _login(portal, holder)
    with email.capture_outbox():
        _change_address(client, holder, "holdr@example.org")
        _change_address(client, holder, "holder@example.org")
        _age(portal, holder["id"], 2)
        _client, response = _sign_up(portal, "thief", "holder@example.org", "10.4.0.2")
    assert response.status_code == 400 and _row(portal, "holder")["email"] == "holder@example.org"


def test_a_link_sent_later_keeps_the_address_until_it_expires(tmp_path):
    portal = _verification_portal(tmp_path)
    with email.capture_outbox() as outbox:
        owner_client, created = _sign_up(portal, "owner", "owner@example.org", "10.4.0.3")
        assert created.status_code == 302
        owner = _row(portal, "owner")
        # The first link never arrived (the day's budget was spent, say); a new one is sent a day later.
        _age(portal, owner["id"], 30)
        owner_client.post("/account/resend-verification")
        link = outbox[-1].action_url
        _thief, response = _sign_up(portal, "thief", "owner@example.org", "10.4.0.4")
        assert response.status_code == 400, "a live link keeps the address"
        assert _open(owner_client, link).status_code == 302
    assert _row(portal, "owner")["email_verified_at"]


def test_a_live_link_does_not_keep_an_address_forever(tmp_path):
    portal = _verification_portal(tmp_path)
    squatter = _make_account(portal, "squatter")
    client = _login(portal, squatter)
    with email.capture_outbox():
        _change_address(client, squatter, "ceo@victim.example")
        _age(portal, squatter["id"], 47)
        client.post("/account/resend-verification")
        _age(portal, squatter["id"], 2)
        _client, response = _sign_up(portal, "ceo", "ceo@victim.example", "10.4.0.5")
    assert response.status_code == 302 and _row(portal, "squatter")["email"] == ""


def test_signing_up_fixing_a_typo_and_moving_once_keep_each_address_held(tmp_path):
    portal = _verification_portal(tmp_path)
    with email.capture_outbox() as outbox:
        client, _created = _sign_up(portal, "mover", "first@example.org", "10.4.0.6")
        mover = _row(portal, "mover")
        _change_address(client, mover, "second@example.org")
        _open(client, outbox[-1].action_url)
        assert _row(portal, "mover")["email_verified_at"]
        _age(portal, mover["id"], 5 * 24)
        _change_address(client, mover, "third@example.org")
        _thief, response = _sign_up(portal, "thief", "third@example.org", "10.4.0.7")
    assert response.status_code == 400, "the third address of the month is still held from when it was typed"


def test_confirming_an_address_of_ones_own_does_not_renew_the_hold_on_another(tmp_path):
    # Confirming an address does not end the series of changes, or a squatter could confirm one
    # of its own each day and take the victim's back, fresh.
    portal = _verification_portal(tmp_path)
    squatter = _make_account(portal, "squatter")
    client = _login(portal, squatter)
    with email.capture_outbox() as outbox:
        _change_address(client, squatter, "ceo@victim.example")
        for day in range(4):
            _age(portal, squatter["id"], 23)
            _query(portal, "DELETE FROM hosting_rate_limit_hits")
            _change_address(client, squatter, f"me{day}@squatter.example")
            _open(client, outbox[-1].action_url)
            assert _row(portal, "squatter")["email_verified_at"]
            _change_address(client, squatter, "ceo@victim.example")
        _age(portal, squatter["id"], 1)
        _client, response = _sign_up(portal, "ceo", "ceo@victim.example", "10.4.0.8")
    assert response.status_code == 302, "the real owner gets the address"
    assert _row(portal, "ceo")["email"] == "ceo@victim.example" and _row(portal, "squatter")["email"] == ""


# Account recovery: per network, per address, and only from its reserve


def _verified_accounts(portal, count, prefix="member"):
    return [_make_account(portal, f"{prefix}{number}", email=f"{prefix}{number}@example.org",
                          email_verified_at="2026-01-01 00:00:00") for number in range(count)]


def _next_minute(portal):
    """The per-minute form limits are not what these tests are about."""
    _query(portal, "DELETE FROM hosting_rate_limit_hits WHERE bucket LIKE 'hosting_auth_forgot_%'")


def test_one_network_cannot_ask_for_recovery_email_all_day(tmp_path):
    portal = _portal(tmp_path)
    people = _verified_accounts(portal, 12)
    client = portal.test_client()
    client.environ_base["REMOTE_ADDR"] = "10.5.0.1"
    statuses = []
    with email.capture_outbox() as outbox:
        for person in people:
            _next_minute(portal)
            statuses.append(client.post("/forgot-username", data={"email": person["email"]}).status_code)
    assert len(outbox) == email.RECOVERY_REQUESTS_PER_NETWORK
    assert statuses[-1] == 429
    page = client.post("/forgot-password", data={"username": "member0", "email": "member0@example.org"})
    assert page.status_code == 429 and "tomorrow" in page.get_data(as_text=True)
    other = portal.test_client()
    other.environ_base["REMOTE_ADDR"] = "10.5.0.2"
    with email.capture_outbox() as outbox:
        other.post("/forgot-password", data={"username": "member11", "email": "member11@example.org"})
    assert [message.to for message in outbox] == ["member11@example.org"], "other networks are not affected"


def test_one_address_gets_a_few_recovery_messages_of_each_kind_a_day(tmp_path):
    portal = _portal(tmp_path)
    (person,) = _verified_accounts(portal, 1)
    with email.capture_outbox() as outbox:
        for number in range(12):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"10.6.0.{number}"
            kind = "/forgot-username" if number % 2 else "/forgot-password"
            client.post(kind, data={"email": person["email"], "username": person["username"]})
    resets = ["/reset-password?token=" in (message.action_url or "") for message in outbox]
    assert resets.count(True) == resets.count(False) == email.RECOVERY_PER_RECIPIENT_DAILY_LIMIT


def test_user_name_reminders_cannot_block_ones_password_reset(tmp_path):
    # Anonymous user-name reminders do not use up the address's recovery messages for the day,
    # so its owner's own reset still arrives.
    portal = _portal(tmp_path)
    (victim,) = _verified_accounts(portal, 1)
    attacker = portal.test_client()
    attacker.environ_base["REMOTE_ADDR"] = "10.40.0.1"
    with email.capture_outbox() as outbox:
        for _ in range(email.RECOVERY_PER_RECIPIENT_DAILY_LIMIT + 2):
            _next_minute(portal)
            attacker.post("/forgot-username", data={"email": victim["email"]})
        reminders = len(outbox)
        owner = portal.test_client()
        owner.environ_base["REMOTE_ADDR"] = "10.41.0.1"
        owner.post("/forgot-password", data={"username": victim["username"], "email": victim["email"]})
    assert reminders == email.RECOVERY_PER_RECIPIENT_DAILY_LIMIT
    assert len(outbox) == reminders + 1 and "/reset-password?token=" in outbox[-1].action_url


def test_user_name_reminders_cannot_use_up_what_password_resets_need(tmp_path):
    # Knowing verified addresses is enough to ask for reminders from many networks; they may use
    # only part of the recovery share, so password resets keep going out.
    portal = _portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="30")
    people = _verified_accounts(portal, 11)
    with email.capture_outbox() as outbox:
        for number in range(30):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"10.8.{number}.1"
            client.post("/forgot-username", data={"email": people[number % 10]["email"]})
        reminders = len(outbox)
        owner = portal.test_client()
        owner.environ_base["REMOTE_ADDR"] = "10.8.200.1"
        owner.post("/forgot-password", data={"username": people[10]["username"], "email": people[10]["email"]})
    assert reminders == email.reminder_allowance(30) < email.recovery_reserve(30)
    assert len(outbox) == reminders + 1 and outbox[-1].to == people[10]["email"]


def test_a_spent_recovery_share_is_said_and_shown_to_administrators(tmp_path):
    # Once the day's recovery share is spent, nobody gets recovery mail: the forms say so (to
    # everyone alike, so nothing about accounts shows) instead of pretending a message is on its way.
    portal = _portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="30")
    people = _verified_accounts(portal, 3)
    with email.capture_outbox() as outbox:
        for number in range(email.recovery_reserve(30)):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"10.9.{number}.1"
            person = people[number % 3]
            client.post("/forgot-password", data={"username": person["username"], "email": person["email"]})
        assert len(outbox) == email.recovery_reserve(30)
        pages = []
        for number, (path, data) in enumerate([
                ("/forgot-password", {"username": "member0", "email": "member0@example.org"}),
                ("/forgot-password", {"username": "nobody", "email": "nobody@example.org"}),
                ("/forgot-username", {"email": "member1@example.org"}),
                ("/forgot-username", {"email": "nobody@example.org"})]):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"10.9.100.{number}"
            response = client.post(path, data=data)
            pages.append((response.status_code, "paused" in response.get_data(as_text=True)))
        assert len(outbox) == email.recovery_reserve(30)
        admin = _make_account(portal, "boss")
        _query(portal, "UPDATE accounts SET is_admin = 1 WHERE id = ?", (admin["id"],))
        page = _login(portal, admin, "10.9.200.1").get("/admin/attention", follow_redirects=True).get_data(as_text=True)
    assert pages == [(429, True)] * 4
    assert "Account recovery emails are paused" in page


def test_ipv6_recovery_requests_also_count_per_site(tmp_path):
    # Each /64 had its own 10 requests a day, and one /48 holds 65,536 of them.
    portal = _portal(tmp_path)
    people = _verified_accounts(portal, 20)
    statuses = []
    with email.capture_outbox() as outbox:
        for number in range(email.RECOVERY_REQUESTS_PER_IPV6_SITE + 5):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"2001:db8:0:{number:x}::1"
            person = people[number % 20]
            response = client.post("/forgot-password", data={"username": person["username"], "email": person["email"]})
            statuses.append(response.status_code)
    assert len(outbox) == email.RECOVERY_REQUESTS_PER_IPV6_SITE
    assert statuses[-1] == 429 and "Too many recovery requests" in response.get_data(as_text=True)
    other = portal.test_client()
    other.environ_base["REMOTE_ADDR"] = "2001:db8:1::1"
    with email.capture_outbox() as outbox:
        other.post("/forgot-password", data={"username": people[19]["username"], "email": people[19]["email"]})
    assert len(outbox) == 1, "another /48 is not affected"


def test_recovery_requests_cannot_spend_what_verification_needs(tmp_path):
    portal = _verification_portal(tmp_path, HOSTING_EMAIL_DAILY_LIMIT="30")
    people = _verified_accounts(portal, 10)
    with email.capture_outbox() as outbox:
        for number in range(30):
            client = portal.test_client()
            client.environ_base["REMOTE_ADDR"] = f"10.7.{number}.1"
            person = people[number % 10]
            path = "/forgot-username" if number % 2 else "/forgot-password"
            client.post(path, data={"email": person["email"], "username": person["username"]})
        recovery = len(outbox)
        _client, created = _sign_up(portal, "newcomer", "newcomer@example.org", "10.7.200.1")
    assert created.status_code == 302
    assert recovery == email.recovery_reserve(30)
    assert outbox[-1].to == "newcomer@example.org", "the newcomer still gets a verification link"
