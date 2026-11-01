"""Regression tests for the MW2 findings of the full re-audit pass 7 (pages, custom pages, search, history, render cache)."""

from __future__ import annotations

import time

import pytest

from bananawiki.wiki.db import connection_scope

# MW2-1: accounts the wiki refuses see public views as anonymous visitors do

BLOCKS = [{"approval_status": "pending"}, {"approval_status": "denied"}, {"suspended": 1}]
SECRETS = (b"Board Secrets Zq", b"Reorg Plan Zq", b"Acquisition Zq", b"SalaryTableZq")


@pytest.fixture
def secret_pages(app, db):
    from bananawiki.wiki.features.pages import categories, service

    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('custom_pages', 'api_service')")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1 WHERE id = 1")
    with app.test_request_context(), connection_scope():
        category = categories.create("Board Secrets Zq")
        service.create("Reorg Plan Zq", "SalaryTableZq", category_id=category["id"], author_id=None)
        service.create("Acquisition Zq", "secret", author_id=None)
    db.insert("custom_pages", {"path": "/about-us", "title": "About", "content_type": "wiki_page",
                               "is_published": 1, "content": "Contact us"})


def _builder_page(app, db) -> None:
    from bananawiki.wiki.features.page_builder import document

    with app.test_request_context(), connection_scope():
        doc = document.validate({"version": 2, "blocks": [
            {"type": "heading", "text": "Welcome"},
            {"type": "pages", "source": "recent", "limit": 12, "excerpt": True}]})
        raw, markdown = document.dump(doc), document.to_markdown(doc)
    db.insert("custom_pages", {"path": "/landing", "title": "Landing", "content_type": "wiki_page",
                               "is_published": 1, "content": markdown, "builder_json": raw})


@pytest.mark.parametrize("block", BLOCKS)
def test_mw2_1_refused_accounts_get_no_page_tree_or_excerpts_on_public_views(secret_pages, app, db, make_user,
                                                                             login, block):
    _builder_page(app, db)
    client = app.test_client()
    login(client, make_user("refused", role="editor", **block))
    assert client.get("/").headers["Location"].endswith("/account-status")
    for path in ("/about-us", "/landing", "/api-docs"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert not [marker for marker in SECRETS if marker in response.data], path
    # The account is still sent to its status where it asks for itself.
    assert client.get("/admin").headers["Location"].endswith("/account-status")
    assert client.get("/signup").headers["Location"].endswith("/")


def test_mw2_1_approved_members_keep_the_page_tree_on_public_views(secret_pages, app, db, make_user, login):
    _builder_page(app, db)
    client = app.test_client()
    login(client, make_user("member", role="editor"))
    assert b"Reorg Plan Zq" in client.get("/about-us").data
    assert b"SalaryTableZq" in client.get("/landing").data


# MW2-2: the rendered history diff is bounded in size, and the version view in rate


def _two_versions(app, db, make_user, login, old: str, new: str):
    editor = app.test_client()
    login(editor, make_user("editor1", role="editor"))
    assert editor.post("/create", data={"title": "Notes", "content": old}).status_code == 302
    page = db.one("SELECT id, revision FROM pages WHERE slug = 'notes'")
    assert editor.post("/page/notes/edit", data={"title": "Notes", "content": new,
                                                  "revision": page["revision"]}).status_code == 302
    reader = app.test_client()
    login(reader, make_user("reader", role="user"))
    return reader, db.scalar("SELECT MAX(id) FROM page_history WHERE page_id = ?", (page["id"],))


def test_mw2_2_rendered_diff_of_large_versions_shows_the_source_diff(app, db, make_user, login, monkeypatch):
    from bananawiki.wiki.features.pages import diff

    monkeypatch.setattr(diff, "RENDERED_DIFF_CHARS", 2_000)
    reader, latest = _two_versions(app, db, make_user, login, "@abc " * 1_000, "@abd " * 1_000)
    calls = []
    monkeypatch.setattr(diff, "_runs", lambda *args, _real=diff._runs: calls.append(args) or _real(*args))
    response = reader.get(f"/page/notes/history/{latest}?view=diff")
    assert response.status_code == 200
    assert b"too large to compare as rendered pages" in response.data
    assert b'<pre class="diff"><del>@abc ' in response.data and b"<ins>@abd " in response.data
    assert len(calls) == 1  # the source diff only, never the rendered one
    assert diff.rendered_diff("<p>a</p>", "x" * 2_001) is None
    small = diff.rendered_diff("<p>old words</p>", "<p>new words</p>")
    assert small is not None and "<ins>new</ins>" in small.html


def test_mw2_2_history_versions_are_rate_limited_per_account(app, db, make_user, login, frozen_limiter_clock):
    reader, latest = _two_versions(app, db, make_user, login, "hello world", "hello there")
    codes = [reader.get(f"/page/notes/history/{latest}?view=diff").status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


# MW2-3: every rendering the renderer accepts fits in the render cache


@pytest.fixture
def empty_render_cache():
    from bananawiki.wiki import markdown

    markdown.clear_render_cache()
    yield markdown
    markdown.clear_render_cache()


def test_mw2_3_a_page_with_an_emoji_is_rendered_once(empty_render_cache, monkeypatch):
    markdown = empty_render_cache
    calls = []
    monkeypatch.setattr(markdown, "_render", lambda *args, _real=markdown._render: calls.append(1) or _real(*args))
    # 3 M characters of HTML: 12 MB as Python holds them, over the old quarter-of-the-cache entry limit.
    source = "\U0001F600 " + "@someone " * 55_000
    first = markdown.render(source)
    assert len(first) > 2_500_000 and "<pre>" not in first[:10]
    assert markdown.render(source) == first and len(calls) == 1
    assert markdown._render_cache.size < markdown.RENDER_CACHE_LIMIT // 2


def test_mw2_3_renderings_over_the_byte_bound_show_the_source_and_are_kept(empty_render_cache, monkeypatch):
    markdown = empty_render_cache
    assert markdown.MAX_HTML_BYTES + 512 <= markdown.RENDER_CACHE_LIMIT // 2
    monkeypatch.setattr(markdown, "MAX_HTML_BYTES", 1_000_000)
    calls = []
    monkeypatch.setattr(markdown, "_render", lambda *args, _real=markdown._render: calls.append(1) or _real(*args))
    source = "\U0001F600 " + "@someone " * 20_000  # about 1.1 M characters, over 1 MB encoded
    html = markdown.render(source)
    assert html.startswith("<pre>") and markdown.render(source) == html and len(calls) == 1


def test_mw2_3_anonymous_views_of_a_planted_page_render_it_once(app, db, make_user, login, empty_render_cache,
                                                               monkeypatch):
    markdown = empty_render_cache
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    editor = app.test_client()
    login(editor, make_user("editor9", role="editor"))
    assert editor.post("/create", data={"title": "Bomb", "content": "\U0001F600 " + "@someone " * 45_000}
                       ).status_code == 302
    markdown.clear_render_cache()
    calls = []
    monkeypatch.setattr(markdown, "_render", lambda *args, _real=markdown._render: calls.append(1) or _real(*args))
    anonymous = app.test_client()
    for _ in range(3):
        assert anonymous.get("/page/bomb").status_code == 200
    assert len(calls) == 1


# MW2-4: a sidebar search without index words costs a bounded LIKE, stopped at the deadline

EMOJI = "\U0001F601"


def _planted(app, count: int, size: int) -> None:
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        for number in range(count):
            service.create(f"Planted {number}", EMOJI * size, author_id=None)
        service.create("Party " + EMOJI, "hello world " + "x" * 40, author_id=None)


def test_mw2_4_queries_without_index_words_find_nothing_with_the_index(app, db, monkeypatch):
    from bananawiki.wiki.features.pages import service

    _planted(app, 3, 200_000)
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    limits = []
    monkeypatch.setattr(service, "time_limit", lambda *steps, _real=service.time_limit: limits.append(steps) or _real(*steps))
    client = app.test_client()
    response = client.get("/api/sidebar/search", query_string={"scope": "content", "q": EMOJI * 150 + "?"})
    assert response.status_code == 200 and response.get_json()["pages"] == []
    assert limits == []  # no statement at all, so no LIKE over the planted pages
    found = client.get("/api/sidebar/search", query_string={"scope": "content", "q": "hello"}).get_json()
    assert [page["title"] for page in found["pages"]] == ["Party " + EMOJI]


def test_mw2_4_the_like_fallback_is_short_and_stops_at_the_deadline(app, db, monkeypatch):
    from bananawiki.wiki.features.pages import service

    _planted(app, 12, 400_000)
    app.extensions["bananawiki.fts"] = False  # a SQLite without FTS5 matches every query with LIKE
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    client = app.test_client()
    monkeypatch.setattr(service, "SEARCH_SECONDS", 0.2)
    for query in (EMOJI * 150 + "?", EMOJI * 31 + "?"):  # each planted page almost matches at every position
        started = time.monotonic()
        response = client.get("/api/sidebar/search", query_string={"scope": "content", "q": query})
        elapsed = time.monotonic() - started
        assert response.status_code in (200, 503) and elapsed < 0.8, elapsed
    assert response.status_code == 503
    # Only the first LIKE_CHARS characters are matched.
    monkeypatch.setattr(service, "SEARCH_SECONDS", 5.0)
    found = client.get("/api/sidebar/search", query_string={"scope": "content", "q": "x" * 32 + "not in the page"})
    assert [page["title"] for page in found.get_json()["pages"]] == ["Party " + EMOJI]
