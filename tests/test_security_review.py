"""Regression tests for the 1.6 security review (one test per confirmed finding)."""

from __future__ import annotations

import pytest

from bananawiki.wiki import registry
from tests.features.pages_support import in_app, make_category, make_page, restrict


@pytest.fixture
def hidden_page(app):
    """A page in a category that restricted readers cannot open."""
    visible = make_category(app, "Open")
    hidden = make_category(app, "Hidden")
    page = make_page(app, "Salaries 2026", "confidential numbers", category_id=hidden["id"])
    return {"open": visible, "hidden": hidden, "page": page}


@pytest.fixture
def restricted_editor(make_user, db, hidden_page):
    user = make_user("restricted_ed", role="editor")
    restrict(db, user, read=[hidden_page["open"]["id"]], write=[hidden_page["open"]["id"]])
    return user


# ── Request pipeline ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("method, url", [
    ("POST", "/api/preview"),
    ("POST", "/api/code/highlight"),
    ("GET", "/api/accessibility"),
    ("POST", "/api/accessibility"),
    ("POST", "/api/draft/save"),
    ("GET", "/api/draft/mine"),
])
def test_bearer_header_does_not_skip_sign_in_outside_the_token_api(client, method, url):
    """Any ``Authorization: Bearer`` header used to skip the session and login gate for all of /api/."""
    response = client.open(url, method=method, json={"content": "x"}, headers={"Authorization": "Bearer junk"})
    assert response.status_code == 401


def test_bearer_calls_still_reach_the_token_api(app, client):
    in_app(app, lambda: registry.set_enabled("api_service", True))
    response = client.get("/api/v1/pages", headers={"Authorization": "Bearer junk"})
    assert response.status_code in (401, 503)  # the API's own answer, not the sign-in redirect
    assert response.get_json()["error"]


# ── Canvas ───────────────────────────────────────────────────────────────────


def test_canvas_export_hides_pages_the_exporter_cannot_read(app, client, db, login, hidden_page, restricted_editor):
    from bananawiki.wiki.features.canvas import service as canvas

    in_app(app, lambda: registry.set_enabled("canvas", True))
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'all' WHERE id = 1")
    layout = in_app(app, lambda: canvas.create("Mine", "", restricted_editor["id"]))
    login(client, restricted_editor)
    node = {"id": "n1", "type": "wiki_page", "x": 0, "y": 0, "page_id": hidden_page["page"]["id"]}
    assert client.post(f"/canvas/{layout['slug']}/ops", json={"ops": [{"op": "upsert_node", "node": node}]}
                       ).status_code == 200
    exported = client.get(f"/canvas/{layout['slug']}/export?format=json")
    assert exported.status_code == 200
    assert b"Salaries" not in exported.data and b"salaries-2026" not in exported.data


# ── Page builder ─────────────────────────────────────────────────────────────


def test_page_builder_publish_cannot_rename_without_edit_metadata(app, client, db, make_user, login):
    from bananawiki.wiki import permissions

    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor' WHERE id = 1")
    page = make_page(app, "Landing", "text")
    editor = make_user("builder_ed", role="editor")
    restrict(db, editor, keys=permissions.defaults("editor") - {"page.edit_metadata"})
    login(client, editor)
    body = {"document": {"version": 1, "blocks": [{"type": "text", "text": "new"}]},
            "base_revision": page["revision"], "title": "Renamed"}
    response = client.post(f"/api/page/{page['slug']}/builder/publish", json=body)
    assert response.status_code == 200, response.data
    row = db.one("SELECT title, content FROM pages WHERE id = ?", (page["id"],))
    assert row["title"] == "Landing" and "new" in row["content"]


# ── Robustness ───────────────────────────────────────────────────────────────


def test_out_of_range_numbers_in_the_query_are_a_bad_request(app, admin_client):
    """``?page=10**20`` reached SQLite as an OFFSET and answered 500 (OverflowError)."""
    page = make_page(app, "Numbers", "x")
    huge = 10 ** 20
    for url in (f"/page/{page['slug']}/history?page={huge}", f"/users?page={huge}", f"/admin/users?page={huge}"):
        assert admin_client.get(url).status_code == 400, url


# ── Account merges ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_direct_merge_respects_the_administrator_hierarchy(admin_client, db, make_user, role):
    """A plain administrator could delete an owner or another administrator by merging it away."""
    make_user("protected_one", role=role)
    make_user("throwaway")
    response = admin_client.post("/admin/users/merge", data={
        "source_username": "protected_one", "target_username": "throwaway", "delete_source": "1",
        "action": "execute"})
    assert response.status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'protected_one'") == 1


def test_direct_merge_of_ordinary_accounts_still_works(admin_client, db, make_user):
    make_user("old_name")
    make_user("new_name")
    response = admin_client.post("/admin/users/merge", data={
        "source_username": "old_name", "target_username": "new_name", "delete_source": "1", "action": "execute"})
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'old_name'") == 0


# ── Search ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", ["#", "category:Hidden", "slug:salaries", '"'])
def test_search_without_full_text_terms_does_not_fail(admin_client, hidden_page, query):
    """Relevance sorting used ``ORDER BY 0`` when no full-text term was present: SQLite refuses that (500)."""
    assert admin_client.get("/search", query_string={"q": query}).status_code == 200


def test_search_works_without_the_full_text_index(admin_client, hidden_page, monkeypatch):
    from bananawiki.wiki.features.pages import service

    monkeypatch.setattr(service, "fts_available", lambda: False)
    response = admin_client.get("/search", query_string={"q": "confidential"})
    assert response.status_code == 200 and b"Salaries 2026" in response.data


# ── Uploads ──────────────────────────────────────────────────────────────────


def test_images_beyond_the_pixel_cap_are_refused_before_decoding(app):
    """Pillow only warns up to twice MAX_IMAGE_PIXELS: a 42-megapixel PNG of a few KB used to be decoded."""
    import io

    from PIL import Image
    from werkzeug.datastructures import FileStorage

    from bananawiki.wiki import storage

    buffer = io.BytesIO()
    Image.new("1", (7000, 6000)).save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 100_000

    def upload() -> None:
        stream = io.BytesIO(buffer.getvalue())
        storage.save(FileStorage(stream, filename="huge.png"), "uploads", allowed=None, max_bytes=10 * 1024 * 1024,
                     images_only=True)

    with pytest.raises(storage.UploadError) as error:
        in_app(app, upload)
    assert error.value.key == "upload.error.not_an_image"
    assert storage.MAX_IMAGE_PIXELS == 40_000_000
