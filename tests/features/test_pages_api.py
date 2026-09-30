"""Search, JSON helpers, editing presence, image uploads and their clean-up."""

import io
import os
import time

import pytest
from PIL import Image

from bananawiki.wiki.features.pages import search, uploads

from .pages_support import in_app, make_category, make_page, restrict


def _png(color=(200, 10, 10)) -> io.BytesIO:
    data = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(data, "PNG")
    data.seek(0)
    return data


def _client(app, make_user, login, name, role="user"):
    client = app.test_client()
    login(client, make_user(name, role=role))
    return client


# ── Search ────────────────────────────────────────────────────────────────────


def test_search_page_filters_by_visibility(app, make_user, login, db):
    secret = make_category(app, "Secret")
    public = make_category(app, "Public")
    make_page(app, "Budget secret", "banana numbers", category_id=secret["id"])
    make_page(app, "Budget public", "banana facts", category_id=public["id"])
    user = make_user("searcher")
    restrict(db, user, read=[public["id"]])
    client = app.test_client()
    login(client, user)
    body = client.get("/search?q=banana").get_data(as_text=True)
    assert "Budget public" in body and "Budget secret" not in body
    api = client.get("/api/sidebar/search?q=budget").get_json()
    assert [p["title"] for p in api["pages"]] == ["Budget public"]
    assert client.get("/api/pages/search?q=budget").get_json()[0]["title"] == "Budget public"


def test_search_snippet_is_escaped_and_marked(app, admin_client):
    make_page(app, "Xss", "<script>alert(1)</script> findme here")
    body = admin_client.get("/search?q=findme").get_data(as_text=True)
    assert "<mark>findme</mark>" in body
    assert "<script>alert(1)</script>" not in body


def test_search_syntax(app, admin_client):
    cat = make_category(app, "Fruit")
    make_page(app, "Apple pie", "sweet recipe", category_id=cat["id"])
    make_page(app, "Apple juice", "sweet drink")
    titles = lambda q: [p["title"] for p in in_app(app, lambda: search.run(  # noqa: E731
        q, user={"id": "x", "role": "admin"}, scope="all", result_type="page", sort="title", page=1))["pages"]]
    assert titles("apple -juice") == ["Apple pie"]
    assert titles("apple category:fruit") == ["Apple pie"]
    assert titles("title:juice") == ["Apple juice"]
    assert titles('content:"sweet drink"') == ["Apple juice"]
    assert titles("slug:apple-p") == ["Apple pie"]
    body = admin_client.get("/search?q=fruit&type=category").get_data(as_text=True)
    assert "<mark>Fruit</mark>" in body


def test_search_requires_permission_and_is_rate_limited(app, make_user, login, db):
    user = make_user("nosearch")
    restrict(db, user, keys={"page.view_all"})
    client = app.test_client()
    login(client, user)
    assert client.get("/search?q=a").status_code == 403
    assert client.get("/api/sidebar/search?q=a").status_code == 403
    other = _client(app, make_user, login, "fastsearch")
    codes = [other.get("/search?q=a").status_code for _ in range(35)]
    assert 429 in codes


def test_search_public_mode_anonymous(app, client, db):
    make_page(app, "Open", "visible text")
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    assert b"Open" in client.get("/search?q=visible").data


# ── Preview and lookups ──────────────────────────────────────────────────────


def test_preview_sanitises(admin_client):
    html = admin_client.post("/api/preview", json={"content": "**b** <script>x</script>"}).get_json()["html"]
    assert "<strong>b</strong>" in html and "<script>" not in html
    assert admin_client.post("/api/preview", data="nope").status_code == 400


def test_highlight(admin_client):
    html = admin_client.post("/api/code/highlight", json={"content": "x = 1", "language": "python"}).get_json()["html"]
    assert "codehilite" in html


def test_preview_by_slug_respects_visibility(app, make_user, login, db, admin_client):
    secret = make_category(app, "Hidden cat")
    make_page(app, "Peek", "secret", category_id=secret["id"])
    assert admin_client.get("/api/pages/preview-by-slug?slug=peek").get_json()["title"] == "Peek"
    user = make_user("peeker")
    restrict(db, user, read=[])
    client = app.test_client()
    login(client, user)
    assert client.get("/api/pages/preview-by-slug?slug=peek").status_code == 404


# ── Presence ──────────────────────────────────────────────────────────────────


def test_editing_presence(app, make_user, login):
    make_page(app, "Busy")
    first = _client(app, make_user, login, "writer1", "editor")
    second = _client(app, make_user, login, "writer2", "editor")
    first.post("/api/page/busy/editing/heartbeat", json={})
    assert second.post("/api/page/busy/editing/heartbeat", json={}).get_json()["editors"] == ["writer1"]
    assert second.get("/api/page/busy/editing/check").get_json()["editors"] == ["writer1"]
    first.post("/api/page/busy/editing/stop")
    assert second.get("/api/page/busy/editing/check").get_json()["editors"] == []
    plain = _client(app, make_user, login, "watcher")
    assert plain.post("/api/page/busy/editing/heartbeat", json={}).status_code == 403


# ── Uploads ───────────────────────────────────────────────────────────────────


def _upload(client, data=None, name="pic.png"):
    return client.post("/api/upload", data={"file": (data or _png(), name)}, content_type="multipart/form-data")


def test_upload_image(app, admin_client):
    response = _upload(admin_client)
    assert response.status_code == 201
    url = response.get_json()["url"]
    assert url.startswith("/static/uploads/") and admin_client.get(url).status_code == 200


def test_upload_rejects_non_images_and_plain_users(app, admin_client, make_user, login):
    assert _upload(admin_client, io.BytesIO(b"<html>"), "evil.png").status_code == 400
    assert _upload(admin_client, io.BytesIO(b"text"), "notes.txt").status_code == 400
    plain = _client(app, make_user, login, "uploader")
    assert _upload(plain).status_code == 403


def test_upload_daily_quota(app, admin_client, db):
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 1 WHERE id = 1")
    assert _upload(admin_client).status_code == 201
    assert _upload(admin_client).status_code == 429


def test_upload_delete_admin_only_and_not_in_use(app, admin_client, make_user, login):
    name = _upload(admin_client).get_json()["filename"]
    editor = _client(app, make_user, login, "deleter", "editor")
    assert editor.post("/api/upload/delete", json={"filename": name}).status_code == 403
    make_page(app, "Uses image", f"![x](/static/uploads/{name})")
    assert admin_client.post("/api/upload/delete", json={"filename": name}).status_code == 409
    assert admin_client.post("/api/upload/delete", json={"filename": name, "force": True}).status_code == 200
    assert admin_client.post("/api/upload/delete", json={"filename": "../../etc/passwd"}).status_code == 404


def _stored(app, name, age_hours):
    folder = in_app(app, lambda: uploads.storage.folder_path("uploads"))
    path = folder / name
    path.write_bytes(b"x")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("where", ["page", "history", "announcement", "canvas", "accessibility"])
def test_cleanup_keeps_referenced_images(app, db, where, admin):
    name = "a" * 32 + ".png"
    path = _stored(app, name, 48)
    if where in ("page", "history"):
        page = make_page(app, "Pic", f"![x](/static/uploads/{name})")
        if where == "history":
            db.execute("UPDATE pages SET content = '' WHERE id = ?", (page["id"],))
    elif where == "announcement":
        db.execute("INSERT INTO announcements (content) VALUES (?)", (f"see /static/uploads/{name}",))
    elif where == "canvas":
        db.execute("INSERT INTO canvas__layouts (slug, title, creator_id, data) VALUES ('c', 'c', ?, ?)",
                   (admin["id"], '{"nodes":[{"url":"/static/uploads/' + name + '"}]}'))
    else:
        db.execute("UPDATE users SET accessibility = ? WHERE id = ?", ('{"background": "' + name + '"}', admin["id"]))
    assert in_app(app, uploads.cleanup_unused) == 0
    assert path.exists()


def test_cleanup_removes_old_unreferenced_only(app):
    old = _stored(app, "b" * 32 + ".png", 48)
    fresh = _stored(app, "c" * 32 + ".png", 1)
    folder = old.parent
    (folder / "avatars").mkdir(exist_ok=True)
    nested = folder / "avatars" / ("d" * 32 + ".png")
    nested.write_bytes(b"x")
    assert in_app(app, uploads.cleanup_unused) == 1
    assert not old.exists() and fresh.exists() and nested.exists()


def test_cleanup_is_a_registered_job(app):
    jobs = [job.name for feature in app.extensions["bananawiki.registry"].ordered() for job in feature.jobs]
    assert "pages.cleanup_uploads" in jobs and "pages.prune_editing_sessions" in jobs
