"""Role x endpoint matrix: every route, every method, for each kind of caller.

The expected outcome is derived from what each view declares (``@auth.public``,
``@auth.public_read``, ``@auth.exempt``) and from where it lives (``/admin``,
``/api/v1``), so a new route is covered the day it is added:

* anonymous visitors reach a private view only as a redirect to sign-in (401 for JSON);
* pending and suspended accounts reach only the views exempt from approval;
* users and editors never get a successful answer from an administrator view;
* the REST API answers 401 without a bearer token;
* nobody ever gets a server error.

State-changing requests are sent with empty bodies and CSRF off: a view that
acts without its parameters would show up as a success.
"""

from __future__ import annotations

import pytest

from bananawiki.wiki import accounts, auth
from bananawiki.wiki.db import connection_scope
from tests.conftest import PASSWORD
from tests.features.api_support import enable_api

VALUES = {"slug": "home", "filename": "x.png", "path": "zz-nothing", "code": "abc", "token": "abc",
          "username": "victim", "board_ref": "1", "role": "user", "preset": "x", "session_id": "x",
          "remote_id": "x", "plugin_id": "kanban"}
SKIP = {"auth.logout"}  # would end the caller's own session half-way through
ADMIN_GATEWAYS = {"auth.admin_login", "admin.entry", "admin.stop_impersonating"}


def _probe(app, role: str):
    with app.test_request_context(), connection_scope():
        victim = accounts.create("victim", PASSWORD, role="user", emit_event=False)
        if role != "anon":
            base = role if role in ("user", "editor", "admin") else "user"
            accounts.create("attacker", PASSWORD, role=base, emit_event=False,
                            approval_status="pending" if role == "pending" else "approved")
    client = app.test_client()
    if role != "anon":
        assert client.post("/login", data={"username": "attacker", "password": PASSWORD}).status_code == 302
        if role == "suspended":
            with app.app_context(), connection_scope() as session:
                session.execute("UPDATE users SET suspended = 1 WHERE username = 'attacker'")
    values = dict(VALUES, user_id=victim["id"])
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.endpoint in SKIP or rule.endpoint == "static" or rule.endpoint.endswith(".static"):
            continue
        url = rule.build({name: values.get(name, 1) for name in rule.arguments}, append_unknown=False)[1]
        view = app.view_functions[rule.endpoint]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            response = client.open(url, method=method, data=None if method == "GET" else {})
            yield rule, view, method, response.status_code, response.headers.get("Location", "")


@pytest.fixture
def matrix_app(app_factory, monkeypatch):
    import bananawiki.wiki.app as wiki_app

    monkeypatch.setattr(wiki_app, "GLOBAL_RATE_LIMIT", 10**9)
    app = app_factory()
    with app.app_context(), connection_scope() as session:
        enable_api(app, session)
    return app


@pytest.mark.parametrize("role", ["anon", "pending", "suspended", "user", "editor", "admin"])
def test_no_route_fails_and_private_routes_stay_private(matrix_app, role):
    problems = []
    for rule, view, method, status, location in _probe(matrix_app, role):
        access = auth.view_access(view)
        where = f"{role} {method} {rule.rule} -> {status} {location}"
        if status >= 500 and status != 503:  # 503: the admin pass switched maintenance or the API off
            problems.append(f"server error: {where}")
        ok = 200 <= status < 300
        if rule.rule.startswith("/api/v1/") and access == "public" and not ok and status not in (401, 404):
            continue  # bearer API: checked below for anonymous callers
        if role == "anon":
            if rule.rule.startswith("/api/v1/") and not rule.endpoint.endswith(("openapi_description", "status")):
                if status != 401:
                    problems.append(f"API without a token not refused: {where}")
            elif access == "login" and not (status == 401 or (status == 302 and location.startswith("/login"))):
                problems.append(f"private view reachable: {where}")
        elif role in ("pending", "suspended"):
            if access != "public" and not auth.view_exempt(view, "approval") and ok:
                problems.append(f"blocked account reached a view: {where}")
        elif role in ("user", "editor"):
            admin_view = rule.rule.startswith("/admin") and rule.endpoint not in ADMIN_GATEWAYS
            if admin_view and (ok or (status in (301, 302, 303, 307) and location.startswith("/admin"))):
                problems.append(f"administrator view reached: {where}")
    assert not problems, "\n".join(problems)
