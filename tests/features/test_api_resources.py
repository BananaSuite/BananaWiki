"""REST API resources: pages, categories, accounts and settings follow the web interface's rules."""

from __future__ import annotations

import pytest

from .api_support import call, enable_api, issue
from .pages_support import make_category, make_page, restrict, set_feature


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


@pytest.fixture
def world(api_app, db, people):
    """Two categories; the editor may only read and write 'Open'."""
    open_cat = make_category(api_app, "Open")
    secret_cat = make_category(api_app, "Secret")
    make_page(api_app, "Open page", "hello world", category_id=open_cat["id"])
    make_page(api_app, "Secret page", "classified", category_id=secret_cat["id"])
    restrict(db, people["editor"], read=[open_cat["id"]], write=[open_cat["id"]])
    restrict(db, people["reader"], read=[open_cat["id"]])
    return {"open": open_cat, "secret": secret_cat}


# ── Pages ─────────────────────────────────────────────────────────────────────


def test_restricted_pages_are_404_and_left_out(api_app, client, world, people):
    token = issue(api_app, people["editor"])
    slugs = [page["slug"] for page in call(client, "GET", "/pages", token).json["pages"]]
    assert "open-page" in slugs and "secret-page" not in slugs
    assert call(client, "GET", "/pages/secret-page", token).status_code == 404
    assert call(client, "GET", "/pages/open-page", token).json["page"]["content"] == "hello world"
    results = call(client, "GET", "/search?q=classified", token).json["results"]
    assert results == []
    admin_slugs = [p["slug"] for p in call(client, "GET", "/pages", issue(api_app, people["admin"])).json["pages"]]
    assert "secret-page" in admin_slugs


def test_writes_outside_writable_categories_are_403(api_app, client, world, people):
    token = issue(api_app, people["editor"])
    assert call(client, "POST", "/pages", token, json={"title": "X", "category_id": world["secret"]["id"]}) \
        .status_code == 403
    assert call(client, "PUT", "/pages/open-page", token, json={"category_id": world["secret"]["id"]}) \
        .status_code == 403
    assert call(client, "POST", "/pages", token, json={"title": "X", "category_id": 99999}).status_code == 400
    created = call(client, "POST", "/pages", token, json={"title": "Fresh", "content": "c",
                                                           "category_id": world["open"]["id"]})
    assert created.status_code == 201 and created.json["page"]["slug"] == "fresh"


def test_readers_cannot_write(api_app, client, world, people):
    token = issue(api_app, people["reader"])
    assert call(client, "POST", "/pages", token, json={"title": "Nope"}).json["code"] == "cannot_create"
    assert call(client, "PUT", "/pages/open-page", token, json={"content": "x"}).json["code"] == "cannot_edit"


def test_create_validation_and_slug_conflicts(api_app, client, world, people):
    token = issue(api_app, people["admin"])
    assert call(client, "POST", "/pages", token, json={"title": "Open page"}).status_code == 409
    assert call(client, "POST", "/pages", token, json={"title": "Other", "slug": "open-page"}).status_code == 409
    assert call(client, "POST", "/pages", token, json={"title": ""}).json["field"] == "title"
    assert call(client, "POST", "/pages", token, json={"title": "t" * 201}).status_code == 400
    assert call(client, "POST", "/pages", token, json={"title": "x", "category_id": True}).status_code == 400
    assert call(client, "POST", "/pages", token, json={"title": "x", "category_id": 2**70}).status_code == 400
    assert call(client, "POST", "/pages", token, json=["not", "an", "object"]).json["code"] == "body_not_object"
    assert call(client, "POST", "/pages", token, json={"title": "x", "slug": "admin"}).json["code"] == "slug_reserved"


def test_update_history_and_edit_conflict(api_app, client, world, people):
    token = issue(api_app, people["admin"])
    page = call(client, "GET", "/pages/open-page", token).json["page"]
    saved = call(client, "PUT", "/pages/open-page", token, json={"content": "v2", "expected_revision": page["revision"]})
    assert saved.status_code == 200 and saved.json["page"]["content"] == "v2"
    stale = call(client, "PUT", "/pages/open-page", token, json={"content": "v3", "expected_revision": page["revision"]})
    assert stale.status_code == 409 and stale.json["code"] == "edit_conflict"
    history = call(client, "GET", "/pages/open-page/history", token).json["history"]
    assert len(history) == 2
    entry = call(client, "GET", f"/history/{history[-1]['id']}", token).json["entry"]
    assert entry["content"] == "hello world"


def test_history_follows_read_access_and_permission(api_app, client, db, world, people):
    admin_token = issue(api_app, people["admin"])
    secret_history = call(client, "GET", "/pages/secret-page/history", admin_token).json["history"]
    token = issue(api_app, people["editor"])
    assert call(client, "GET", f"/history/{secret_history[0]['id']}", token).status_code == 404
    set_feature(api_app, "page_history", False)
    assert call(client, "GET", "/pages/open-page/history", token).json["code"] == "cannot_view_history"


def test_protected_pages_are_409(api_app, client, db, world, people):
    set_feature(api_app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE slug = 'open-page'",
               (people["admin"]["id"],))
    token = issue(api_app, people["editor"])
    response = call(client, "PUT", "/pages/open-page", token, json={"content": "overwrite"})
    assert response.status_code == 409 and response.json["code"] == "page_locked"


def test_delete_needs_page_delete_permission(api_app, client, db, world, people):
    token = issue(api_app, people["editor"])
    assert call(client, "DELETE", "/pages/open-page", token).json["code"] == "cannot_delete"
    db.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (people["editor"]["id"],))
    restrict(db, people["editor"], keys={"page.view_all", "page.edit_all", "page.delete"},
             read=[world["open"]["id"]], write=[world["open"]["id"]])
    assert call(client, "DELETE", "/pages/open-page", token).json["deleted"] is True
    assert call(client, "DELETE", "/pages/home", issue(api_app, people["admin"])).json["code"] == "cannot_delete_home"


def test_deletion_slowdown_answers_202(api_app, client, db, world, people):
    set_feature(api_app, "deletion_slowdown", True)
    token = issue(api_app, people["admin"])
    response = call(client, "DELETE", "/pages/open-page", token)
    assert response.status_code == 202 and response.json["pending_deletion"] is True
    assert db.scalar("SELECT pending_deletion FROM pages WHERE slug = 'open-page'") == 1
    assert call(client, "DELETE", "/pages/open-page", token).json["code"] == "already_pending"


def test_bulk_endpoints_are_for_admins_and_validate_first(api_app, client, db, world, people):
    editor = issue(api_app, people["editor"])
    assert call(client, "POST", "/pages/bulk", editor, json={"pages": [{"title": "a"}]}).status_code == 403
    token = issue(api_app, people["admin"])
    bad = call(client, "POST", "/pages/bulk", token, json={"pages": [{"title": "Good"}, {"title": ""}]})
    assert bad.status_code == 400 and db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Good'") == 0
    made = call(client, "POST", "/pages/bulk", token, json={"pages": [{"title": "One"}, {"title": "Open page"}]})
    assert made.status_code == 201 and len(made.json["created"]) == 1 and made.json["errors"][0]["code"] == "slug_taken"
    edited = call(client, "POST", "/pages/bulk-edit", token, json={"edits": [{"slug": "one", "content": "new"},
                                                                             {"slug": "missing", "content": "x"}]})
    assert edited.json["updated"] == 1 and edited.json["errors"][0]["code"] == "page_not_found"
    removed = call(client, "POST", "/pages/bulk-delete", token, json={"slugs": ["one", "home", "missing"]})
    assert (removed.json["deleted"], removed.json["skipped"], removed.json["not_found"]) == (1, 1, 1)
    assert call(client, "POST", "/pages/bulk-delete", token, json={"ids": ["1" * 30]}).status_code == 400


def test_page_events_fire_for_api_writes(api_app, client, world, people):
    from bananawiki.wiki import registry

    seen = []
    reg = api_app.extensions["bananawiki.registry"]
    reg.add(registry.Feature(id="event_probe", name="p", toggle="always",
                             events={"page.created": [lambda page, author_id: seen.append(page["slug"])]}))
    call(client, "POST", "/pages", issue(api_app, people["admin"]), json={"title": "Evented"})
    assert seen == ["evented"]


# ── Categories ────────────────────────────────────────────────────────────────


def test_categories_follow_read_restrictions(api_app, client, world, people):
    token = issue(api_app, people["reader"], ["categories"])
    names = [c["name"] for c in call(client, "GET", "/categories", token).json["categories"]]
    assert names == ["Open"]
    assert call(client, "GET", f"/categories/{world['secret']['id']}", token).status_code == 404


def test_category_changes_need_their_permissions(api_app, client, db, world, people):
    token = issue(api_app, people["editor"], ["categories"])
    assert call(client, "POST", "/categories", token, json={"name": "Top"}).json["code"] == "cannot_manage_category"
    restrict(db, people["editor"], keys={"category.create", "category.edit", "page.view_all"})
    admin = issue(api_app, people["admin"], ["categories"])
    child = call(client, "POST", "/categories", admin, json={"name": "Child", "parent_id": world["open"]["id"]})
    assert child.status_code == 201
    assert call(client, "PUT", f"/categories/{world['open']['id']}", admin,
                json={"parent_id": child.json["category"]["id"]}).json["code"] == "category_cycle"
    assert call(client, "PUT", f"/categories/{world['open']['id']}", admin, json={"parent_id": 999}).status_code == 400
    assert call(client, "PUT", f"/categories/{world['open']['id']}", admin, json={"name": "n" * 101}).status_code == 400
    renamed = call(client, "PUT", f"/categories/{world['open']['id']}", admin,
                   json={"name": "Opened", "sequential_nav": True})
    assert renamed.json["category"]["name"] == "Opened" and renamed.json["category"]["sequential_nav"] is True
    assert call(client, "PUT", f"/categories/{child.json['category']['id']}", token,
                json={"parent_id": None}).json["code"] == "cannot_manage_category"
    assert call(client, "DELETE", f"/categories/{world['secret']['id']}", admin).status_code == 200


# ── Accounts ──────────────────────────────────────────────────────────────────


def test_account_creation_rules(api_app, client, people):
    token = issue(api_app, people["admin"], ["users"])
    assert call(client, "POST", "/users", token, json={"username": "ab", "password": "long enough pw"}) \
        .status_code == 400
    assert call(client, "POST", "/users", token, json={"username": "valid_name", "password": "short"}).status_code == 400
    assert call(client, "POST", "/users", token, json={"username": "valid_name", "password": "long enough pw",
                                                       "role": "owner"}).status_code == 400
    created = call(client, "POST", "/users", token, json={"username": "valid_name", "password": "long enough pw"})
    assert created.status_code == 201 and "password" not in created.json["user"]
    assert call(client, "POST", "/users", token, json={"username": "VALID_NAME", "password": "long enough pw"}) \
        .status_code == 409
    too_many = [{"username": f"user_{i}", "password": "long enough pw"} for i in range(21)]
    assert call(client, "POST", "/users/bulk", token, json={"users": too_many}).status_code == 400
    assert call(client, "POST", "/users/bulk", token, json={"users": ["x"]}).status_code == 400
    bulk = call(client, "POST", "/users/bulk", token, json={"users": too_many[:2] + [{"username": "!"}]})
    assert len(bulk.json["created"]) == 2 and len(bulk.json["errors"]) == 1


def test_account_updates_and_protections(api_app, client, db, people, make_user):
    token = issue(api_app, people["admin"], ["users", "pages"])
    reader = people["reader"]
    reader_token = issue(api_app, reader)
    assert call(client, "PUT", f"/users/{reader['id']}", token, json={"suspended": "false"}).status_code == 400
    changed = call(client, "PUT", f"/users/{reader['id']}", token, json={"password": "a brand new password"})
    assert changed.json["api_tokens_revoked"] == 1
    assert call(client, "GET", "/pages", reader_token).status_code == 401
    assert call(client, "PUT", f"/users/{reader['id']}", token, json={"role": "owner"}).status_code == 403
    assert call(client, "PUT", f"/users/{people['admin']['id']}", token, json={"role": "user"}).json["code"] \
        == "last_admin"
    owner = make_user("the_owner", role="owner")
    assert call(client, "PUT", f"/users/{owner['id']}", token, json={"suspended": True}).status_code == 403
    assert call(client, "DELETE", f"/users/{owner['id']}", token).status_code == 403
    db.execute("UPDATE users SET is_superuser = 1 WHERE id = ?", (people["editor"]["id"],))
    assert call(client, "PUT", f"/users/{people['editor']['id']}", token, json={"suspended": True}).status_code == 403
    suspended = call(client, "PUT", f"/users/{reader['id']}", token, json={"suspended": True, "api_access_enabled": 0})
    assert suspended.json["user"]["suspended"] is True and suspended.json["user"]["api_access_enabled"] is False
    assert call(client, "DELETE", f"/users/{reader['id']}", token).json["deleted"] is True
    assert call(client, "GET", f"/users/{reader['id']}", token).status_code == 404


# ── Settings ──────────────────────────────────────────────────────────────────


def test_settings_hide_secrets_and_round_trip(api_app, client, db, people):
    db.execute("UPDATE site_settings SET tts_gpu_auth_token = 'fernet:abc'")
    token = issue(api_app, people["admin"], ["settings"])
    current = call(client, "GET", "/settings", token).json["settings"]
    assert "tts_gpu_auth_token" not in current and "banana_mode" not in current
    current["site_name"] = "Round Trip"
    response = call(client, "PUT", "/settings", token, json=current)
    assert response.status_code == 200 and response.json["updated"] == ["site_name"]


@pytest.mark.parametrize("key, value, status", [
    ("setup_done", 0, 403), ("last_server_restart_at", "2025-01-01 00:00:00", 403), ("tts_gpu_url", "http://10.0.0.1", 403),
    ("tts_gpu_auth_token", "guess", 403), ("banana_mode", 1, 403), ("no_such_setting", 1, 400),
    ("api_service_rate_limit", "fast", 400), ("primary_color", "red", 400), ("timezone", "Mars/Base", 400),
    ("public_mode_until", "2001-01-01T00:00:00Z", 400),
])
def test_settings_refusals(api_app, client, people, key, value, status):
    token = issue(api_app, people["admin"], ["settings"])
    response = call(client, "PUT", "/settings", token, json={key: value, "site_name": "Changed"})
    assert response.status_code == status
    assert key in (response.json.get("refused") or response.json.get("invalid"))
    assert call(client, "GET", "/settings", token).json["settings"]["site_name"] != "Changed"


def test_settings_feature_gates_platform_rules_and_clamping(api_app, client, db, people):
    token = issue(api_app, people["admin"], ["settings"])
    set_feature(api_app, "kanban", False)
    assert "kanban_access" in call(client, "PUT", "/settings", token, json={"kanban_access": "all"}).json["refused"]
    clamped = call(client, "PUT", "/settings", token, json={"api_service_max_tokens_per_user": 5000})
    assert clamped.status_code == 200
    assert db.scalar("SELECT api_service_max_tokens_per_user FROM site_settings") == 100
    call(client, "PUT", "/settings", token, json={"public_mode": True, "public_mode_until": "2999-01-01T00:00:00Z"})
    assert db.scalar("SELECT public_mode_until FROM site_settings") == "2999-01-01 00:00:00"
    call(client, "PUT", "/settings", token, json={"public_mode": False})
    assert db.scalar("SELECT public_mode_until FROM site_settings") is None


def test_settings_platform_restrictions(app_factory):
    app = app_factory(environ={"BW_FORBID_PUBLIC_MODE": "1"})
    from bananawiki.core.sqlite import Session
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    from .api_support import enable_api

    db = Session(app.extensions["bananawiki.database"].connect())
    enable_api(app, db)
    with app.test_request_context(), connection_scope():
        admin = accounts.create("platform_admin", "correct horse battery", role="admin", emit_event=False)
    token = issue(app, admin, ["settings"])
    response = call(app.test_client(), "PUT", "/settings", token, json={"public_mode": True})
    assert response.status_code == 403 and "public_mode" in response.json["refused"]
    db.conn.close()
