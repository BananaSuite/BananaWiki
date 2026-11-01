"""Regression tests for the NW1 and NW3 findings of the full re-audit pass 8 (request pipeline, sign-in, account status)."""

from __future__ import annotations

import io

import pytest

from tests.conftest import PASSWORD

# NW1-1: requests that match no route count against the global limit


def test_nw1_1_unrouted_methods_count_against_the_global_limit(app, monkeypatch, frozen_limiter_clock):
    from werkzeug import formparser

    import bananawiki.wiki.app as wiki_app

    monkeypatch.setattr(wiki_app, "GLOBAL_RATE_LIMIT", 5)
    app.config["CSRF_DISABLED"] = False
    client = app.test_client()
    assert client.get("/login").status_code == 200  # the session now holds a CSRF token
    parsed = []
    original = formparser.FormDataParser.parse

    def spy(self, stream, mimetype, content_length, options=None):
        parsed.append(content_length)
        return original(self, stream, mimetype, content_length, options)

    monkeypatch.setattr(formparser.FormDataParser, "parse", spy)
    statuses = [client.open("/login", method="PROPFIND", content_type="multipart/form-data",
                            data={"f": (io.BytesIO(b"x" * 4096), "a.bin")}).status_code for _ in range(6)]
    assert statuses[:4] == [400] * 4  # the CSRF check still refuses them...
    assert statuses[4:] == [429, 429]  # ...but they count, and are refused before the body is read
    assert len(parsed) == 4
    assert client.open("/no-such", method="FOO").status_code == 429
    assert client.get("/login").status_code == 429


def test_nw1_1_static_files_stay_outside_the_global_limit(app, monkeypatch, frozen_limiter_clock):
    import bananawiki.wiki.app as wiki_app

    monkeypatch.setattr(wiki_app, "GLOBAL_RATE_LIMIT", 2)
    client = app.test_client()
    for _ in range(2):
        client.get("/login")
    assert client.get("/login").status_code == 429
    assert client.get("/static/does-not-exist.css").status_code == 404


# NW1-3: successful sign-ins are bounded per account and per client network


def _sign_in(app, user, password=PASSWORD, address="10.1.0.1"):
    return app.test_client().post("/login", data={"username": user["username"], "password": password},
                                  environ_base={"REMOTE_ADDR": address})


def _sessions(db, user):
    return db.scalar("SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (user["id"],))


@pytest.fixture
def small_sign_in_budget(monkeypatch):
    from bananawiki.core import passwords
    from bananawiki.wiki.features.auth import signin

    monkeypatch.setattr(signin, "MAX_SIGN_INS_PER_ACCOUNT", 3, raising=False)
    monkeypatch.setattr(signin, "MAX_SIGN_INS_PER_IP", 5, raising=False)
    hashes = []
    original = passwords.verify_password

    def spy(stored, password):
        hashes.append(password)
        return original(stored, password)

    monkeypatch.setattr(passwords, "verify_password", spy)
    return hashes


def test_nw1_3_successful_sign_ins_are_limited_per_account(small_sign_in_budget, app, db, make_user):
    pending = make_user("pending_one", approval_status="pending")
    assert _sign_in(app, pending, "wrong", address="10.1.0.9").status_code == 401  # failures are not counted
    statuses = [_sign_in(app, pending, address=f"10.1.{n}.1").status_code for n in range(4)]
    assert statuses == [302, 302, 302, 429]
    assert _sessions(db, pending) == 3
    hashes = len(small_sign_in_budget)
    assert _sign_in(app, pending, address="10.1.9.1").status_code == 429
    assert len(small_sign_in_budget) == hashes  # refused before the password is hashed
    assert _sign_in(app, make_user("other_one")).status_code == 302  # other accounts are unaffected


def test_nw1_3_successful_sign_ins_are_limited_per_network(small_sign_in_budget, app, db, make_user):
    users = [make_user(f"member{n}") for n in range(6)]
    statuses = [_sign_in(app, user, address=f"2001:db8:7:7::{n + 1}").status_code for n, user in enumerate(users)]
    assert statuses == [302] * 5 + [429]
    assert _sessions(db, users[5]) == 0
    assert _sign_in(app, users[5], address="2001:db8:7:8::1").status_code == 302


# NW3-1: refused accounts get no members-only announcements on their status page

BLOCKS = [{"approval_status": "pending"}, {"approval_status": "denied"}, {"suspended": 1}]


@pytest.fixture
def two_announcements(app, db, admin):
    from tests.features.pages_support import set_feature

    set_feature(app, "announcements", True)
    for content, visibility in (("MembersOnlyZq door code 4711", "logged_in"), ("PublicOnlyZq", "logged_out")):
        db.insert("announcements", {"content": content, "color": "orange", "text_size": "normal",
                                    "visibility": visibility, "audience_mode": "all", "created_by": admin["id"]})


@pytest.mark.parametrize("block", BLOCKS)
def test_nw3_1_refused_accounts_see_no_members_only_announcements(two_announcements, app, make_user, login, block):
    client = app.test_client()
    login(client, make_user("refused", **block))
    assert client.get("/").headers["Location"].endswith("/account-status")
    page = client.get("/account-status")
    assert page.status_code == 200
    assert b"MembersOnlyZq" not in page.data
    assert b"PublicOnlyZq" in page.data  # what an anonymous visitor sees


def test_nw3_1_approved_members_keep_their_announcements(two_announcements, app, make_user, login):
    client = app.test_client()
    login(client, make_user("member"))
    page = client.get("/")
    assert b"MembersOnlyZq" in page.data and b"PublicOnlyZq" not in page.data
