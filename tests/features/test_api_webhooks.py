"""Outgoing webhooks: configuration (API and admin page), signed deliveries, retries, SSRF guard, SDK helpers."""

from __future__ import annotations

import json
import threading

import pytest

from bananawiki.core import http as core_http
from bananawiki.sdk.client import WikiClient, verify_webhook
from bananawiki.wiki.features.api_service import webhooks

from .api_support import call, enable_api, issue
from .pages_support import in_app, make_page


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


@pytest.fixture
def boss(make_user):
    return make_user("boss", role="admin")


class Receiver:
    """A fake transport that records requests and answers with the given statuses in turn."""

    def __init__(self, *statuses: int | Exception):
        self.statuses = list(statuses) or [200]
        self.calls: list[dict] = []

    def __call__(self, method, url, **options):
        self.calls.append({"method": method, "url": url, **options})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return core_http.HttpResponse(status, {}, b"ok")


def create_hook(client, token, **fields):
    body = {"url": "https://hooks.example.org/wiki", "events": ["page.created", "page.updated"], **fields}
    response = call(client, "POST", "/admin/webhooks", token, json=body)
    assert response.status_code == 201, response.json
    return response.json


def run_deliveries(app, transport):
    return in_app(app, lambda: webhooks.deliver_due(transport=transport))


def test_webhook_crud_and_secret_handling(api_app, client, db, boss, make_user):
    token = issue(api_app, boss, ["admin"])
    created = create_hook(client, token, description="CI")
    secret, hook = created["secret"], created["webhook"]
    assert secret.startswith("whsec_") and "secret" not in hook
    stored = db.scalar("SELECT secret FROM api_service__webhooks WHERE id = ?", (hook["id"],))
    assert stored.startswith("fernet:") and secret not in stored
    listed = call(client, "GET", "/admin/webhooks", token).json
    assert [row["id"] for row in listed["webhooks"]] == [hook["id"]] and "page.created" in listed["events"]
    assert "secret" not in json.dumps(listed["webhooks"])

    changed = call(client, "PUT", f"/admin/webhooks/{hook['id']}", token,
                   json={"events": ["user.created"], "active": False})
    assert changed.json["webhook"]["events"] == ["user.created"] and changed.json["webhook"]["active"] is False
    rotated = call(client, "POST", f"/admin/webhooks/{hook['id']}/rotate-secret", token).json["secret"]
    assert rotated != secret

    for body, field in (({"url": "ftp://x.example/", "events": ["page.created"]}, "url"),
                        ({"url": "https://user:pw@x.example/", "events": ["page.created"]}, "url"),
                        ({"url": "https://x.example/", "events": ["page.nope"]}, "events"),
                        ({"url": "https://x.example/", "events": []}, "events")):
        refused = call(client, "POST", "/admin/webhooks", token, json=body)
        assert refused.status_code == 400 and refused.json["field"] == field, body

    member = make_user("member1", api_access_enabled=1)
    assert call(client, "GET", "/admin/webhooks", issue(api_app, member, ["pages"])).status_code == 403
    editor_token = issue(api_app, make_user("eddie", role="editor", api_access_enabled=1), ["pages"])
    assert call(client, "GET", "/admin/webhooks", editor_token).status_code == 403
    assert call(client, "DELETE", f"/admin/webhooks/{hook['id']}", token).status_code == 200
    assert call(client, "GET", f"/admin/webhooks/{hook['id']}", token).status_code == 404


def test_events_are_signed_and_delivered(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin", "pages"])
    created = create_hook(client, token)
    create_hook(client, token, url="https://other.example.org/", events=["user.created"])
    call(client, "POST", "/pages", token, json={"title": "Launch", "content": "secret words"})
    rows = db.all("SELECT * FROM api_service__webhook_deliveries")
    assert [row["event"] for row in rows] == ["page.created"]

    receiver = Receiver(200)
    assert run_deliveries(api_app, receiver) == 1
    sent = receiver.calls[0]
    assert sent["method"] == "POST" and sent["url"] == "https://hooks.example.org/wiki"
    assert sent["allow_private"] is False
    headers = sent["headers"]
    assert headers["X-BananaWiki-Event"] == "page.created"
    assert verify_webhook(created["secret"], sent["body"], headers["X-BananaWiki-Timestamp"],
                          headers["X-BananaWiki-Signature"])
    assert not verify_webhook("whsec_wrong", sent["body"], headers["X-BananaWiki-Timestamp"],
                              headers["X-BananaWiki-Signature"])
    payload = json.loads(sent["body"])
    assert payload["event"] == "page.created" and payload["data"]["page"]["slug"] == "launch"
    assert payload["id"] == headers["X-BananaWiki-Delivery"]
    assert "secret words" not in sent["body"].decode()

    deliveries = call(client, "GET", f"/admin/webhooks/{created['webhook']['id']}/deliveries", token).json
    assert deliveries["deliveries"][0]["state"] == "delivered" and deliveries["deliveries"][0]["response_status"] \
        == 200
    hook = call(client, "GET", f"/admin/webhooks/{created['webhook']['id']}", token).json["webhook"]
    assert hook["last_status"] == "ok" and hook["consecutive_failures"] == 0
    assert run_deliveries(api_app, receiver) == 0  # nothing left


def test_failures_are_retried_with_backoff_then_given_up(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)["webhook"]
    delivery = call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token)
    assert delivery.status_code == 202 and delivery.json["delivery"]["event"] == "ping"
    delivery_id = delivery.json["delivery"]["id"]

    failing = Receiver(503)
    assert run_deliveries(api_app, failing) == 1
    row = db.one("SELECT * FROM api_service__webhook_deliveries WHERE id = ?", (delivery_id,))
    assert row["state"] == "pending" and row["attempts"] == 1 and row["error"] == "http_503"
    assert row["next_attempt_at"] > db.scalar("SELECT datetime('now', '+50 seconds')")
    assert run_deliveries(api_app, failing) == 0  # not due yet

    for _attempt in range(webhooks.MAX_ATTEMPTS - 1):
        db.execute("UPDATE api_service__webhook_deliveries SET next_attempt_at = datetime('now', '-1 second')")
        run_deliveries(api_app, Receiver(core_http.HttpError("down")))
    row = db.one("SELECT * FROM api_service__webhook_deliveries WHERE id = ?", (delivery_id,))
    assert row["state"] == "failed" and row["attempts"] == webhooks.MAX_ATTEMPTS and row["error"] == "network_error"
    assert db.scalar("SELECT consecutive_failures FROM api_service__webhooks") == webhooks.MAX_ATTEMPTS

    again = call(client, "POST", f"/admin/webhooks/{hook['id']}/deliveries/{delivery_id}/redeliver", token)
    assert again.status_code == 202 and again.json["delivery"]["state"] == "pending"
    assert call(client, "POST", f"/admin/webhooks/{hook['id']}/deliveries/{delivery_id}/redeliver",
                token).status_code == 409
    run_deliveries(api_app, Receiver(204))
    detail = call(client, "GET", f"/admin/webhooks/{hook['id']}/deliveries/{delivery_id}", token).json["delivery"]
    assert detail["state"] == "delivered" and detail["payload"]["event"] == "ping"


def test_private_destinations_are_refused_without_retry(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token, url="http://127.0.0.1:9/hook")["webhook"]
    call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token)
    run_deliveries(api_app, None)  # the real, SSRF-guarded client
    row = db.one("SELECT state, error, attempts FROM api_service__webhook_deliveries")
    assert row == {"state": "failed", "error": "blocked_destination", "attempts": 1}


def test_managed_hosting_never_allows_the_local_network(app_factory, tmp_path, login):
    """On a hosted wiki the local network is the host's: the switch is refused, and ignored if stored earlier."""
    from bananawiki.core.sqlite import Session
    from bananawiki.wiki import accounts
    from tests.conftest import PASSWORD

    hosted = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "hosted"), "BW_MANAGED_HOSTING": "1"})
    db = Session(hosted.extensions["bananawiki.database"].connect())
    try:
        enable_api(hosted, db)
        boss = in_app(hosted, lambda: accounts.create("boss", PASSWORD, role="admin", emit_event=False))
        token, client = issue(hosted, boss, ["admin"]), hosted.test_client()
        body = {"url": "http://127.0.0.1:9/hook", "events": ["page.created"], "allow_private_network": True}
        refused = call(client, "POST", "/admin/webhooks", token, json=body)
        assert refused.status_code == 403 and refused.json["code"] == "private_network_managed"
        hook = create_hook(client, token, url="http://127.0.0.1:9/hook")["webhook"]
        assert call(client, "PUT", f"/admin/webhooks/{hook['id']}", token,
                    json={"allow_private_network": True}).json["code"] == "private_network_managed"
        db.execute("UPDATE api_service__webhooks SET allow_private_network = 1")
        assert call(client, "GET", f"/admin/webhooks/{hook['id']}", token).json["webhook"][
            "allow_private_network"] is False
        call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token)
        receiver = Receiver(200)
        run_deliveries(hosted, receiver)
        assert receiver.calls[0]["allow_private"] is False
        login(client, boss)
        assert b'name="allow_private_network"' not in client.get("/admin/api-service").data
        client.post("/admin/api-service/webhooks", data={"url": "http://127.0.0.1:9/x", "events": ["page.created"],
                                                         "allow_private_network": "1"})
        assert db.scalar("SELECT COUNT(*) FROM api_service__webhooks") == 1
    finally:
        db.conn.close()


def test_a_failing_receiver_does_not_hold_up_the_others(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin", "pages"])
    create_hook(client, token, url="https://down.example.org/")
    create_hook(client, token, url="https://up.example.org/")
    for number in range(3):
        call(client, "POST", "/pages", token, json={"title": f"Page {number}"})

    def transport(method, url, **options):
        if "down" in url:
            raise core_http.HttpError("down")
        return core_http.HttpResponse(200, {}, b"")

    run_deliveries(api_app, transport)
    states = db.all("SELECT w.url, d.state, d.attempts FROM api_service__webhook_deliveries d "
                    "JOIN api_service__webhooks w ON w.id = d.webhook_id ORDER BY d.id")
    assert [row["state"] for row in states if "up." in row["url"]] == ["delivered"] * 3
    assert sum(row["attempts"] for row in states if "down." in row["url"]) == 1


def test_inactive_webhooks_get_nothing_and_events_carry_no_content(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin", "users"])
    create_hook(client, token, events=["user.created", "page.updated"], active=False)
    new_user = {"password": "a long enough password 1"}
    assert call(client, "POST", "/users", token, json={**new_user, "username": "newcomer"}).status_code == 201
    assert db.scalar("SELECT COUNT(*) FROM api_service__webhook_deliveries") == 0
    db.execute("UPDATE api_service__webhooks SET active = 1")
    assert call(client, "POST", "/users", token, json={**new_user, "username": "second"}).status_code == 201
    payload = json.loads(db.scalar("SELECT payload FROM api_service__webhook_deliveries"))
    assert payload["data"]["user"]["username"] == "second" and set(payload["data"]["user"]) == {"id", "username",
                                                                                               "role"}
    page = make_page(api_app, "Doc", "body text")
    from bananawiki.wiki.features.pages import service

    in_app(api_app, lambda: service.update(page, author_id=boss["id"], content="changed text"))
    updated = json.loads(db.scalar("SELECT payload FROM api_service__webhook_deliveries WHERE event = 'page.updated'"))
    assert updated["data"]["page"]["revision"] == 2 and updated["data"]["previous_revision"] == 1
    assert "changed text" not in json.dumps(updated)


def test_admin_page_manages_webhooks(api_app, client, db, login, boss, make_user):
    login(client, boss)
    page = client.post("/admin/api-service/webhooks", data={
        "url": "https://hooks.example.org/x", "events": ["page.created", "page.deleted"], "description": "Chat"})
    assert page.status_code == 201 and page.headers["Cache-Control"] == "no-store"
    hook = db.one("SELECT * FROM api_service__webhooks")
    assert hook is not None and b"whsec_" in page.data
    assert b"whsec_" not in client.get("/admin/api-service").data
    bad = client.post("/admin/api-service/webhooks", data={"url": "nope", "events": ["page.created"]})
    assert bad.status_code == 302 and db.scalar("SELECT COUNT(*) FROM api_service__webhooks") == 1

    assert client.post(f"/admin/api-service/webhooks/{hook['id']}/ping").status_code == 302
    detail = client.get(f"/admin/api-service/webhooks/{hook['id']}")
    assert detail.status_code == 200 and b"ping" in detail.data
    assert client.post(f"/admin/api-service/webhooks/{hook['id']}/toggle").status_code == 302
    assert db.scalar("SELECT active FROM api_service__webhooks") == 0
    rotated = client.post(f"/admin/api-service/webhooks/{hook['id']}/rotate")
    assert rotated.status_code == 200 and b"whsec_" in rotated.data
    assert client.post(f"/admin/api-service/webhooks/{hook['id']}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM api_service__webhooks") == 0

    client.get("/logout")
    login(client, make_user("eddie", role="editor"))
    assert client.post("/admin/api-service/webhooks", data={"url": "https://x.example/",
                                                            "events": ["page.created"]}).status_code in (302, 403)
    assert db.scalar("SELECT COUNT(*) FROM api_service__webhooks") == 0
    assert client.get("/admin/api-service/webhooks/1").status_code in (302, 403, 404)


def test_prune_drops_old_deliveries(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)["webhook"]
    call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token)
    db.execute("UPDATE api_service__webhook_deliveries SET created_at = datetime('now', '-40 days')")
    assert in_app(api_app, webhooks.prune) == 1


def test_sdk_client_against_a_running_wiki(api_app, make_user):
    from werkzeug.serving import make_server

    member = make_user("sdk_user", role="editor", api_access_enabled=1)
    token = issue(api_app, member, ["pages"])
    server = make_server("127.0.0.1", 0, api_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wiki = WikiClient(f"http://127.0.0.1:{server.server_port}", token)
        created = wiki.create_page("From SDK", "one", idempotency_key="sdk-1")
        again = wiki.create_page("From SDK", "one", idempotency_key="sdk-1")
        assert created["id"] == again["id"] and wiki.last_headers["idempotent-replayed"] == "true"
        assert int(wiki.last_headers["x-ratelimit-remaining"]) >= 0
        updated = wiki.update_page("from-sdk", content="two", revision=created["revision"])
        assert updated["content"] == "two"
        from bananawiki.sdk.client import ApiError

        with pytest.raises(ApiError) as stale:
            wiki.update_page("from-sdk", content="three", revision=created["revision"])
        assert stale.value.status == 412 and stale.value.data["revision"] == updated["revision"]
        assert "from-sdk" in [page["slug"] for page in wiki.iter_pages(page_size=1)]
    finally:
        server.shutdown()


def test_verify_webhook_rejects_old_timestamps():
    body = b'{"event":"ping"}'
    signature = webhooks.signature("whsec_x", "1000", body)
    assert verify_webhook("whsec_x", body, "1000", signature, now=1100)
    assert not verify_webhook("whsec_x", body, "1000", signature, now=2000)
    assert not verify_webhook("whsec_x", body, "not-a-number", signature, now=1000)


def test_verify_webhook_answers_false_for_any_signature():
    body = b'{"event":"ping"}'
    signature = webhooks.signature("whsec_x", "1000", body)
    assert verify_webhook("whsec_x", body, "1000", signature.encode(), now=1000)
    for forged in ("sha256=ümlaut", "\u2603", "", None, 42):
        assert verify_webhook("whsec_x", body, "1000", forged, now=1000) is False
