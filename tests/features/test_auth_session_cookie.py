# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""The session cookie: ``__Host-`` prefixed over HTTPS, moved once from the plain name."""

from __future__ import annotations

import time

from conftest import PASSWORD

HTTPS = "https://localhost"
PLAIN = "bw_session"
PREFIXED = "__Host-bw_session"


def _set_cookie(response, name: str) -> str | None:
    """The ``Set-Cookie`` header the response sends for *name*, if any."""
    for header in response.headers.getlist("Set-Cookie"):
        if header.split("=", 1)[0] == name:
            return header
    return None


def _value(header: str) -> str:
    return header.split("=", 1)[1].split(";", 1)[0]


def _sign_in(client, user, **kwargs):
    response = client.post("/login", data={"username": user["username"], "password": PASSWORD}, **kwargs)
    assert response.status_code == 302, response.data[:300]
    return response


def _signed_in_value(response) -> str:
    """The session the sign-in response hands out, under whichever name."""
    header = _set_cookie(response, PREFIXED) or _set_cookie(response, PLAIN)
    assert header is not None
    return _value(header)


def test_https_sign_in_sets_a_host_prefixed_cookie(client, make_user):
    response = _sign_in(client, make_user("alice"), base_url=HTTPS)
    header = _set_cookie(response, PREFIXED)
    assert header is not None and _set_cookie(response, PLAIN) is None
    assert "Secure" in header and "Path=/" in header and "HttpOnly" in header
    assert "Domain=" not in header
    assert client.get("/_probe/private", base_url=HTTPS).data == b"private"


def test_plain_http_keeps_the_plain_name(client, make_user):
    response = _sign_in(client, make_user("alice"))
    header = _set_cookie(response, PLAIN)
    assert header is not None and "Secure" not in header
    assert _set_cookie(response, PREFIXED) is None
    assert client.get("/_probe/private").data == b"private"


def test_secure_cookies_setting_decides_the_name(app_factory, make_user):
    user = make_user("alice")
    forced = app_factory(environ={"BW_SECURE_COOKIES": "1"}).test_client()
    assert _set_cookie(_sign_in(forced, user), PREFIXED) is not None
    never = app_factory(environ={"BW_SECURE_COOKIES": "0"}).test_client()
    response = _sign_in(never, user, base_url=HTTPS)
    assert _set_cookie(response, PLAIN) is not None and _set_cookie(response, PREFIXED) is None


def test_a_session_from_before_the_upgrade_moves_to_the_prefixed_name_once(app, client, make_user):
    _sign_in(client, make_user("bob"))  # plain name, as the previous version issued it
    app.session_interface.legacy_before = time.time() + 60  # the upgrade came after that sign-in
    response = client.get("/_probe/private", base_url=HTTPS)
    assert response.data == b"private"
    moved = _set_cookie(response, PREFIXED)
    expired = _set_cookie(response, PLAIN)
    assert moved is not None and _value(moved) and "Secure" in moved
    assert expired is not None and _value(expired) == "" and "Path=/" in expired
    again = client.get("/_probe/private", base_url=HTTPS)
    assert again.data == b"private" and _set_cookie(again, PLAIN) is None
    browser = app.test_client()
    browser.set_cookie(PREFIXED, _value(moved))
    assert browser.get("/_probe/private", base_url=HTTPS).data == b"private"


def test_a_plain_cookie_issued_after_the_upgrade_does_not_sign_in_over_https(app, client, make_user):
    # A sibling wiki plants its own session for this wiki's plain name (Domain=<base domain>).
    attacker = _signed_in_value(_sign_in(client, make_user("mallory"), base_url=HTTPS))
    victim = app.test_client()
    victim.set_cookie(PLAIN, attacker)
    response = victim.get("/_probe/private", base_url=HTTPS)
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert _set_cookie(response, PREFIXED) is None


def test_two_plain_cookies_are_never_moved(app, client, make_user):
    value = _signed_in_value(_sign_in(client, make_user("bob")))
    app.session_interface.legacy_before = time.time() + 60
    client.set_cookie(PLAIN, value, path="/_probe")  # a second one, as a planted cookie would be
    response = client.get("/_probe/private", base_url=HTTPS)
    assert response.status_code == 302 and _set_cookie(response, PREFIXED) is None


def test_https_sign_out_clears_both_names(app, client, make_user, db):
    user = make_user("bob")
    _sign_in(client, user, base_url=HTTPS)
    client.set_cookie(PLAIN, "left-over")
    response = client.post("/logout", base_url=HTTPS)
    assert response.status_code == 302
    expired = _set_cookie(response, PLAIN)
    assert expired is not None and _value(expired) == ""
    assert db.scalar("SELECT COUNT(*) FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                     (user["id"],)) == 0
    assert client.get("/_probe/private", base_url=HTTPS).status_code == 302


def test_the_upgrade_time_is_kept_across_restarts(app_factory, tmp_path):
    first = app_factory()
    marker = tmp_path / "instance" / ".host_cookie_since"
    since = int(marker.read_text())
    assert first.session_interface.legacy_before == since
    marker.write_text(str(since - 3600))  # first started an hour ago
    assert app_factory().session_interface.legacy_before == since - 3600
