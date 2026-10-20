"""The API's ``current_password`` check counts against the account's password budget.

A token holder must not be able to guess the account's password through
``PUT /users/<own id>`` faster than through the account settings.
"""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.wiki.features.users import service as users_service

from .api_support import call, enable_api, issue

NEW_PASSWORD = "BrandNewPassw0rd!!"


@pytest.fixture
def owner_token(app, db, make_user):
    enable_api(app, db)
    owner = make_user("boss", role="owner")
    return owner, issue(app, owner, ["users"])


def _change(client, owner, token, current: str):
    return call(client, "PUT", f"/users/{owner['id']}", token,
                json={"password": NEW_PASSWORD, "current_password": current})


def test_wrong_current_passwords_are_limited_like_on_the_account_page(client, owner_token):
    owner, token = owner_token
    codes = [_change(client, owner, token, f"guess number {n}").json["code"]
             for n in range(users_service.PASSWORD_CHECKS)]
    assert codes == ["current_password_wrong"] * users_service.PASSWORD_CHECKS
    refused = _change(client, owner, token, PASSWORD)
    assert refused.status_code == 429 and refused.json["code"] == "too_many_attempts"
    assert client.post("/login", data={"username": "boss", "password": NEW_PASSWORD}).status_code != 302, \
        "nothing changed, not even with the right password"


def test_the_api_and_the_account_page_share_the_budget(app, client, db, owner_token, login):
    owner, token = owner_token
    browser = app.test_client()
    login(browser, owner)
    for _ in range(users_service.PASSWORD_CHECKS):
        browser.post("/settings/two-step", data={"action": "enable", "password": "wrong", "code": "000000"})
    assert _change(client, owner, token, PASSWORD).status_code == 429


def test_the_right_current_password_does_not_use_up_the_budget(client, owner_token, db):
    owner, token = owner_token
    assert _change(client, owner, token, PASSWORD).status_code == 200
    assert not db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'account:password'")
