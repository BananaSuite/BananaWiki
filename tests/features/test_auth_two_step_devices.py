"""Two-step code limits cannot be multiplied with device cookies (second audit of the 1.7 round).

A device cookie gives a browser its own budget of sign-in failures and of
wrong codes. Codes are only worth a budget of their own when the cookie was
earned with the current two-step set-up: turning two-step verification on,
off or resetting it retires every device cookie of the account (the browser
making the change gets a fresh one). On top of the per-browser budgets, an
account accepts a fixed number of wrong codes a day from all browsers
together, and every browser waits longer and longer between wrong codes.
"""

from __future__ import annotations

from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import devices, signin

from .test_auth_two_step import DEVICE_COOKIE, _next_code, enrol, password_step, signed_in

WRONG = {"code": "000000"}


def _cookie(client) -> str:
    cookie = client.get_cookie(DEVICE_COOKIE)
    assert cookie is not None
    return cookie.value


def _age_wrong_codes(db, seconds: int) -> None:
    """Move every recorded wrong code *seconds* into the past."""
    db.execute("UPDATE rate_limit_hits SET hit_at = datetime(hit_at, ?) WHERE bucket LIKE 'login:2fa%'",
               (f"-{seconds} seconds",))


def test_device_cookies_minted_before_two_step_was_on_share_the_accounts_code_budget(app, make_user, login):
    user = make_user("bob")
    jar = []
    for _ in range(3):
        attacker = app.test_client()
        login(attacker, user)
        jar.append(_cookie(attacker))
    owner = app.test_client()
    login(owner, user)
    enrol(owner)

    browser = app.test_client()
    assert password_step(browser, user).headers["Location"].endswith("/login/two-step")
    statuses = []
    for cookie in jar:
        browser.set_cookie(DEVICE_COOKIE, cookie)
        statuses += [browser.post("/login/two-step", data=WRONG).status_code for _ in range(signin.TWO_STEP_MAX)]
    assert statuses.count(401) == signin.TWO_STEP_MAX, "one budget, however many old cookies the browser presents"
    assert set(statuses[signin.TWO_STEP_MAX:]) == {429}


def test_turning_two_step_on_keeps_the_browser_that_did_it_a_known_device(app, make_user, login):
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    before = _cookie(owner)
    _secret, codes = enrol(owner)
    assert _cookie(owner) != before, "the browser that turned it on gets a cookie for the new set-up"
    owner.post("/logout")

    stranger = app.test_client()
    password_step(stranger, user)
    for _ in range(signin.TWO_STEP_MAX):
        assert stranger.post("/login/two-step", data=WRONG).status_code == 401
    assert stranger.post("/login/two-step", data={"code": codes[0]}).status_code == 429

    password_step(owner, user)
    assert owner.post("/login/two-step", data={"code": codes[0]}).status_code == 302
    assert signed_in(owner)


def test_turning_two_step_off_and_on_again_retires_the_cookies_of_the_first_set_up(app, make_user, login):
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    secret, codes = enrol(owner)
    other = app.test_client()
    password_step(other, user)
    assert other.post("/login/two-step", data={"code": _next_code(secret)}).status_code == 302
    earned = _cookie(other)

    assert owner.post("/settings/two-step", data={"action": "disable", "password": PASSWORD,
                                                  "code": codes[0]}).status_code == 302
    enrol(owner)
    owner.post("/logout")
    _use_up_the_shared_budget(app, user)

    browser = app.test_client()
    browser.set_cookie(DEVICE_COOKIE, earned)
    password_step(browser, user)
    assert browser.post("/login/two-step", data=WRONG).status_code == 429, \
        "a cookie earned with the previous set-up has no budget of its own"


def _use_up_the_shared_budget(app, user) -> None:
    for _ in range(signin.TWO_STEP_MAX):
        browser = app.test_client()
        password_step(browser, user)
        browser.post("/login/two-step", data=WRONG)


def test_an_owners_reset_retires_the_accounts_device_cookies(app, db, make_user, login):
    member = make_user("bob")
    member_browser = app.test_client()
    login(member_browser, member)
    enrol(member_browser)
    earned = _cookie(member_browser)
    boss = app.test_client()
    login(boss, make_user("boss", role="owner"))
    assert boss.post(f"/admin/users/{member['id']}/two-step/reset").status_code == 302
    assert not db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (member["id"],))
    with app.test_request_context(headers={"Cookie": f"{DEVICE_COOKIE}={earned}"}), connection_scope():
        assert devices.nonce(member) is None

    again = app.test_client()
    login(again, member)
    enrol(again)
    again.post("/logout")
    _use_up_the_shared_budget(app, member)
    browser = app.test_client()
    browser.set_cookie(DEVICE_COOKIE, earned)
    password_step(browser, member)
    assert browser.post("/login/two-step", data=WRONG).status_code == 429


def test_the_account_accepts_a_fixed_number_of_wrong_codes_a_day_from_all_browsers(app, make_user, login,
                                                                                    monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_MAX_PER_ACCOUNT_DAY", 3)
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    secret, codes = enrol(first)
    second = app.test_client()
    password_step(second, user)
    assert second.post("/login/two-step", data={"code": _next_code(secret)}).status_code == 302
    for browser in (first, second):
        browser.post("/logout")

    password_step(first, user)
    assert [first.post("/login/two-step", data=WRONG).status_code for _ in range(2)] == [401, 401]
    password_step(second, user)
    assert second.post("/login/two-step", data=WRONG).status_code == 401
    assert second.post("/login/two-step", data={"code": codes[0]}).status_code == 429, \
        "the account's ceiling holds whatever device cookies are used"
    assert not signed_in(second)


def test_the_accounts_ceiling_is_out_of_reach_for_browsers_without_a_device_cookie():
    """Someone who knows only the password shares one budget, which ends well below the ceiling."""
    assert signin.TWO_STEP_MAX_PER_ACCOUNT_DAY > signin.TWO_STEP_MAX_PER_DAY


def test_each_further_wrong_code_waits_longer(app, db, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_MAX", 100)
    monkeypatch.setattr(signin, "TWO_STEP_DELAY_AFTER", 2)
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    _secret, codes = enrol(owner)
    browser = app.test_client()
    password_step(browser, user)
    assert [browser.post("/login/two-step", data=WRONG).status_code for _ in range(2)] == [401, 401]

    waiting = browser.post("/login/two-step", data={"code": codes[0]})
    assert waiting.status_code == 429 and not signed_in(browser)
    assert "Wait 1 min" in waiting.get_data(as_text=True)
    _age_wrong_codes(db, signin.TWO_STEP_DELAY_SECONDS)
    assert browser.post("/login/two-step", data=WRONG).status_code == 401

    _age_wrong_codes(db, signin.TWO_STEP_DELAY_SECONDS)
    assert browser.post("/login/two-step", data={"code": codes[0]}).status_code == 429, "twice as long now"
    _age_wrong_codes(db, signin.TWO_STEP_DELAY_SECONDS)
    response = browser.post("/login/two-step", data={"code": codes[0]})
    assert response.status_code == 302 and signed_in(browser)


def test_the_wait_is_translated(app, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_DELAY_AFTER", 1)
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    enrol(owner)
    browser = app.test_client()
    password_step(browser, user)
    browser.post("/login/two-step", data=WRONG)
    italian = browser.post("/login/two-step", data=WRONG, headers={"Accept-Language": "it"})
    assert italian.status_code == 429 and "Attendi 1 min" in italian.get_data(as_text=True)


def test_a_waiting_browser_does_not_hold_up_the_owners_known_browser(app, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_DELAY_AFTER", 1)
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    _secret, codes = enrol(owner)
    owner.post("/logout")
    stranger = app.test_client()
    password_step(stranger, user)
    stranger.post("/login/two-step", data=WRONG)
    assert stranger.post("/login/two-step", data=WRONG).status_code == 429
    password_step(owner, user)
    assert owner.post("/login/two-step", data={"code": codes[0]}).status_code == 302


def test_version_9_adds_the_device_epoch_columns(db):
    """Pinned like the rest of version 9 (``tests/test_wiki_migrations_frozen.py``)."""
    columns = {row["name"]: (row["type"], row["notnull"], row["dflt_value"])
               for row in db.all('PRAGMA table_info("users")')}
    assert columns["device_epoch"] == ("TEXT", 0, None)
    assert columns["device_epoch_stamp"] == ("TEXT", 0, None)
