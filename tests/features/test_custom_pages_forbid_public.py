"""Custom pages on hosts that forbid public wikis (BW_FORBID_PUBLIC_MODE) or public builder pages."""

from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

FORBID_ALL = {"BW_FORBID_PUBLIC_MODE": "1", "BW_FORBID_PUBLIC_BUILDER_PAGES": "1"}
BUILDER_ONLY = {"BW_FORBID_PUBLIC_BUILDER_PAGES": "1"}
DOC = {"version": 2, "blocks": [{"type": "hero", "title": "Welcome", "text": "Hello", "layout": {"align": "center"}}]}


@pytest.fixture
def app(app_factory, request):
    """A wiki whose host forbids public access (like an unapproved hosted tenant), or what the test asks."""
    return app_factory(environ=getattr(request, "param", FORBID_ALL))


@pytest.fixture
def enabled(db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1")


@pytest.fixture
def anon(app):
    return app.test_client()


@pytest.fixture
def member(app, make_user, login):
    client = app.test_client()
    login(client, make_user("member1"))
    return client


def insert(db, **values):
    base = {"path": "/p", "title": "T", "content_type": "wiki_page", "is_published": 1}
    base.update(values)
    return db.insert("custom_pages", base)


def upload(admin_client, db, path, kind, name, data):
    response = admin_client.post("/admin/custom-pages/create", content_type="multipart/form-data", data={
        "path": path, "title": "Files", "content_type": kind, "is_published": "1", "file": (io.BytesIO(data), name)})
    assert response.status_code == 302
    return db.one("SELECT f.* FROM custom_page_files f JOIN custom_pages p ON p.id = f.custom_page_id "
                  "WHERE p.path = ?", (path,))


def builder_page(admin_client, db, path="/landing"):
    """A published builder custom page holding DOC, made through the custom builder."""
    admin_client.post("/admin/custom-pages/create", content_type="multipart/form-data",
                      data={"path": path, "title": "Landing", "content_type": "builder", "is_published": "1"})
    page_id = db.scalar("SELECT id FROM custom_pages WHERE path = ?", (path,))
    html = admin_client.get(f"/admin/custom-pages/{page_id}/builder").get_data(as_text=True)
    token = html.split('data-base-token="', 1)[1].split('"', 1)[0]
    saved = admin_client.post(f"/api/custom-pages/{page_id}/builder/save", content_type="application/json",
                              data=json.dumps({"document": DOC, "base_token": token}))
    assert saved.status_code == 200
    return page_id


def assert_sign_in(response, path):
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert location.path == "/login" and parse_qs(location.query)["next"] == [path]


# ── BW_FORBID_PUBLIC_MODE: anonymous visitors sign in first ──────────────────


@pytest.mark.parametrize("kind", ["html", "html_full", "markdown", "wiki_page", "plain_text", "json_content",
                                  "link_list", "code_snippet", "youtube_video"])
def test_anonymous_visitors_are_sent_to_sign_in(enabled, anon, db, kind):
    page_id = insert(db, path="/signin-help", content_type=kind, content="<form>Password</form>",
                     js="fetch('https://evil.example')", video_url="dQw4w9WgXcQ")
    response = anon.get("/signin-help")
    assert_sign_in(response, "/signin-help")
    assert b"Password" not in response.data
    document = anon.get(f"/_cpd/{page_id}")
    if kind in ("html", "html_full", "markdown"):
        assert_sign_in(document, f"/_cpd/{page_id}")
        assert b"evil.example" not in document.data
    else:
        assert document.status_code == 404


def test_redirect_pages_do_not_send_anonymous_visitors_away(enabled, anon, member, db):
    insert(db, path="/go", content_type="redirect", redirect_url="https://phish.example/login", redirect_code=301)
    assert_sign_in(anon.get("/go"), "/go")
    trailing = anon.get("/go/")  # the slash redirect leads to the same sign-in
    assert trailing.status_code == 308 and urlsplit(trailing.headers["Location"]).path == "/go"
    assert member.get("/go").headers["Location"] == "https://phish.example/login"


def test_files_and_file_pages_need_a_sign_in(enabled, admin_client, anon, member, db):
    listed = upload(admin_client, db, "/downloads", "file_listing", "notes.txt", b"hello")
    single = upload(admin_client, db, "/setup.exe", "file_download", "data.csv", b"a,b")
    for path in ("/downloads", "/setup.exe", f"/_cpf/{listed['id']}/notes.txt", f"/_cpf/{single['id']}/data.csv"):
        response = anon.get(path)
        assert_sign_in(response, path)
        assert b"hello" not in response.data and b"a,b" not in response.data
    assert b"notes.txt" in member.get("/downloads").data
    assert member.get(f"/_cpf/{listed['id']}/notes.txt").data == b"hello"
    assert member.get("/setup.exe").data == b"a,b"


def test_members_keep_access_to_every_published_type(enabled, admin_client, member, db):
    html_id = insert(db, path="/app", content_type="html_full", content="<h1>App</h1>", js="run()")
    insert(db, path="/about", content="**About us**")
    builder_page(admin_client, db)
    wrapper = member.get("/app")
    assert wrapper.status_code == 200 and f'src="/_cpd/{html_id}"'.encode() in wrapper.data
    document = member.get(f"/_cpd/{html_id}")
    assert document.status_code == 200 and b"run()" in document.data
    assert document.headers["Content-Security-Policy"].startswith("sandbox ")
    assert b"<strong>About us</strong>" in member.get("/about").data
    landing = member.get("/landing").get_data(as_text=True)
    assert '<section class="builder-page">' in landing and "Welcome" in landing


def test_unpublished_and_missing_pages_still_answer_404(enabled, anon, member, db):
    hidden_id = insert(db, path="/draft", content_type="html", content="secret", is_published=0)
    for client in (anon, member):
        assert client.get("/draft").status_code == 404
        assert client.get(f"/_cpd/{hidden_id}").status_code == 404
    assert anon.get("/nothing-here").status_code == 404
    assert anon.get("/_cpf/999/x.txt").status_code == 404
    assert anon.get("/_cpd/999").status_code == 404


def test_accounts_that_may_not_use_the_wiki_are_sent_to_their_status(enabled, app, make_user, login, db):
    page_id = insert(db, path="/about", content_type="html", content="Members only")
    client = app.test_client()
    login(client, make_user("newbie", approval_status="pending"))
    for path in ("/about", f"/_cpd/{page_id}"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/account-status")
        assert b"Members only" not in response.data


def test_suspended_administrators_lose_members_and_manager_access(enabled, app, make_user, login, db):
    published = insert(db, path="/about", content_type="html", content="Members only")
    insert(db, path="/draft", content_type="html", content="Unpublished draft", is_published=0)
    client = app.test_client()
    login(client, make_user("benched", role="admin", suspended=1))
    for path in ("/about", f"/_cpd/{published}", "/draft"):
        response = client.get(path)
        assert response.status_code in (302, 404)
        assert b"Members only" not in response.data and b"Unpublished draft" not in response.data


def test_builder_pages_need_a_sign_in(enabled, admin_client, anon, db):
    builder_page(admin_client, db)
    response = anon.get("/landing")
    assert_sign_in(response, "/landing")
    assert b"builder-page" not in response.data and b"Welcome" not in response.data


def test_admin_pages_say_published_pages_are_for_members(enabled, admin_client, db):
    page_id = insert(db, path="/about")
    listing = admin_client.get("/admin/custom-pages").get_data(as_text=True)
    assert "shown only to signed-in members" in listing and "can be opened by anyone" not in listing
    edit = admin_client.get(f"/admin/custom-pages/{page_id}/edit").get_data(as_text=True)
    assert "shown to signed-in members only" in edit and "anyone can open them" not in edit


# ── BW_FORBID_PUBLIC_BUILDER_PAGES alone: only builder pages need a sign-in ──


@pytest.mark.parametrize("app", [BUILDER_ONLY], indirect=True)
def test_only_builder_pages_hidden_when_public_builder_pages_are_forbidden(enabled, admin_client, anon, member, db):
    builder_page(admin_client, db)
    insert(db, path="/about", content="**About us**")
    response = anon.get("/landing")
    assert_sign_in(response, "/landing")
    assert b"Welcome" not in response.data
    assert b"<strong>About us</strong>" in anon.get("/about").data
    assert '<section class="builder-page">' in member.get("/landing").get_data(as_text=True)
    page_id = db.scalar("SELECT id FROM custom_pages WHERE path = '/landing'")
    edit = admin_client.get(f"/admin/custom-pages/{page_id}/edit").get_data(as_text=True)
    assert "does not allow public builder pages" in edit and "anyone can open them" in edit
