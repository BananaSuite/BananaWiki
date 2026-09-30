"""Webhooks: kanban and canvas events, prompt delivery after a request, automatic switch-off after failures."""

from __future__ import annotations

import json
import time

import pytest

from bananawiki.core import http as core_http
from bananawiki.sdk.client import WikiClient
from bananawiki.wiki import attention, registry
from bananawiki.wiki.features.api_service import webhooks

from .api_support import call, enable_api, issue
from .pages_support import in_app

KANBAN_EVENTS = ["kanban.board.created", "kanban.board.updated", "kanban.board.deleted", "kanban.ticket.created",
                 "kanban.ticket.updated", "kanban.ticket.deleted", "kanban.ticket.moved", "kanban.comment.created",
                 "canvas.created", "canvas.updated", "canvas.deleted"]


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


@pytest.fixture
def boss(make_user):
    return make_user("boss", role="admin")


class Receiver:
    def __init__(self, status: int | Exception = 200):
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, method, url, **options):
        self.calls.append({"url": url, **options})
        if isinstance(self.status, Exception):
            raise self.status
        return core_http.HttpResponse(self.status, {}, b"")


def create_hook(client, token, **fields):
    body = {"url": "https://hooks.example.org/wiki", "events": ["page.created"], **fields}
    response = call(client, "POST", "/admin/webhooks", token, json=body)
    assert response.status_code == 201, response.json
    return response.json["webhook"]


def payloads(db, event):
    return [json.loads(row["payload"])["data"] for row in
            db.all("SELECT payload FROM api_service__webhook_deliveries WHERE event = ? ORDER BY id", (event,))]


# ── Kanban and canvas events ──────────────────────────────────────────────────


def test_kanban_and_canvas_events_carry_ids_and_names_only(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    listed = call(client, "GET", "/admin/webhooks", token).json["events"]
    assert set(KANBAN_EVENTS) <= set(listed)
    create_hook(client, token, events=KANBAN_EVENTS)

    public = {"id": 7, "title": "Roadmap", "description": "Board secret", "visibility": "public"}
    private = {"id": 8, "title": "Layoffs", "description": "Very secret", "visibility": "private"}
    ticket = {"id": 70, "board_id": 7, "column_id": 3, "title": "Ship it", "description": "Ticket body"}
    hidden_ticket = {"id": 80, "board_id": 8, "column_id": 4, "title": "Fire Bob", "description": "Details"}
    comment = {"id": 5, "ticket_id": 70, "user_id": boss["id"], "content": "Comment body"}

    def emit_all():
        registry.emit("kanban.board.created", board=public, actor_id=boss["id"])
        registry.emit("kanban.board.updated", board=private, actor_id=boss["id"])
        registry.emit("kanban.ticket.created", ticket=ticket, board=public, actor_id=boss["id"])
        registry.emit("kanban.ticket.updated", ticket=hidden_ticket, board=private, actor_id=None)
        registry.emit("kanban.ticket.moved", ticket=ticket, board=public, from_column_id=3, to_column_id=9,
                      actor_id=boss["id"])
        registry.emit("kanban.comment.created", comment=comment, ticket=ticket, board=public, actor_id=boss["id"])
        registry.emit("kanban.ticket.deleted", ticket=hidden_ticket, board=private, actor_id=boss["id"])
        registry.emit("kanban.board.deleted", board=private, actor_id=boss["id"])
        registry.emit("canvas.created", canvas={"id": 1, "slug": "plan", "title": "Plan", "visibility": "shared",
                                                "data": "{\"nodes\": [\"canvas body\"]}"}, actor_id=boss["id"])
        registry.emit("canvas.updated", canvas={"id": 2, "slug": "merger", "title": "Merger",
                                                "visibility": "private"}, actor_id=boss["id"])
        registry.emit("canvas.deleted", canvas={"id": 3, "slug": "old", "title": "Old"}, actor_id=None)

    in_app(api_app, emit_all)
    everything = json.dumps([row["payload"] for row in db.all("SELECT payload FROM api_service__webhook_deliveries")])
    for secret in ("secret", "Ticket body", "Details", "Comment body", "canvas body", "Layoffs", "Fire Bob",
                   "Merger", "merger", "old"):
        assert secret not in everything, secret

    assert payloads(db, "kanban.board.created") == [
        {"board": {"id": 7, "title": "Roadmap", "visibility": "public"}, "actor_id": boss["id"]}]
    assert payloads(db, "kanban.board.updated")[0]["board"] == {"id": 8, "title": None, "visibility": "private"}
    created = payloads(db, "kanban.ticket.created")[0]
    assert created["ticket"] == {"id": 70, "board_id": 7, "column_id": 3, "title": "Ship it"}
    assert payloads(db, "kanban.ticket.updated")[0]["ticket"] == {"id": 80, "board_id": 8, "column_id": 4,
                                                                  "title": None}
    moved = payloads(db, "kanban.ticket.moved")[0]
    assert moved["from_column_id"] == 3 and moved["to_column_id"] == 9 and moved["ticket"]["id"] == 70
    assert payloads(db, "kanban.comment.created")[0]["comment"] == {"id": 5, "ticket_id": 70, "user_id": boss["id"]}
    assert payloads(db, "canvas.created")[0]["canvas"] == {"id": 1, "slug": "plan", "title": "Plan",
                                                           "visibility": "shared"}
    assert payloads(db, "canvas.updated")[0]["canvas"] == {"id": 2, "slug": None, "title": None,
                                                           "visibility": "private"}
    # A canvas row without its visibility counts as private.
    assert payloads(db, "canvas.deleted")[0]["canvas"]["title"] is None


def test_real_kanban_changes_reach_subscribed_webhooks(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin", "kanban"])
    create_hook(client, token, events=["kanban.board.created", "kanban.board.updated"])
    board = call(client, "POST", "/kanban/boards", token, json={"title": "Launch"}).json["board"]
    assert [row["board"] for row in payloads(db, "kanban.board.created")] == [
        {"id": board["id"], "title": "Launch", "visibility": board["visibility"]}]
    call(client, "PUT", f"/kanban/boards/{board['id']}", token, json={"visibility": "private"})
    assert all(row["board"]["title"] is None for row in payloads(db, "kanban.board.updated")
               if row["board"]["visibility"] == "private")


def test_canvas_changes_through_the_api_name_the_token_owner(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin", "canvas"])
    create_hook(client, token, events=["canvas.updated", "canvas.deleted"])
    slug = call(client, "POST", "/canvas", token, json={"title": "Plan"}).json["canvas"]["slug"]
    call(client, "PUT", f"/canvas/{slug}", token, json={"visibility": "public"})
    call(client, "DELETE", f"/canvas/{slug}", token)
    updated = payloads(db, "canvas.updated")
    assert updated[-1]["canvas"]["visibility"] == "public" and updated[-1]["canvas"]["title"] == "Plan"
    assert [row["actor_id"] for row in updated + payloads(db, "canvas.deleted")] == [boss["id"]] * (len(updated) + 1)


# ── Prompt delivery ───────────────────────────────────────────────────────────


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_deliveries_are_sent_right_after_the_request(api_app, client, db, boss, monkeypatch):
    receiver = Receiver(200)
    monkeypatch.setattr(core_http, "request", receiver)
    api_app.config[webhooks.PROMPT_CONFIG] = True
    try:
        token = issue(api_app, boss, ["admin", "pages"])
        create_hook(client, token)
        assert call(client, "POST", "/pages", token, json={"title": "Fresh"}).status_code == 201
        assert wait_for(lambda: db.scalar("SELECT state FROM api_service__webhook_deliveries") == "delivered")
    finally:
        api_app.config.pop(webhooks.PROMPT_CONFIG)
    assert len(receiver.calls) == 1 and json.loads(receiver.calls[0]["body"])["event"] == "page.created"
    assert in_app(api_app, lambda: webhooks.deliver_due(transport=receiver)) == 0  # never twice
    assert len(receiver.calls) == 1


def test_prompt_delivery_is_off_in_tests_by_default(api_app, client, db, boss, monkeypatch):
    receiver = Receiver(200)
    monkeypatch.setattr(core_http, "request", receiver)
    token = issue(api_app, boss, ["admin", "pages"])
    create_hook(client, token)
    call(client, "POST", "/pages", token, json={"title": "Queued"})
    assert db.scalar("SELECT state FROM api_service__webhook_deliveries") == "pending" and receiver.calls == []


def test_deliver_now_claims_and_leaves_failing_webhooks_to_the_job(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)
    first = call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token).json["delivery"]["id"]
    receiver = Receiver(200)
    assert in_app(api_app, lambda: webhooks.deliver_now([first], transport=receiver)) == 1
    assert in_app(api_app, lambda: webhooks.deliver_now([first], transport=receiver)) == 0  # already delivered
    assert len(receiver.calls) == 1

    db.execute("UPDATE api_service__webhooks SET consecutive_failures = 2")
    second = call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token).json["delivery"]["id"]
    assert in_app(api_app, lambda: webhooks.deliver_now([second], transport=receiver)) == 0
    assert in_app(api_app, lambda: webhooks.deliver_now([second], even_if_failing=frozenset({second}),
                                                        transport=receiver)) == 1
    # A delivery the job has claimed is not sent again by the prompt path.
    third = call(client, "POST", f"/admin/webhooks/{hook['id']}/ping", token).json["delivery"]["id"]
    assert in_app(api_app, lambda: webhooks._claim(third))
    assert in_app(api_app, lambda: webhooks.deliver_now([third], even_if_failing=frozenset({third}),
                                                        transport=receiver)) == 0


# ── Automatic switch-off ──────────────────────────────────────────────────────


def fail_pings(app, client, db, token, hook_id, count):
    for _ in range(count):
        call(client, "POST", f"/admin/webhooks/{hook_id}/ping", token)
        db.execute("UPDATE api_service__webhook_deliveries SET next_attempt_at = datetime('now', '-1 second') "
                   "WHERE state = 'pending'")
        in_app(app, lambda: webhooks.deliver_due(transport=Receiver(core_http.HttpError("down"))))


def test_webhook_is_switched_off_after_repeated_failures(api_app, client, db, boss, login):
    db.execute("UPDATE site_settings SET api_service_webhook_max_failures = 3")
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)
    fail_pings(api_app, client, db, token, hook["id"], 2)
    state = call(client, "GET", f"/admin/webhooks/{hook['id']}", token).json["webhook"]
    assert state["active"] is True and state["consecutive_failures"] == 2 and state["failing_since"]
    fail_pings(api_app, client, db, token, hook["id"], 1)
    state = call(client, "GET", f"/admin/webhooks/{hook['id']}", token).json["webhook"]
    assert state["active"] is False and state["disabled_reason"] == "failures" and state["disabled_at"]

    counts = in_app(api_app, lambda: attention.counts(boss))
    assert counts[webhooks.ATTENTION_SOURCE] == 1
    login(client, boss)
    page = client.get("/admin/api-service")
    assert b"Switched off automatically" in page.data
    assert b"Switched off automatically" in client.get(f"/admin/api-service/webhooks/{hook['id']}").data
    assert db.scalar("SELECT COUNT(*) FROM attention_events WHERE source_id = ?", (webhooks.ATTENTION_SOURCE,)) == 1

    # Enabling it again starts over.
    enabled = call(client, "PUT", f"/admin/webhooks/{hook['id']}", token, json={"active": True}).json["webhook"]
    assert enabled["active"] is True and enabled["consecutive_failures"] == 0
    assert enabled["disabled_reason"] is None and enabled["failing_since"] is None
    assert in_app(api_app, lambda: attention.counts(boss))[webhooks.ATTENTION_SOURCE] == 0


def test_failing_for_days_switches_off_and_zero_means_never(api_app, client, db, boss):
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)
    db.execute("UPDATE api_service__webhooks SET consecutive_failures = 1, "
               "failing_since = datetime('now', '-4 days')")
    fail_pings(api_app, client, db, token, hook["id"], 1)
    assert db.one("SELECT active, disabled_reason FROM api_service__webhooks") == {"active": 0,
                                                                                  "disabled_reason": "failures"}
    # Switching it off by hand settles the notice without re-enabling it.
    call(client, "PUT", f"/admin/webhooks/{hook['id']}", token, json={"active": False})
    assert db.scalar("SELECT disabled_reason FROM api_service__webhooks") is None

    db.execute("UPDATE site_settings SET api_service_webhook_max_failures = 0")
    call(client, "PUT", f"/admin/webhooks/{hook['id']}", token, json={"active": True})
    db.execute("UPDATE api_service__webhooks SET failing_since = datetime('now', '-9 days')")
    fail_pings(api_app, client, db, token, hook["id"], 3)
    assert db.scalar("SELECT active FROM api_service__webhooks") == 1


def test_a_success_resets_the_failure_run(api_app, client, db, boss):
    db.execute("UPDATE site_settings SET api_service_webhook_max_failures = 3")
    token = issue(api_app, boss, ["admin"])
    hook = create_hook(client, token)
    fail_pings(api_app, client, db, token, hook["id"], 2)
    db.execute("UPDATE api_service__webhook_deliveries SET next_attempt_at = datetime('now', '-1 second') "
               "WHERE state = 'pending'")
    in_app(api_app, lambda: webhooks.deliver_due(transport=Receiver(200)))
    row = db.one("SELECT active, consecutive_failures, failing_since FROM api_service__webhooks")
    assert row == {"active": 1, "consecutive_failures": 0, "failing_since": None}


def test_failure_limit_setting(api_app, client, db, boss, login):
    token = issue(api_app, boss, ["settings"])
    changed = call(client, "PUT", "/settings", token, json={"api_service_webhook_max_failures": 50})
    assert changed.status_code == 200, changed.json
    assert db.scalar("SELECT api_service_webhook_max_failures FROM site_settings") == 50
    assert call(client, "PUT", "/settings", token, json={"api_service_webhook_max_failures": 5000}).status_code \
        == 200
    assert db.scalar("SELECT api_service_webhook_max_failures FROM site_settings") == 1000  # clamped
    assert call(client, "PUT", "/settings", token, json={"api_service_webhook_max_failures": "many"}).status_code \
        == 400
    login(client, boss)
    client.post("/admin/api-service/settings", data={"enabled": "1", "webhook_max_failures": "7"})
    assert db.scalar("SELECT api_service_webhook_max_failures FROM site_settings") == 7
    assert in_app(api_app, webhooks.max_failures) == 7


def test_sdk_helpers_build_the_right_requests(monkeypatch):
    sent = []
    wiki = WikiClient("https://wiki.example.org", "token")

    def fake(method, path, payload=None, **options):
        sent.append((method, path, payload))
        return {"checklist": [], "column": {}, "tickets": [], "webhook": {}, "webhooks": [], "next_offset": None}

    monkeypatch.setattr(wiki, "request", fake)
    wiki.add_checklist_item(4, "Write")
    wiki.update_checklist_item(9, done=True)
    wiki.reorder_checklist(4, [2, 1])
    wiki.delete_checklist_item(9)
    wiki.update_column(3, wip_limit=None)
    wiki.update_column(3, title="Doing")
    wiki.update_webhook(1, active=True)
    wiki.my_tickets()
    assert sent[:7] == [
        ("POST", "/kanban/tickets/4/checklist", {"text": "Write"}),
        ("PUT", "/kanban/checklist/9", {"done": True}),
        ("POST", "/kanban/tickets/4/checklist/reorder", {"order": [2, 1]}),
        ("DELETE", "/kanban/checklist/9", None),
        ("PUT", "/kanban/columns/3", {"wip_limit": None}),
        ("PUT", "/kanban/columns/3", {"title": "Doing"}),
        ("PUT", "/admin/webhooks/1", {"active": True}),
    ]
    assert sent[7][1] == "/kanban/my-tickets"
