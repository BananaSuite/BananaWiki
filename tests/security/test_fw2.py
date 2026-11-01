"""Regression tests for the FW2 findings of the full re-audit pass (hosting portal, ops, contrib)."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time

import pytest

from tests.hosting.conftest import login, make_account, make_wiki, portal, query, web  # noqa: F401
from tests.hosting.hosting_support import PASSWORD, build_portal, counter, csrf_token
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401

MIB = 1024 * 1024


# FW2-1: request bodies and the global limit before the portal's CSRF check


def _big(token: str, field: str = "f", size: int = 3 * MIB) -> dict:
    return {"csrf_token": token, field: (io.BytesIO(b"A" * size), "a.bin")}


def test_fw2_1_anonymous_bodies_are_capped_before_csrf_reads_them(tmp_path):
    client = build_portal(tmp_path, csrf=True).test_client()
    token = csrf_token(client)
    assert client.post("/login", data=_big("wrong"), content_type="multipart/form-data").status_code == 413
    assert client.post("/login", data=_big(token), content_type="multipart/form-data").status_code == 413


def test_fw2_1_requests_refused_by_csrf_count_against_the_global_limit(tmp_path, monkeypatch):
    from bananawiki.hosting import app as hosting_app

    monkeypatch.setattr(hosting_app, "GLOBAL_RATE_LIMIT", 5)
    client = build_portal(tmp_path, csrf=True).test_client()
    csrf_token(client)
    for _ in range(4):
        assert client.post("/login", data={"csrf_token": "wrong"}).status_code in (302, 400)
    assert client.post("/login", data={"csrf_token": "wrong"}).status_code == 429
    assert client.get("/login").status_code == 429


@pytest.fixture
def csrf_portal(tmp_path):
    return build_portal(tmp_path, csrf=True)


def _account(app, *, admin: bool = False) -> dict:
    from bananawiki.hosting import accounts
    from bananawiki.hosting.db import connection_scope

    with app.app_context(), connection_scope(app.extensions["bananawiki.hosting.database"]):
        return accounts.create(f"fw2user{next(counter)}", PASSWORD, is_admin=admin)


def _signed_in(app, account: dict):
    client = app.test_client()
    token = csrf_token(client)
    response = client.post("/login", data={"csrf_token": token, "username": account["username"],
                                           "password": PASSWORD})
    assert response.status_code == 302
    return client, token


def _start_import(client, token: str) -> str:
    response = client.post("/admin/instances/import/chunk/start", data={
        "csrf_token": token, "filename": "a.zip", "size": str(4 * MIB), "subdomain": "imported"})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    return response.get_json()["upload_id"]


def _chunk(token: str) -> dict:
    return {"csrf_token": token, "offset": "0", "chunk": (io.BytesIO(b"A" * (3 * MIB)), "chunk")}


def test_fw2_1_administrator_still_uploads_large_archive_chunks(csrf_portal):
    client, token = _signed_in(csrf_portal, _account(csrf_portal, admin=True))
    upload_id = _start_import(client, token)
    response = client.post(f"/admin/instances/import/chunk/{upload_id}", data=_chunk(token),
                           content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.get_json()["received"] == 3 * MIB


def test_fw2_1_large_upload_bodies_need_the_administrators_own_session(csrf_portal):
    user = _account(csrf_portal)
    client, token = _signed_in(csrf_portal, _account(csrf_portal, admin=True))
    upload_id = _start_import(client, token)
    other, other_token = _signed_in(csrf_portal, user)
    assert other.post(f"/admin/instances/import/chunk/{upload_id}", data=_chunk(other_token),
                      content_type="multipart/form-data").status_code == 413
    assert client.post(f"/admin/accounts/{user['id']}/impersonate", data={"csrf_token": token}).status_code == 302
    assert client.get("/admin").status_code == 403  # now acting as the regular account
    assert client.post(f"/admin/instances/import/chunk/{upload_id}", data=_chunk(token),
                       content_type="multipart/form-data").status_code == 413


@pytest.mark.parametrize("path", ["/admin/settings", "/account/change-password"])
def test_fw2_1_other_signed_in_forms_keep_the_small_limit(csrf_portal, path):
    client, token = _signed_in(csrf_portal, _account(csrf_portal, admin=True))
    assert client.post(path, data=_big(token), content_type="multipart/form-data").status_code == 413


# FW2-2: a change of owner (and the administrator password reset) ends the previous owner's platform link


def _connect_admin_account(platform, login):  # noqa: F811
    """The owner connects the wiki's administrator account to their portal account (the documented flow)."""
    from tests.hosting.test_cross_wiki_sign_in import _hash, _open_wiki, _signed_in, _wiki_sql

    _wiki_sql(platform, "INSERT INTO users (id, username, password, role) VALUES ('adm', 'admin_team', ?, 'owner')",
              (_hash(platform),))
    browser, wiki = platform["portal"].test_client(), platform["wiki"].test_client()
    login(browser, platform["owner"])
    _open_wiki(platform, browser, wiki)
    wiki.post("/platform-oauth/merge", data={"username": "admin_team", "password": PASSWORD},
              base_url=platform["url"])
    assert _signed_in(platform, wiki)
    return browser, wiki


def _platform_sign_in(platform, browser):  # noqa: F811
    """The previous owner opens the wiki through the portal again; returns where the wiki sends them."""
    from urllib.parse import urlsplit

    from tests.hosting.test_cross_wiki_sign_in import _open_wiki

    wiki = platform["wiki"].test_client()
    answer, _consent = _open_wiki(platform, browser, wiki)
    if answer is None:  # the portal asks for consent: one click
        authorize = urlsplit(wiki.get("/platform-oauth/login", base_url=platform["url"]).headers["Location"])
        allowed = urlsplit(browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
                           .headers["Location"])
        answer = wiki.get(f"{allowed.path}?{allowed.query}", base_url=platform["url"])
    return wiki, answer.headers.get("Location", "")


def _transfer(platform, make_account, login, query, keep: str) -> None:  # noqa: F811
    owner_browser, recipient_browser = platform["portal"].test_client(), platform["portal"].test_client()
    recipient = make_account()
    login(owner_browser, platform["owner"])
    login(recipient_browser, recipient)
    inst_id = platform["inst"]["id"]
    assert owner_browser.post(f"/instances/{inst_id}/transfer",
                              data={"username": recipient["username"]}).status_code == 302
    transfer = query("SELECT id FROM instance_ownership_transfers WHERE status = 'pending'", one=True)
    recipient_browser.post(f"/transfers/{transfer['id']}/accept", data={"previous_owner": keep})
    assert query("SELECT account_id FROM instances WHERE id = ?", (inst_id,), one=True)["account_id"] == recipient["id"]


@pytest.mark.parametrize("keep", ["none", "view"])
def test_fw2_2_transfer_ends_the_previous_owner_link(platform, make_account, login, query, keep):  # noqa: F811
    from tests.hosting.test_cross_wiki_sign_in import _run_signouts, _signed_in, _wiki_sql

    browser, old_session = _connect_admin_account(platform, login)
    inst_id, owner_id = platform["inst"]["id"], platform["owner"]["id"]
    _transfer(platform, make_account, login, query, keep)
    assert query("SELECT 1 FROM hosting_oauth_account_links WHERE account_id = ? AND instance_id = ?",
                 (owner_id, inst_id)) == []
    assert query("SELECT 1 FROM hosting_oauth_grants WHERE account_id = ? AND instance_id = ?",
                 (owner_id, inst_id)) == []
    kept = query("SELECT permissions FROM instance_collaborators WHERE instance_id = ? AND account_id = ?",
                 (inst_id, owner_id))
    assert kept == ([] if keep == "none" else [{"permissions": '["view"]'}]), "the chosen portal access stays"
    assert _run_signouts(platform) == 1
    assert not _signed_in(platform, old_session)
    assert _wiki_sql(platform, "SELECT * FROM platform_oauth_links") == []
    wiki, location = _platform_sign_in(platform, browser)
    assert location.endswith("/platform-oauth/connect"), "no longer the wiki's administrator without its password"
    assert not _signed_in(platform, wiki)


def test_fw2_2_administrators_move_now_ends_the_previous_owner_link(platform, make_account, login, query):  # noqa: F811
    from tests.hosting.test_cross_wiki_sign_in import _run_signouts, _signed_in, _wiki_sql

    browser, old_session = _connect_admin_account(platform, login)
    admin, recipient = make_account(admin=True), make_account()
    admin_browser = platform["portal"].test_client()
    login(admin_browser, admin)
    inst_id = platform["inst"]["id"]
    assert admin_browser.post(f"/admin/instances/{inst_id}/transfer",
                              data={"username": recipient["username"]}).status_code == 302
    assert query("SELECT account_id FROM instances WHERE id = ?", (inst_id,), one=True)["account_id"] == recipient["id"]
    _run_signouts(platform)
    assert not _signed_in(platform, old_session)
    assert _wiki_sql(platform, "SELECT * FROM platform_oauth_links") == []
    _wiki, location = _platform_sign_in(platform, browser)
    assert location.endswith("/platform-oauth/connect")


def test_fw2_2_an_older_owner_changed_event_leaves_a_newer_link(platform, login, query):  # noqa: F811
    """A sign-out fetched again (a portal restored from a backup) never drops a link made after it."""
    from tests.hosting.test_cross_wiki_sign_in import _run_signouts, _wiki_sql

    _connect_admin_account(platform, login)
    query("INSERT INTO hosting_oauth_signouts (instance_id, account_id, wiki_user_id, reason, created_at) "
          "VALUES (?, ?, 'adm', 'owner_changed', '2000-01-01 00:00:00')", (platform["inst"]["id"],
                                                                            platform["owner"]["id"]))
    _run_signouts(platform)
    assert _wiki_sql(platform, "SELECT user_id FROM platform_oauth_links") == [{"user_id": "adm"}]


def test_fw2_2_admin_password_reset_drops_its_platform_links(platform, login, query):  # noqa: F811
    from bananawiki.hosting import oauth
    from bananawiki.hosting.db import connection_scope
    from bananawiki.ops import tenant_task
    from tests.hosting.test_cross_wiki_sign_in import _wiki_sql

    browser, _old_session = _connect_admin_account(platform, login)
    result = tenant_task.reset_admin(platform["wiki"].config["BW"], {"username": "admin_team",
                                                                     "password": "Temporary pass 12345"})
    assert result["user_ids"] == ["adm"]
    assert _wiki_sql(platform, "SELECT * FROM platform_oauth_links") == []
    with platform["portal"].test_request_context("/"), connection_scope():
        assert oauth.forget_wiki_users(platform["inst"]["id"], result["user_ids"]) == 1
    _wiki, location = _platform_sign_in(platform, browser)
    assert location.endswith("/platform-oauth/connect"), "the portal does not hand the link back either"


def test_fw2_2_portal_reset_forgets_links_to_the_reset_accounts(portal, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import instances
    from bananawiki.hosting.db import connection_scope

    owner, other = make_account(), make_account()
    inst = make_wiki(owner)
    for account, wiki_user in ((owner, inst["admin_username"]), (other, "someone")):
        query("INSERT INTO hosting_oauth_account_links (instance_id, account_id, wiki_user_id, wiki_username, "
              "linked_at) VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')", (inst["id"], account["id"], wiki_user,
                                                                        wiki_user))
    with portal.test_request_context("/"), connection_scope():
        instances.reset_admin_password(instances.get(inst["id"]), actor_id=owner["id"])
    assert [row["account_id"] for row in query("SELECT account_id FROM hosting_oauth_account_links")] == [other["id"]]


# FW2-3: the GPU speech server bounds each connection's request and each address's connections


@pytest.fixture
def speech_server():
    from tests.features.test_tts_gpu import FakeEngine, load_server

    servers = []

    def start(**values):
        module = load_server()
        server = module.make_server(module.Settings(token="x" * 32, port=0, **values), FakeEngine())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _held_open(sock, seconds: float) -> bool:
    """Whether the server keeps *sock* open while it trickles one header byte every 0.1 s for *seconds*."""
    deadline = time.monotonic() + seconds
    sock.settimeout(0.1)
    while time.monotonic() < deadline:
        try:
            sock.sendall(b"X")
            if sock.recv(1024) == b"":
                return False
        except TimeoutError:
            continue
        except OSError:
            return False
    return True


def test_fw2_3_defaults():
    from tests.features.test_tts_gpu import load_server

    settings = load_server().Settings.from_env({"TTS_AUTH_TOKEN": "x" * 32})
    assert (settings.request_timeout, settings.max_connections_per_client) == (10.0, 8)


def test_fw2_3_a_trickling_client_is_dropped_at_the_request_deadline(speech_server):
    port = speech_server(socket_timeout=5, request_timeout=0.5)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"POST /synthesize HTTP/1.1\r\n")
    started = time.monotonic()
    assert not _held_open(sock, 3), "a byte every 0.1 s no longer keeps the connection"
    assert time.monotonic() - started < 2
    sock.close()


def test_fw2_3_one_address_cannot_take_every_connection(speech_server):
    from tests.features.test_tts_gpu import call

    port = speech_server(max_connections=64, max_connections_per_client=2, socket_timeout=10)
    idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
    for sock in idle:
        sock.sendall(b"POST /synthesize HTTP/1.1\r\n")
    third = socket.create_connection(("127.0.0.1", port), timeout=5)
    # GW2-4: the newcomer takes the place of the address's oldest connection without a token.
    assert idle[0].recv(64) == b"", "a third connection from the same address closes the oldest idle one"
    assert _held_open(third, 0.3)
    other = http.client.HTTPConnection("127.0.0.1", port, timeout=5, source_address=("127.0.0.2", 0))
    other.request("POST", "/synthesize", body=json.dumps({"text": "hi"}),
                  headers={"Authorization": "Bearer " + "x" * 32, "Content-Type": "application/json"})
    assert other.getresponse().status == 200, "another address is still served"
    other.close()
    for sock in [*idle, third]:
        sock.close()
    for _attempt in range(50):  # the address gets its connections back once they close
        try:
            if call(port, "GET", "/health", token=None)[0] == 200:
                break
        except (ConnectionError, http.client.RemoteDisconnected):
            time.sleep(0.05)
    else:
        raise AssertionError("the address was not served again")


def test_fw2_3_requests_sent_in_pieces_within_the_deadline_still_work(speech_server):
    port = speech_server(request_timeout=3)
    body = json.dumps({"text": "hello"}).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    for piece in (b"POST /synthesize HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + b"x" * 32 + b"\r\n",
                  b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n",
                  body[:5], body[5:]):
        sock.sendall(piece)
        time.sleep(0.1)
    assert sock.recv(64).startswith(b"HTTP/1.1 200")
    sock.close()
