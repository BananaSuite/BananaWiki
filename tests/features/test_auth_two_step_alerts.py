"""Repeated wrong two-step codes at sign-in leave the account's owner a notice (once a day)."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import signin

from .test_auth_two_step import enrol, password_step

NOTICE = "Several wrong two-step verification codes were entered"


@pytest.fixture
def enrolled(app, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_ALERT_AFTER", 3)
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    enrol(owner)
    return user, owner


def _notices(db, user):
    return db.all("SELECT source_id, outcome, endpoint FROM user_notices WHERE user_id = ?", (user["id"],))


def _wrong_code(app, user, times=1):
    browser = app.test_client()
    password_step(browser, user)
    for _ in range(times):
        assert browser.post("/login/two-step", data={"code": "000000"}).status_code == 401


def test_wrong_codes_from_any_browser_add_up_to_one_notice_a_day(app, db, enrolled):
    user, owner = enrolled
    _wrong_code(app, user, 2)
    assert _notices(db, user) == []
    _wrong_code(app, user)  # another browser: the account's total counts
    assert _notices(db, user) == [{"source_id": "auth.two_step", "outcome": "warning",
                                   "endpoint": "auth.two_step_settings"}]
    _wrong_code(app, user, 3)
    assert len(_notices(db, user)) == 1, "once a day"
    page = owner.get("/attention").get_data(as_text=True)
    assert NOTICE in page and "Security" in page


def test_the_owners_own_typos_in_the_settings_do_not_count(app, db, enrolled):
    user, owner = enrolled
    for _ in range(4):
        owner.post("/settings/two-step", data={"action": "renew", "password": PASSWORD, "code": "000000"})
    assert _notices(db, user) == []


def test_the_notice_is_emailed_with_a_security_subject(app, db, enrolled):
    from bananawiki.wiki.features.attention import mailer

    user, _owner = enrolled
    _wrong_code(app, user, 3)
    db.execute("UPDATE users SET email = 'bob@example.com' WHERE id = ?", (user["id"],))
    account = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    notice = db.one("SELECT * FROM user_notices WHERE user_id = ?", (user["id"],))
    with app.test_request_context(), connection_scope():
        message = mailer.decision_message(account, notice, links=False)
    assert "security" in message.subject.lower() and NOTICE in message.text
