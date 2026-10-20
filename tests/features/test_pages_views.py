"""Reading pages: home, page view, navigation, categories, sidebar, search, public mode."""

import pytest
from markupsafe import Markup

from bananawiki.wiki.features.pages import service
from bananawiki.wiki.features.pages.search import snippet_html, snippet_text

from .pages_support import add_interceptor, in_app, make_category, make_page, restrict


def test_home_without_home_page_shows_welcome(admin_client, db):
    db.execute("DELETE FROM pages")
    response = admin_client.get("/")
    assert response.status_code == 200
    assert b"There are no pages yet" in response.data


def test_home_page_is_rendered(app, admin_client):
    page = make_page(app, "Start", "Welcome **friends**")
    in_app(app, lambda: service.set_home(service.get(page["id"])))
    body = admin_client.get("/").get_data(as_text=True)
    assert "<strong>friends</strong>" in body


def test_view_page_with_toc_breadcrumbs_and_last_edit(app, admin_client, admin):
    parent = make_category(app, "Guides")
    child = make_category(app, "Basics", parent["id"])
    make_page(app, "Intro", "# One\n\ntext\n\n## Two\n\n## Three", category_id=child["id"], author_id=admin["id"])
    body = admin_client.get("/page/intro").get_data(as_text=True)
    assert 'href="#two"' in body and "toc-list" in body
    assert "Guides" in body and "Basics" in body
    assert "admin_user" in body


def test_missing_and_invisible_pages_are_404(app, client, make_user, login, db):
    secret = make_category(app, "Secret")
    open_cat = make_category(app, "Open")
    make_page(app, "Hidden plan", category_id=secret["id"])
    make_page(app, "Public note", category_id=open_cat["id"])
    reader = make_user("reader")
    restrict(db, reader, read=[open_cat["id"]])
    login(client, reader)
    assert client.get("/page/nope").status_code == 404
    assert client.get("/page/hidden-plan").status_code == 404
    assert client.get("/page/public-note").status_code == 200
    assert client.get(f"/category/{secret['id']}").status_code == 404


def test_deindexed_page_needs_permission(app, client, make_user, login):
    page = make_page(app, "Quiet")
    in_app(app, lambda: service.set_fields(page["id"], is_deindexed=1))
    login(client, make_user("plain"))
    assert client.get("/page/quiet").status_code == 404


def test_anonymous_redirected_unless_public_mode(app, client, db):
    make_page(app, "Open page", "hello")
    assert client.get("/page/open-page").status_code == 302
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    response = client.get("/page/open-page")
    assert response.status_code == 200 and b"hello" in response.data
    assert client.get("/page/open-page/edit").status_code == 302


def test_sequential_navigation(app, admin_client):
    cat = make_category(app, "Course")
    in_app(app, lambda: service.db.execute("UPDATE categories SET sequential_nav = 1 WHERE id = ?", (cat["id"],)))
    for title in ("Lesson 1", "Lesson 2", "Lesson 3"):
        make_page(app, title, category_id=cat["id"])
    body = admin_client.get("/page/lesson-2").get_data(as_text=True)
    assert 'rel="prev" href="/page/lesson-1"' in body and 'rel="next" href="/page/lesson-3"' in body


def test_render_interceptor_replaces_body(app, admin_client):
    make_page(app, "Built", "markdown body")
    add_interceptor(app, "page.render", lambda page: Markup("<p>builder output</p>") if page["slug"] == "built" else None)
    body = admin_client.get("/page/built").get_data(as_text=True)
    assert "builder output" in body and "markdown body" not in body


def test_slots_receive_page(app, admin_client):
    from bananawiki.wiki import registry

    make_page(app, "Slotted")
    reg = app.extensions["bananawiki.registry"]
    reg.add(registry.Feature(id="slot_probe", name="x", toggle="always", slots={
        "page.header_actions": lambda page: Markup(f"<i>header-{page['id']}</i>"),
        "page.below_content": lambda page: Markup("<i>below</i>"),
        "page.sidebar_panel": lambda page: Markup("<i>panel</i>"),
    }))
    body = admin_client.get("/page/slotted").get_data(as_text=True)
    assert "header-" in body and "<i>below</i>" in body and "<i>panel</i>" in body


def test_sidebar_tree_hides_unreadable_category_names(app, client, make_user, login, db):
    top = make_category(app, "TopSecret")
    child = make_category(app, "Allowed child", top["id"])
    make_page(app, "Child page", category_id=child["id"])
    make_page(app, "Top page", category_id=top["id"])
    reader = make_user("reader2")
    restrict(db, reader, read=[child["id"]])
    login(client, reader)
    body = client.get("/page/child-page").get_data(as_text=True)
    assert "Allowed child" in body and "TopSecret" not in body and "Top page" not in body


def test_sidebar_loads_more_pages_in_batches(app, admin_client):
    cat = make_category(app, "Big")
    for n in range(30):
        make_page(app, f"Item {n:02d}", category_id=cat["id"])
    body = admin_client.get("/").get_data(as_text=True)
    assert "data-nav-more" in body
    first = admin_client.get(f"/api/sidebar/pages?category_id={cat['id']}&after=0").get_json()
    assert first["html"].count("data-reorder-item") == 30 and first["more"] is None
    page25 = in_app(app, lambda: service.get_by_slug("item-24"))
    rest = admin_client.get(f"/api/sidebar/pages?category_id={cat['id']}&after={page25['id']}").get_json()
    assert rest["html"].count("data-reorder-item") == 5
    fallback = admin_client.get(f"/navigation/pages?category_id={cat['id']}&after={page25['id']}")
    assert fallback.status_code == 200 and b"Item 29" in fallback.data


def test_sidebar_batch_refuses_unreadable_category(app, client, make_user, login, db):
    secret = make_category(app, "Private")
    other = make_category(app, "Other")
    reader = make_user("reader3")
    restrict(db, reader, read=[other["id"]])
    login(client, reader)
    assert client.get(f"/api/sidebar/pages?category_id={secret['id']}").status_code == 404
    assert client.get("/api/sidebar/pages?category_id=abc").status_code == 400


def test_navigation_page_lists_tree(app, admin_client):
    cat = make_category(app, "Docs")
    make_page(app, "Doc page", category_id=cat["id"])
    body = admin_client.get("/navigation").get_data(as_text=True)
    assert "Docs" in body and "Doc page" in body and 'id="new-category"' in body


def test_sync_endpoint_reports_changes(app, admin_client, admin):
    page = make_page(app, "Live", "v1")
    assert admin_client.get("/api/page/live/sync?since=1").get_json()["changed"] is False
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"], content="v2"))
    data = admin_client.get("/api/page/live/sync?since=1").get_json()
    assert data["changed"] is True and data["revision"] == 2


@pytest.mark.parametrize("query", ["#", "category:Hidden", "slug:salaries", '"'])
def test_search_without_full_text_terms_does_not_fail(app, admin_client, query):
    """Relevance sorting needs a full-text term; queries without one still answer."""
    hidden = make_category(app, "Hidden")
    make_page(app, "Salaries 2026", "confidential numbers", category_id=hidden["id"])
    assert admin_client.get("/search", query_string={"q": query}).status_code == 200


def test_search_works_without_the_full_text_index(app, admin_client, monkeypatch):
    make_page(app, "Salaries 2026", "confidential numbers")
    monkeypatch.setattr(service, "fts_available", lambda: False)
    response = admin_client.get("/search", query_string={"q": "confidential"})
    assert response.status_code == 200 and b"Salaries 2026" in response.data


@pytest.mark.parametrize(("raw", "expected"), [
    ("**Operations**, **Product**, HR", "Operations, Product, HR"),
    ("[TOC] Overview of the release", "Overview of the release"),
    (" … release notes and project pages. ![Team offsite 2026](/static/up … ", "… release notes and project pages. …"),
    ('see [[kanban board="1"]] here', "see here"),
    ("ask @mia_member about snake_case_name", "ask @mia_member about snake_case_name"),
    ("read [the docs](https://example.org/docs) now", "read the docs now"),
    ("## Heading\n> quote\n- [x] done\n| a | b |\n| --- | --- |", "Heading quote done a b"),
    (" … example.org/x.png) after the image", "… after the image"),
])
def test_search_snippets_show_words_not_markdown(raw, expected):
    assert snippet_text(raw) == expected


def test_search_snippet_keeps_the_match_marks_and_escapes():
    html = str(snippet_html(" … the _\x02release\x03_ <b>notes</b> for mia_\x02member\x03 … ", [], marked=True))
    assert html == "… the <mark>release</mark> &lt;b&gt;notes&lt;/b&gt; for mia_<mark>member</mark> …"


def test_viewing_a_page_twice_renders_its_markdown_once(app, admin_client, monkeypatch):
    from bananawiki.wiki import markdown

    markdown.clear_render_cache()
    calls = []
    original = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args: calls.append(1) or original(*args))
    page = make_page(app, "Long read", "# Long read\n\n" + "A paragraph with some words.\n\n" * 300)
    for _ in range(3):
        assert admin_client.get(f"/page/{page['slug']}").status_code == 200
    assert len(calls) == 1
