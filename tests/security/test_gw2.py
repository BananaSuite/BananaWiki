"""Regression tests for the GW2 findings of the full re-audit pass 2 (hosting portal, ops, contrib)."""

from __future__ import annotations

import pytest

from tests.hosting.conftest import login, make_account, make_wiki, portal, query, runtime, web  # noqa: F401
from tests.hosting.hosting_support import PASSWORD
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401
from tests.security.test_fw2 import _platform_sign_in, _transfer, speech_server  # noqa: F401

# GW2-1: a platform sign-in between the change of owner and the wiki's sign-out job


def _connect(platform, login, username: str):  # noqa: F811
    """The owner connects a wiki administrator account (*username*) to their portal account."""
    from tests.hosting.test_cross_wiki_sign_in import _hash, _open_wiki, _signed_in, _wiki_sql

    _wiki_sql(platform, "INSERT INTO users (id, username, password, role) VALUES ('adm', 'admin_team', ?, 'owner')",
              (_hash(platform),))
    _wiki_sql(platform, "INSERT INTO users (id, username, password, role) VALUES ('me1', 'oldowner', ?, 'admin')",
              (_hash(platform),))
    browser, wiki = platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, platform["owner"])
    _open_wiki(platform, browser, wiki)
    wiki.post("/platform-oauth/merge", data={"username": username, "password": PASSWORD}, base_url=platform["url"])
    assert _signed_in(platform, wiki)
    return browser


@pytest.mark.parametrize("username", ["admin_team", "oldowner"])
def test_gw2_1_sign_in_before_the_signout_job_meets_the_connect_page(platform, make_account, login, query,  # noqa: F811
                                                                     username):
    from tests.hosting.test_cross_wiki_sign_in import _signed_in, _wiki_sql

    browser = _connect(platform, login, username)
    _transfer(platform, make_account, login, query, "none")
    wiki, location = _platform_sign_in(platform, browser)  # the wiki has not fetched the event yet
    assert location.endswith("/platform-oauth/connect")
    assert not _signed_in(platform, wiki)
    assert _wiki_sql(platform, "SELECT * FROM platform_oauth_links") == []


@pytest.mark.parametrize("username", ["admin_team", "oldowner"])
def test_gw2_1_owner_changed_ends_sessions_started_after_it(platform, make_account, login, query,  # noqa: F811
                                                            username):
    """Without the portal's link lookup the stale link signs in, and the sign-out job then ends that session."""
    from tests.hosting.test_cross_wiki_sign_in import _run_signouts, _signed_in, _wiki_sql

    browser = _connect(platform, login, username)
    _transfer(platform, make_account, login, query, "none")
    query("UPDATE hosting_oauth_signouts SET created_at = '2001-01-01 00:00:00' WHERE reason = 'owner_changed'")
    _wiki_sql(platform, "UPDATE platform_oauth_links SET linked_at = '2000-06-01 00:00:00'")
    platform["wiki"].config["BW"].platform_oauth.pop("link_status_url")
    race, location = _platform_sign_in(platform, browser)
    assert "/platform-oauth/connect" not in location and _signed_in(platform, race)
    _run_signouts(platform)
    assert _wiki_sql(platform, "SELECT * FROM platform_oauth_links") == []
    assert not _signed_in(platform, race)
    assert race.get("/admin/users", base_url=platform["url"]).status_code != 200


def test_gw2_1_new_owner_and_members_keep_signing_in(platform, make_account, login, query):  # noqa: F811
    from tests.hosting.test_cross_wiki_sign_in import _signed_in, _wiki_sql

    member = make_account("alex")
    member_browser = platform["portal"].test_client()
    login(member_browser, member)
    first, location = _platform_sign_in(platform, member_browser)
    assert location.endswith("/platform-oauth/connect")
    first.post("/platform-oauth/signup", data={"username": "alex"}, base_url=platform["url"])
    assert _signed_in(platform, first)
    _connect(platform, login, "admin_team")
    _transfer(platform, make_account, login, query, "none")
    recipient = query("SELECT a.* FROM accounts a JOIN instances i ON i.account_id = a.id WHERE i.id = ?",
                      (platform["inst"]["id"],), one=True)

    again, location = _platform_sign_in(platform, member_browser)
    assert "/platform-oauth/connect" not in location and _signed_in(platform, again), "members still one click"

    new_owner = platform["portal"].test_client()
    login(new_owner, recipient)
    wiki, location = _platform_sign_in(platform, new_owner)
    assert location.endswith("/platform-oauth/connect")
    wiki.post("/platform-oauth/merge", data={"username": "admin_team", "password": PASSWORD},
              base_url=platform["url"])
    assert _signed_in(platform, wiki), "the new owner connects the administrator before the sign-out job ran"
    assert _wiki_sql(platform, "SELECT account_id FROM platform_oauth_links WHERE user_id = 'adm'") == [
        {"account_id": recipient["id"]}]


def test_gw2_1_a_failing_portal_keeps_the_link(platform, make_account, login, monkeypatch):  # noqa: F811
    """Only the portal's answer drops a link: a busy or failing link lookup fails that sign-in and keeps it."""
    from bananawiki.wiki.features.auth import platform_oauth
    from tests.hosting.test_cross_wiki_sign_in import _signed_in, _wiki_sql

    member = make_account("alex")
    member_browser = platform["portal"].test_client()
    login(member_browser, member)
    first, _location = _platform_sign_in(platform, member_browser)
    first.post("/platform-oauth/signup", data={"username": "alex"}, base_url=platform["url"])
    links = _wiki_sql(platform, "SELECT account_id, user_id FROM platform_oauth_links")
    assert len(links) == 1
    real_call = platform_oauth._call

    for status, body in ((429, None), (503, {"error": "unavailable"}), (200, {"error": "?"})):
        def failing(method, url, status=status, body=body, **kwargs):
            if "link-status" in url:
                return status, body
            return real_call(method, url, **kwargs)

        monkeypatch.setattr(platform_oauth, "_call", failing)
        refused, location = _platform_sign_in(platform, member_browser)
        assert not _signed_in(platform, refused) and "/platform-oauth/connect" not in location
        assert _wiki_sql(platform, "SELECT account_id, user_id FROM platform_oauth_links") == links
    monkeypatch.setattr(platform_oauth, "_call", real_call)
    again, _location = _platform_sign_in(platform, member_browser)
    assert _signed_in(platform, again)


# GW2-2: safe-method bodies and requests that match no route


def test_gw2_2_get_bodies_are_capped_too(tmp_path):
    import io

    from bananawiki.hosting import accounts
    from bananawiki.hosting.db import connection_scope
    from tests.hosting.hosting_support import build_portal

    app = build_portal(tmp_path, csrf=True)
    with app.app_context(), connection_scope(app.extensions["bananawiki.hosting.database"]):
        accounts.create("gw2someone", PASSWORD)  # sign-up is open: the page reads request.form
    client = app.test_client()
    big = {"f": (io.BytesIO(b"A" * (3 * 1024 * 1024)), "a.bin")}
    assert client.get("/signup", data=big, content_type="multipart/form-data").status_code == 413
    assert client.get("/signup").status_code == 200


def test_gw2_2_unrouted_requests_count_against_the_global_limit(tmp_path, monkeypatch):
    from bananawiki.hosting import app as hosting_app
    from tests.hosting.hosting_support import build_portal, csrf_token

    monkeypatch.setattr(hosting_app, "GLOBAL_RATE_LIMIT", 5)
    client = build_portal(tmp_path, csrf=True).test_client()
    csrf_token(client)
    for _ in range(2):
        assert client.post("/no-such-page", data={"csrf_token": "wrong"}).status_code != 429
        assert client.post("/", data={"csrf_token": "wrong"}).status_code != 429  # GET-only: 405
    assert client.post("/no-such-page", data={"csrf_token": "wrong"}).status_code == 429
    assert client.get("/login").status_code == 429
    assert client.get("/health").status_code == 200, "health checks stay exempt"


# GW2-3: account moderation re-publishes the wikis' routes at once


def _routed(runtime, inst_id: str) -> bool:  # noqa: F811
    return inst_id in {spec.instance_id for spec in runtime.routes}


def _running_wiki(portal, runtime, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import instances
    from bananawiki.hosting.db import connection_scope

    owner = make_account()
    inst = make_wiki(owner)
    query("UPDATE instances SET status = 'running' WHERE id = ?", (inst["id"],))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.sync_routes()
    assert _routed(runtime, inst["id"])
    return owner, inst


def test_gw2_3_scheduled_deletion_republishes_routes(portal, runtime, make_account, make_wiki, login,  # noqa: F811
                                                     query):  # noqa: F811
    owner, inst = _running_wiki(portal, runtime, make_account, make_wiki, query)
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    assert client.post(f"/admin/accounts/{owner['id']}/schedule-deletion",
                       data={"deletion_days": "1"}).status_code == 302
    assert not _routed(runtime, inst["id"])
    assert client.post(f"/admin/accounts/{owner['id']}/cancel-deletion").status_code == 302
    assert _routed(runtime, inst["id"])


def test_gw2_3_cli_suspension_republishes_routes(portal, runtime, make_account, make_wiki, query):  # noqa: F811
    import argparse

    from bananawiki.hosting import admin as cli
    from bananawiki.hosting.db import connection_scope

    owner, inst = _running_wiki(portal, runtime, make_account, make_wiki, query)
    args = argparse.Namespace(account=owner["username"], reason="abuse", reason_visible=False, time_visible=False,
                              also_instances=False, json=False, until=None, hours=None, days=None, duration=None)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        cli.cmd_account_suspend(args)
        assert not _routed(runtime, inst["id"])
        cli.cmd_account_unsuspend(args)
        assert _routed(runtime, inst["id"])


# GW2-4: the GPU speech server keeps no connection alive for clients without a token


TOKEN = "x" * 32


def _synthesize(port: int, connection=None) -> int | str:
    import http.client
    import json

    conn = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/synthesize", body=json.dumps({"text": "hello"}),
                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        return response.status
    except (OSError, http.client.HTTPException) as error:
        return type(error).__name__
    finally:
        if connection is None:
            conn.close()


def _health(sock, token: str = "") -> bytes:
    """One keep-alive GET /health on *sock*; its whole answer (b"" when the server closed the connection)."""
    auth = f"Authorization: Bearer {token}\r\n" if token else ""
    sock.sendall(f"GET /health HTTP/1.1\r\nHost: x\r\n{auth}\r\n".encode())
    answer = b""
    while b"\r\n\r\n" not in answer and (chunk := sock.recv(4096)):
        answer += chunk
    head, _, body = answer.partition(b"\r\n\r\n")
    length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    while len(body) < length and (chunk := sock.recv(4096)):
        body += chunk
    return answer and head + b"\r\n\r\n" + body


def test_gw2_4_answers_without_a_token_close_the_connection(speech_server):  # noqa: F811
    import socket

    port = speech_server()
    for request in (b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n", b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n",
                    b"PUT /synthesize HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(request)
        answer = b""
        while chunk := sock.recv(4096):
            answer += chunk
        assert answer.startswith(b"HTTP/1.1 ") and b"Connection: close" in answer
        sock.close()
    kept = socket.create_connection(("127.0.0.1", port), timeout=5)
    assert _health(kept, TOKEN).startswith(b"HTTP/1.1 200")
    assert _health(kept, TOKEN).startswith(b"HTTP/1.1 200"), "authenticated keep-alive still works"
    kept.close()


def test_gw2_4_tokenless_connections_cannot_shut_out_the_shared_address(speech_server):  # noqa: F811
    import socket

    port = speech_server()  # defaults: 8 per address, 4 of them without a token
    keep_alive = __import__("http.client").client.HTTPConnection("127.0.0.1", port, timeout=5)
    assert _synthesize(port, keep_alive) == 200
    holders = []
    for number in range(8):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        holders.append(sock)
        try:
            if number % 2:
                sock.sendall(b"POST /synthesize HTTP/1.1\r\n")  # trickling a request
            else:
                _health(sock)  # the finding's keep-alive /health, which now ends the connection
        except OSError:
            pass  # closed to make room for a newer one
    assert _synthesize(port) == 200, "a wiki on the same address is still served"
    assert _synthesize(port, keep_alive) == 200, "an authenticated keep-alive connection is not taken"
    keep_alive.close()
    for sock in holders:
        sock.close()
