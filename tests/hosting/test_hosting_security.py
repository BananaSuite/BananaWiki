"""Cross-cutting protections: CSRF, headers, health, maintenance gate, loopback-only endpoints."""

from __future__ import annotations

import sqlite3

from .hosting_support import PASSWORD, build_portal, csrf_token, portal_environ


def test_forms_without_csrf_token_are_refused(tmp_path):
    app = build_portal(tmp_path, csrf=True)
    client = app.test_client()
    with app.app_context():
        from bananawiki.hosting import accounts
        from bananawiki.hosting.db import connection_scope

        with connection_scope():
            accounts.create("guarded", PASSWORD)
    response = client.post("/login", data={"username": "guarded", "password": PASSWORD})
    assert response.status_code == 302 and not response.headers["Location"].endswith("/dashboard")
    assert client.get("/dashboard").status_code == 302
    response = client.post("/login", data={"username": "guarded", "password": PASSWORD,
                                           "csrf_token": csrf_token(client)})
    assert response.headers["Location"].endswith("/dashboard")


def test_security_headers(web):
    response = web.get("/login")
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "script-src" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "bwh_session" not in response.headers.get("Set-Cookie", "") or "HttpOnly" in response.headers["Set-Cookie"]


def test_signed_in_pages_are_not_cached(web, make_account, login):
    login(web, make_account())
    assert "no-store" in web.get("/dashboard").headers["Cache-Control"]


def test_health_never_reveals_error_details(portal, web, monkeypatch):
    database = portal.extensions["bananawiki.hosting.database"]

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open /srv/secret/hosting.db")

    monkeypatch.setattr(database, "connect", broken)
    response = web.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable"}
    assert "secret" not in response.get_data(as_text=True)


def test_maintenance_file_gates_everything_but_health(tmp_path):
    marker = tmp_path / "maintenance"
    marker.write_text("1")
    app = build_portal(tmp_path, environ=portal_environ(tmp_path, BANANA_MAINTENANCE_FILE=str(marker)))
    client = app.test_client()
    assert client.get("/login").status_code == 503
    assert client.get("/health").status_code == 200


def test_certificate_ask_endpoint_answers_loopback_only(web, make_account, make_wiki):
    remote = {"REMOTE_ADDR": "203.0.113.9"}
    assert web.get("/internal/domains/authorize?domain=x.example", environ_base=remote).status_code == 404
    wiki = make_wiki(make_account(), "certs")
    assert wiki
    assert web.get("/internal/domains/authorize?domain=certs-hosting.wiki.test").status_code == 200
    assert web.get("/internal/domains/authorize?domain=unknown.example").status_code == 403


def test_open_redirects_are_refused_after_login(web, make_account):
    user = make_account()
    response = web.post("/login?next=https://evil.example/", data={"username": user["username"], "password": PASSWORD})
    assert response.headers["Location"].endswith("/dashboard")


def test_error_pages_do_not_leak_internals(portal, web, make_account, login, monkeypatch):
    from bananawiki.hosting import instances

    def explode(_account):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(instances, "dashboard_list", explode)
    login(web, make_account())
    response = web.get("/dashboard")
    assert response.status_code == 500
    assert "secret internal detail" not in response.get_data(as_text=True)
