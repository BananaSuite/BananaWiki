"""REST API conventions: rate-limit headers, Idempotency-Key, ETag/If-Match, pagination, page attachments,
and an OpenAPI description that matches the routes."""

from __future__ import annotations

import io
import json
import re

import pytest

from bananawiki.wiki.features.api_service import idempotency, openapi

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
    assert again.json == first.json
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE slug = 'once'") == 1
    # Another token of the same account shares the key; another account does not.
    other_token = issue(api_app, people["admin"])
    assert call(client, "POST", "/pages", other_token, json=body,
                headers={"Idempotency-Key": "abc-123"}).headers.get("Idempotent-Replayed") == "true"
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
    db.execute("INSERT INTO api_service__idempotency (user_id, idempotency_key, request_hash, created_at) "
               "VALUES (?, 'stuck', ?, datetime('now'))", (people["reader"]["id"], fingerprint))
    headers = {"Idempotency-Key": "stuck", "Content-Type": "application/json"}
    busy = call(client, "POST", "/pages", token, data=raw, headers=dict(headers))
    assert busy.status_code == 409 and busy.json["code"] == "idempotency_in_progress"
    db.execute("UPDATE api_service__idempotency SET created_at = datetime('now', '-1 hour') "
               "WHERE idempotency_key = 'stuck'")
    retried = call(client, "POST", "/pages", token, data=raw, headers=dict(headers))
    assert retried.status_code == 403 and "Idempotent-Replayed" not in retried.headers


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

    folder = api_service.__path__[0] + "/translations/"
    for language in ("en", "it"):
        with open(folder + f"{language}.json", encoding="utf-8") as handle:
            strings = json.load(handle)
        for endpoint in openapi.ENDPOINTS:
            assert f"api_service.endpoint.{endpoint.method.lower()} {endpoint.path}" in strings, endpoint.path
            assert f"api_service.docs.group.{endpoint.group}" in strings
    assert client.get("/api-docs").status_code == 200
