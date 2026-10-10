"""REST API conventions: rate-limit headers, Idempotency-Key, ETag/If-Match, pagination, page attachments,
and an OpenAPI description that matches the routes."""

from __future__ import annotations

import io
import json
import re

import pytest

from bananawiki.core.sqlite import DatabaseUnavailable
from bananawiki.wiki.features.api_service import audit, idempotency, openapi, serialize

from .api_support import call, enable_api, issue
from .pages_support import in_app, make_category, make_page, restrict, set_feature


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


@pytest.fixture
def people(make_user):
    return {
        "admin": make_user("boss", role="admin"),
        "editor": make_user("editor1", role="editor", api_access_enabled=1),
        "reader": make_user("reader1", api_access_enabled=1),
    }


def test_operator_maintenance_preserves_the_api_error_contract(app_factory, tmp_path):
    marker = tmp_path / "maintenance"
    marker.write_text("1")
    app = app_factory(environ={"BW_MAINTENANCE_FILE": str(marker)})
    client = app.test_client()
    response = client.post("/api/v1/pages", json={"title": "Wait"})
    assert response.status_code == 503 and response.json["ok"] is False
    assert response.json["code"] == "maintenance"
    assert response.json["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "30"
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 503


# ── Rate limit headers ────────────────────────────────────────────────────────


def test_rate_limit_headers(api_app, client, db, people):
    db.execute("UPDATE site_settings SET api_service_rate_limit = 3")
    token = issue(api_app, people["reader"])
    first = call(client, "GET", "/pages", token)
    assert first.headers["X-RateLimit-Limit"] == "3" and first.headers["X-RateLimit-Remaining"] == "2"
    assert 1 <= int(first.headers["X-RateLimit-Reset"]) <= 60
    assert call(client, "GET", "/pages", token).headers["X-RateLimit-Remaining"] == "1"
    call(client, "GET", "/pages", token)
    limited = call(client, "GET", "/pages", token)
    assert limited.status_code == 429 and limited.headers["X-RateLimit-Remaining"] == "0"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


# ── Idempotency-Key ───────────────────────────────────────────────────────────


def test_idempotent_post_is_replayed_not_repeated(api_app, client, db, people):
    token = issue(api_app, people["admin"])
    body = {"title": "Once", "content": "only once"}
    first = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "abc-123"})
    assert first.status_code == 201 and "Idempotent-Replayed" not in first.headers
    again = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "abc-123"})
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    assert again.headers["ETag"] == first.headers["ETag"]
    assert again.json == first.json
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE slug = 'once'") == 1
    # The key is bound to the token that used it: another token of the account gets no replay.
    other_token = issue(api_app, people["admin"])
    other = call(client, "POST", "/pages", other_token, json=body, headers={"Idempotency-Key": "abc-123"})
    assert other.status_code == 422 and other.json["code"] == "idempotency_key_reused"
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE slug LIKE 'once%'") == 1
    changed = call(client, "POST", "/pages", token, json={"title": "Twice"}, headers={"Idempotency-Key": "abc-123"})
    assert changed.status_code == 422 and changed.json["code"] == "idempotency_key_reused"
    invalid = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "bad key"})
    assert invalid.status_code == 400 and invalid.json["code"] == "idempotency_key_invalid"


def test_idempotency_keeps_errors_but_not_rate_limits(api_app, client, db, people):
    token = issue(api_app, people["reader"])
    refused = call(client, "POST", "/pages", token, json={"title": "Nope"}, headers={"Idempotency-Key": "k1"})
    assert refused.status_code == 403
    replay = call(client, "POST", "/pages", token, json={"title": "Nope"}, headers={"Idempotency-Key": "k1"})
    assert replay.status_code == 403 and replay.headers["Idempotent-Replayed"] == "true"
    # A reservation left by a request that never finished blocks retries only for a while.
    raw = b'{"title": "Nope"}'
    fingerprint = idempotency.request_hash("POST", "/api/v1/pages", b"", raw)
    token_id = db.scalar("SELECT id FROM api_service__tokens WHERE user_id = ?", (people["reader"]["id"],))
    db.execute("INSERT INTO api_service__idempotency (user_id, token_id, idempotency_key, request_hash, created_at) "
               "VALUES (?, ?, 'stuck', ?, datetime('now'))", (people["reader"]["id"], token_id, fingerprint))
    headers = {"Idempotency-Key": "stuck", "Content-Type": "application/json"}
    busy = call(client, "POST", "/pages", token, data=raw, headers=dict(headers))
    assert busy.status_code == 409 and busy.json["code"] == "idempotency_in_progress"
    db.execute("UPDATE api_service__idempotency SET created_at = datetime('now', '-1 hour') "
               "WHERE idempotency_key = 'stuck'")
    retried = call(client, "POST", "/pages", token, data=raw, headers=dict(headers))
    assert retried.status_code == 403 and "Idempotent-Replayed" not in retried.headers


def test_idempotency_never_stores_secrets(api_app, client, db, people):
    """New tokens and webhook secrets are not kept for replays: a retry is refused, never run again."""
    token = issue(api_app, people["admin"], ["tokens", "pages", "admin"])
    minted = call(client, "POST", "/tokens", token, json={"permissions": {"read": True, "scopes": ["pages"]}},
                  headers={"Idempotency-Key": "mint-1"})
    hook = call(client, "POST", "/admin/webhooks", token, json={"url": "https://example.org/hook",
                                                                "events": ["page.created"]},
                headers={"Idempotency-Key": "hook-1"})
    rotated = call(client, "POST", f"/admin/webhooks/{hook.json['webhook']['id']}/rotate-secret", token,
                   headers={"Idempotency-Key": "rotate-1"})
    assert (minted.status_code, hook.status_code, rotated.status_code) == (201, 201, 200)
    rows = db.all("SELECT idempotency_key, status_code, response_body FROM api_service__idempotency "
                  "ORDER BY idempotency_key")
    assert [(row["idempotency_key"], row["status_code"], row["response_body"]) for row in rows] == [
        ("hook-1", 201, None), ("mint-1", 201, None), ("rotate-1", 200, None)]
    for key, path, body, status in (("mint-1", "/tokens", {"permissions": {"read": True, "scopes": ["pages"]}}, 201),
                                    ("rotate-1", f"/admin/webhooks/{hook.json['webhook']['id']}/rotate-secret",
                                     None, 200)):
        retry = call(client, "POST", path, token, json=body, headers={"Idempotency-Key": key})
        assert retry.status_code == 409 and retry.json["code"] == "idempotency_replay_unavailable"
        assert retry.json["status"] == status and "Idempotent-Replayed" not in retry.headers
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 2
    # Refusals carry no secret and are replayed as before.
    refused = {"permissions": {"read": True, "scopes": ["users"]}}
    assert call(client, "POST", "/tokens", token, json=refused, headers={"Idempotency-Key": "mint-2"}).status_code == 403
    again = call(client, "POST", "/tokens", token, json=refused, headers={"Idempotency-Key": "mint-2"})
    assert again.status_code == 403 and again.headers["Idempotent-Replayed"] == "true"


def test_idempotent_page_replay_applies_current_category_access(api_app, client, db, people):
    category = make_category(api_app, "Restricted later")
    user = people["admin"]
    token = issue(api_app, user)
    body = {"title": "Previously readable", "content": "private text", "category_id": category["id"]}
    headers = {"Idempotency-Key": "page-access"}
    first = call(client, "POST", "/pages", token, json=body, headers=dict(headers))
    assert first.status_code == 201
    db.execute("UPDATE users SET role = 'editor', api_access_enabled = 1 WHERE id = ?", (user["id"],))
    restrict(db, user, read=[], write=[])
    assert call(client, "GET", "/pages/previously-readable", token).status_code == 404
    retry = call(client, "POST", "/pages", token, json=body, headers=dict(headers))
    assert retry.status_code == 409 and retry.json["code"] == "idempotency_replay_unavailable"
    assert b"private text" not in retry.data
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Previously readable'") == 1


@pytest.mark.parametrize("kind", ["canvas", "kanban"])
def test_idempotent_replay_applies_current_private_resource_ownership(api_app, client, db, people, kind):
    set_feature(api_app, kind, True)
    user = people["admin"]
    token = issue(api_app, user, [kind])
    body = {"title": "Private resource"}
    headers = {"Idempotency-Key": "private-resource"}
    path = "/canvas" if kind == "canvas" else "/kanban/boards"
    first = call(client, "POST", path, token, json=body, headers=dict(headers))
    assert first.status_code == 201
    db.execute("UPDATE users SET role = 'editor', api_access_enabled = 1 WHERE id = ?", (user["id"],))
    if kind == "canvas":
        resource = first.json["canvas"]
        db.execute("UPDATE canvas__layouts SET creator_id = ?, visibility = 'private' WHERE id = ?",
                   (people["editor"]["id"], resource["id"]))
    else:
        resource = first.json["board"]
        db.execute("UPDATE kanban_boards SET created_by = ?, visibility = 'private' WHERE id = ?",
                   (people["editor"]["id"], resource["id"]))
    retry = call(client, "POST", path, token, json=body, headers=dict(headers))
    assert retry.status_code == 409 and retry.json["code"] == "idempotency_replay_unavailable"
    assert b"Private resource" not in retry.data


def test_administrator_response_replay_rejects_a_demoted_account(api_app, client, db, people):
    user = people["admin"]
    token = issue(api_app, user, ["users"])
    body = {"username": "created_member", "password": "long enough password"}
    headers = {"Idempotency-Key": "administrator-response"}
    assert call(client, "POST", "/users", token, json=body, headers=dict(headers)).status_code == 201
    db.execute("UPDATE users SET role = 'editor', api_access_enabled = 1 WHERE id = ?", (user["id"],))
    retry = call(client, "POST", "/users", token, json=body, headers=dict(headers))
    assert retry.status_code == 403 and retry.json["code"] == "admin_required"
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'created_member'") == 1


def test_idempotent_category_replay_applies_current_access(api_app, client, db, people):
    user = people["admin"]
    token = issue(api_app, user, ["categories"])
    body = {"name": "Private category"}
    headers = {"Idempotency-Key": "category-access"}
    first = call(client, "POST", "/categories", token, json=body, headers=dict(headers))
    assert first.status_code == 201
    db.execute("UPDATE users SET role = 'editor', api_access_enabled = 1 WHERE id = ?", (user["id"],))
    restrict(db, user, read=[], write=[])
    retry = call(client, "POST", "/categories", token, json=body, headers=dict(headers))
    assert retry.status_code == 409 and retry.json["code"] == "idempotency_replay_unavailable"
    assert b"Private category" not in retry.data


@pytest.mark.parametrize("kind", ["column", "ticket", "comment", "checklist"])
def test_idempotent_kanban_child_response_applies_current_board_access(api_app, client, db, people, kind):
    set_feature(api_app, "kanban", True)
    user = people["admin"]
    token = issue(api_app, user, ["kanban"])
    board = call(client, "POST", "/kanban/boards", token, json={"title": "Restricted board"}).json["board"]
    path = f"/kanban/boards/{board['id']}/columns"
    body = {"title": "Private child content"}
    if kind != "column":
        column = call(client, "POST", path, token, json={"title": "Column"}).json["column"]
        path = f"/kanban/columns/{column['id']}/tickets"
    if kind in ("comment", "checklist"):
        ticket = call(client, "POST", path, token, json={"title": "Ticket"}).json["ticket"]
        path = f"/kanban/tickets/{ticket['id']}/{'comments' if kind == 'comment' else 'checklist'}"
        body = {"content" if kind == "comment" else "text": "Private child content"}
    headers = {"Idempotency-Key": "child-access"}
    first = call(client, "POST", path, token, json=body, headers=dict(headers))
    assert first.status_code == 201
    db.execute("UPDATE users SET role = 'editor', api_access_enabled = 1 WHERE id = ?", (user["id"],))
    db.execute("UPDATE kanban_boards SET created_by = ?, visibility = 'private' WHERE id = ?",
               (people["editor"]["id"], board["id"]))
    retry = call(client, "POST", path, token, json=body, headers=dict(headers))
    assert retry.status_code == 409 and retry.json["code"] == "idempotency_replay_unavailable"
    assert b"Private child content" not in retry.data


def test_idempotency_keeps_success_when_the_answer_exceeds_the_storage_budget(api_app, client, db, people,
                                                                           monkeypatch):
    monkeypatch.setattr(idempotency, "MAX_STORED_BODY", 32)
    token = issue(api_app, people["admin"])
    body = {"title": "Large answer", "content": "A response larger than the replay budget."}
    first = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "large-answer"})
    assert first.status_code == 201
    stored = db.one("SELECT status_code, response_body FROM api_service__idempotency "
                    "WHERE idempotency_key = 'large-answer'")
    assert stored == {"status_code": 201, "response_body": None}
    replay = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "large-answer"})
    assert replay.status_code == 409 and replay.json["code"] == "idempotency_replay_unavailable"
    assert replay.json["status"] == 201
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Large answer'") == 1


@pytest.mark.parametrize("component, method", [(audit, "record"), (idempotency, "finish"),
                                                (serialize, "page_full")])
def test_idempotent_write_and_response_record_are_atomic(api_app, client, db, people, monkeypatch,
                                                        component, method):
    """A failure after a service writes must leave no mutation for a retry to repeat."""
    original = getattr(component, method)
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("response recording failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(component, method, fail_once)
    monkeypatch.setitem(api_app.config, "PROPAGATE_EXCEPTIONS", False)
    token = issue(api_app, people["admin"])
    body = {"title": "Atomic create"}
    first = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "atomic-create"})
    assert first.status_code == 500
    assert first.json["ok"] is False and first.json["code"] == "internal_error"
    assert first.json["request_id"] == first.headers["X-Request-ID"]
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Atomic create'") == 0
    assert db.scalar("SELECT COUNT(*) FROM api_service__idempotency WHERE idempotency_key = 'atomic-create'") == 0
    retry = call(client, "POST", "/pages", token, json=body, headers={"Idempotency-Key": "atomic-create"})
    assert retry.status_code == 201
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Atomic create'") == 1


def test_interrupted_idempotent_write_is_rolled_back_at_teardown(api_app, client, db, people, monkeypatch):
    def interrupted(_page):
        raise SystemExit("worker interrupted")

    monkeypatch.setattr(serialize, "page_full", interrupted)
    token = issue(api_app, people["admin"])
    with pytest.raises(SystemExit):
        call(client, "POST", "/pages", token, json={"title": "Interrupted"},
             headers={"Idempotency-Key": "interrupted"})
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Interrupted'") == 0
    assert db.scalar("SELECT COUNT(*) FROM api_service__idempotency WHERE idempotency_key = 'interrupted'") == 0


def test_api_storage_failure_has_a_stable_error_without_reading_translations(api_app, client, monkeypatch):
    from bananawiki.wiki import settings

    monkeypatch.setattr(settings, "load", lambda: (_ for _ in ()).throw(DatabaseUnavailable("private-storage-path")))
    response = call(client, "GET", "/pages", "unverified-token")
    assert response.status_code == 503
    assert response.json["ok"] is False and response.json["code"] == "storage_unavailable"
    assert response.json["request_id"] == response.headers["X-Request-ID"]
    assert "private-storage-path" not in response.get_data(as_text=True)
    assert response.cache_control.no_store


@pytest.mark.parametrize("method, path, status, code", [("GET", "/unknown", 404, "not_found"),
                                                        ("DELETE", "/status", 405, "method_not_allowed")])
def test_framework_api_errors_use_the_same_error_shape(api_app, client, method, path, status, code):
    response = call(client, method, path)
    assert response.status_code == status
    assert response.json["ok"] is False and response.json["code"] == code
    assert response.json["request_id"] == response.headers["X-Request-ID"]
    assert response.cache_control.no_store
    if status == 405:
        assert "GET" in response.headers["Allow"]


def test_idempotent_delete_preserves_attachment_files_until_commit(api_app, client, db, people, monkeypatch):
    from bananawiki.wiki import storage

    page = make_page(api_app, "Atomic delete")
    token = issue(api_app, people["admin"])
    attachment = upload(client, token, page["slug"]).json["attachment"]
    filename = db.scalar("SELECT filename FROM page_attachments WHERE id = ?", (attachment["id"],))
    path = in_app(api_app, lambda: storage.resolve("attachments", filename))
    original = idempotency.finish
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("response recording failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(idempotency, "finish", fail_once)
    monkeypatch.setitem(api_app.config, "PROPAGATE_EXCEPTIONS", False)
    body = {"slugs": [page["slug"]]}
    headers = {"Idempotency-Key": "atomic-delete"}
    first = call(client, "POST", "/pages/bulk-delete", token, json=body, headers=dict(headers))
    assert first.status_code == 500
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM page_attachments WHERE id = ?", (attachment["id"],)) == 1
    assert path.read_bytes() == b"data"
    retry = call(client, "POST", "/pages/bulk-delete", token, json=body, headers=dict(headers))
    assert retry.status_code == 200 and retry.json["deleted"] == 1
    assert not path.exists()


def test_idempotency_rows_without_a_token_are_dropped_by_the_upgrade(api_app, db, people):
    from bananawiki.wiki.features.api_service import schema

    db.execute("INSERT INTO api_service__idempotency (user_id, idempotency_key, request_hash, status_code, "
               "response_body, created_at) VALUES (?, 'legacy', 'x', 201, '{\"token\": \"raw\"}', datetime('now'))",
               (people["admin"]["id"],))
    schema.upgrade_v4(db.conn)
    assert db.scalar("SELECT COUNT(*) FROM api_service__idempotency") == 0


def test_idempotency_prune(api_app, db, people):
    db.execute("INSERT INTO api_service__idempotency (user_id, idempotency_key, request_hash, status_code, "
               "response_body, created_at) VALUES (?, 'old', 'x', 200, '{}', datetime('now', '-2 days'))",
               (people["admin"]["id"],))
    assert in_app(api_app, idempotency.prune) == 1


# ── ETag / If-Match ───────────────────────────────────────────────────────────


def test_page_etag_and_if_match(api_app, client, people):
    make_page(api_app, "Doc", "v1")
    token = issue(api_app, people["admin"])
    got = call(client, "GET", "/pages/doc", token)
    etag = got.headers["ETag"]
    assert etag == f'"r{got.json["page"]["revision"]}"'
    saved = call(client, "PUT", "/pages/doc", token, json={"content": "v2"}, headers={"If-Match": etag})
    assert saved.status_code == 200 and saved.headers["ETag"] != etag
    stale = call(client, "PUT", "/pages/doc", token, json={"content": "v3"}, headers={"If-Match": etag})
    assert stale.status_code == 412 and stale.json["code"] == "precondition_failed"
    assert stale.json["revision"] == saved.json["page"]["revision"]
    listed = call(client, "PUT", "/pages/doc", token, json={"content": "v3"},
                  headers={"If-Match": f'"r0", {saved.headers["ETag"]}'})
    assert listed.status_code == 200
    assert call(client, "PUT", "/pages/doc", token, json={"content": "v4"}, headers={"If-Match": "*"}).status_code \
        == 200
    assert call(client, "GET", "/pages/doc", token).json["page"]["content"] == "v4"


# ── Pagination ────────────────────────────────────────────────────────────────


def test_history_search_and_audit_are_paginated(api_app, client, people):
    make_page(api_app, "Paged", "alpha")
    token = issue(api_app, people["admin"], ["pages", "admin"])
    for number in range(3):
        call(client, "PUT", "/pages/paged", token, json={"content": f"alpha {number}"})
    first = call(client, "GET", "/pages/paged/history?limit=2", token).json
    assert len(first["history"]) == 2 and first["next_offset"] == 2
    rest = call(client, "GET", "/pages/paged/history?limit=2&offset=2", token).json
    assert rest["history"] and rest["next_offset"] is None
    search = call(client, "GET", "/search?q=alpha&limit=5", token).json
    assert search["next_offset"] is None and search["limit"] == 5
    audit = call(client, "GET", "/admin/audit-log?limit=1", token).json
    assert len(audit["entries"]) == 1 and audit["next_offset"] == 1 and audit["total"] >= 2


# ── Page attachments ──────────────────────────────────────────────────────────


def upload(client, token, slug, content=b"data", name="notes.txt"):
    return client.post(f"/api/v1/pages/{slug}/attachments", headers={"Authorization": f"Bearer {token}"},
                       data={"file": (io.BytesIO(content), name)}, content_type="multipart/form-data")


def test_page_attachments(api_app, client, db, people):
    open_cat, secret_cat = make_category(api_app, "Open"), make_category(api_app, "Secret")
    make_page(api_app, "Shared", category_id=open_cat["id"])
    make_page(api_app, "Hidden", category_id=secret_cat["id"])
    restrict(db, people["reader"], read=[open_cat["id"]])
    admin, reader = issue(api_app, people["admin"]), issue(api_app, people["reader"])

    created = upload(client, admin, "shared")
    assert created.status_code == 201, created.json
    attachment = created.json["attachment"]
    assert attachment["name"] == "notes.txt" and attachment["uploader"] == "boss"
    listed = call(client, "GET", "/pages/shared/attachments", reader).json["attachments"]
    assert [row["id"] for row in listed] == [attachment["id"]]
    download = call(client, "GET", f"/pages/shared/attachments/{attachment['id']}", reader)
    assert download.status_code == 200 and download.data == b"data"

    assert upload(client, reader, "shared").json["code"] == "cannot_upload"
    assert call(client, "DELETE", f"/pages/shared/attachments/{attachment['id']}", reader).status_code == 403
    assert call(client, "GET", "/pages/hidden/attachments", reader).status_code == 404
    hidden = upload(client, admin, "hidden").json["attachment"]
    # An attachment id of another page is not reachable through a readable page.
    assert call(client, "GET", f"/pages/shared/attachments/{hidden['id']}", reader).status_code == 404
    multipart_key = client.post("/api/v1/pages/shared/attachments",
                                headers={"Authorization": f"Bearer {admin}", "Idempotency-Key": "file-1"},
                                data={"file": (io.BytesIO(b"x"), "x.txt")}, content_type="multipart/form-data")
    assert multipart_key.json["code"] == "idempotency_unsupported"

    assert call(client, "DELETE", f"/pages/shared/attachments/{attachment['id']}", admin).status_code == 200
    assert call(client, "GET", f"/pages/shared/attachments/{attachment['id']}", admin).status_code == 404
    set_feature(api_app, "attachments", False)
    assert call(client, "GET", "/pages/shared/attachments", admin).status_code == 404


# ── OpenAPI ───────────────────────────────────────────────────────────────────


def _routes(app) -> set[tuple[str, str]]:
    found = set()
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith("api_v1."):
            continue
        path = re.sub(r"<(?:[a-z]+:)?([a-z_]+)>", r"{\1}", rule.rule.removeprefix("/api/v1"))
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            found.add((method, path))
    return found


def test_openapi_lists_exactly_the_routes(api_app):
    described = {(endpoint.method, endpoint.path) for endpoint in openapi.ENDPOINTS}
    assert described == _routes(api_app)


def test_openapi_scopes_match_the_views(api_app):
    views = api_app.view_functions
    for rule in api_app.url_map.iter_rules():
        if not rule.endpoint.startswith("api_v1."):
            continue
        path = re.sub(r"<(?:[a-z]+:)?([a-z_]+)>", r"{\1}", rule.rule.removeprefix("/api/v1"))
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            endpoint = next(e for e in openapi.ENDPOINTS if (e.method, e.path) == (method, path))
            scope = getattr(views[rule.endpoint], "_api_scope", None)
            assert (endpoint.scope, endpoint.write) == (scope or (None, False)), (method, path)
            assert endpoint.feature == getattr(views[rule.endpoint], "_api_feature", None), (method, path)


def test_openapi_references_resolve_and_endpoints_are_translated(api_app, client):
    spec = call(client, "GET", "/openapi.json").json
    text = json.dumps(spec)
    for ref in set(re.findall(r'"\$ref": "#/components/([a-z]+)/([A-Za-z]+)"', text)):
        assert ref[1] in spec["components"][ref[0]], ref
    assert spec["paths"]["/pages/{slug}"]["put"]["responses"]["412"]
    assert spec["paths"]["/kanban/boards/{board_id}"]["get"]["parameters"][0]["schema"] == {"type": "integer"}
    operation_ids = [op["operationId"] for path in spec["paths"].values() for op in path.values()]
    assert len(operation_ids) == len(set(operation_ids))
    from bananawiki.wiki.features import api_service
    from bananawiki.wiki.i18n import BUILTIN_LANGUAGES

    folder = api_service.__path__[0] + "/translations/"
    for language in BUILTIN_LANGUAGES:
        with open(folder + f"{language}.json", encoding="utf-8") as handle:
            strings = json.load(handle)
        for endpoint in openapi.ENDPOINTS:
            assert f"api_service.endpoint.{endpoint.method.lower()} {endpoint.path}" in strings, endpoint.path
            assert f"api_service.docs.group.{endpoint.group}" in strings
    assert client.get("/api-docs").status_code == 200
