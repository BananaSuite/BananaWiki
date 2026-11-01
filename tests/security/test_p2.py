"""Regression tests for the p2 findings of the full re-audit pass 10 (builder rendering, search, proposals)."""

from __future__ import annotations

import json

import pytest

from bananawiki.wiki import markdown
from bananawiki.wiki.db import connection_scope


@pytest.fixture
def builder_page(app, db):
    from bananawiki.wiki.features.pages import service

    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor'")
    with app.test_request_context(), connection_scope():
        return service.create("Target", "x", author_id=None)


def _publish(client, page, blocks):
    body = {"document": {"version": 2, "blocks": blocks}, "base_revision": page["revision"]}
    response = client.post(f"/api/page/{page['slug']}/builder/publish", data=json.dumps(body),
                           content_type="application/json")
    assert response.status_code == 200, response.get_json()


def _text_blocks(texts):
    return [{"type": "text", "format": "markdown", "text": text} for text in texts]


# PW2-1: the Markdown blocks of a builder page share one document's limits, and the page is rendered once


def test_pw2_1_builder_blocks_share_one_heading_limit(app, builder_page, monkeypatch):
    from bananawiki.wiki.features.page_builder import document

    monkeypatch.setattr(markdown, "MAX_HEADINGS", 300)
    markdown.clear_render_cache()
    # 20 blocks of 100 repeated headings: each far under the limit, 2,000 in all.
    loaded = {"version": 2, "blocks": _text_blocks(["# h\n" * 100 + str(n) for n in range(20)])}
    with app.test_request_context(), connection_scope():
        html = str(document.render(document.validate(loaded, complete=True)))
    assert html.count("<h1") <= 300
    # Blocks past the shared limit are shown as escaped source, like an over-limit Markdown page.
    assert "<pre># h" in html


def test_pw2_1_builder_blocks_share_one_output_limit(app, builder_page, monkeypatch):
    from bananawiki.wiki.features.page_builder import document

    monkeypatch.setattr(markdown, "MAX_HTML_CHARS", 200_000)
    monkeypatch.setattr(markdown, "MAX_HTML_BYTES", 200_000)
    markdown.clear_render_cache()
    # Each block repeats a 900-character reference link URL 150 times (about 140 KB of HTML).
    block = "[r]: http://e.example/" + "a" * 900 + "\n\n" + "[r] " * 150
    loaded = {"version": 2, "blocks": _text_blocks([block + str(n) for n in range(6)])}
    with app.test_request_context(), connection_scope():
        html = str(document.render(document.validate(loaded, complete=True)))
    assert len(html) < 2 * 200_000


def test_pw2_1_builder_page_is_not_rendered_again_on_every_view(app, client, builder_page, make_user, login,
                                                                  monkeypatch):
    login(client, make_user("eve", role="editor"))
    # Short blocks (under RENDER_CACHE_MIN_CHARS), each with headings to number.
    _publish(client, builder_page, _text_blocks(["## same\n" * 20 + str(n) for n in range(30)]))
    reader = app.test_client()
    login(reader, make_user("rita"))
    first = reader.get("/page/target")
    assert first.status_code == 200
    calls = []
    real = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    again = reader.get("/page/target")
    assert again.status_code == 200 and again.data.count(b"<h2") == first.data.count(b"<h2") > 0
    assert calls == []
    # The editor's preview of the same document is served from the same rendering.
    preview = client.post("/api/page/target/builder/preview", data=json.dumps({"document": {
        "version": 2, "blocks": _text_blocks(["## same\n" * 20 + str(n) for n in range(30)])}}),
        content_type="application/json")
    assert preview.status_code == 200 and calls == []


def test_pw2_1_a_full_builder_page_with_highlighted_code_is_still_cached(app, client, builder_page, make_user,
                                                                          login, monkeypatch):
    # One document's HTML is 200 KB here, and the cache keeps entries of up to 201 KB.
    monkeypatch.setattr(markdown, "MAX_HTML_CHARS", 200_000)
    monkeypatch.setattr(markdown, "MAX_HTML_BYTES", 200_000)
    monkeypatch.setattr(markdown, "RENDER_CACHE_LIMIT", 2 * 201_000)
    markdown.clear_render_cache()
    login(client, make_user("eve", role="editor"))
    # Highlighted code (about 25 times its source), then text blocks that use up the rest of the HTML.
    code = [{"type": "code", "language": "c", "code": "\n".join(["&a" * 35] * 30)}]
    block = "[r]: http://e.example/" + "a" * 900 + "\n\n" + "[r] " * 50
    _publish(client, builder_page, code + _text_blocks([block + str(n) for n in range(8)]))
    reader = app.test_client()
    login(reader, make_user("rita"))
    first = reader.get("/page/target")
    assert first.status_code == 200 and b'class="codehilite"' in first.data
    calls = []
    for name in ("_render", "_highlight"):
        real = getattr(markdown, name)
        monkeypatch.setattr(markdown, name, lambda *args, _real=real, **kwargs: calls.append(1) or _real(*args, **kwargs))
    again = reader.get("/page/target")
    assert again.status_code == 200 and again.data.count(b"e.example") == first.data.count(b"e.example") > 0
    assert calls == []


# PW2-2: /search highlights only the category paths it shows


def test_pw2_2_search_highlights_only_the_categories_shown(app, client, db, make_user, login, monkeypatch):
    from bananawiki.wiki.features.pages import search

    db.execute("BEGIN")
    for chain in range(40):
        parent = None
        for level in range(4):
            parent = db.execute("INSERT INTO categories (name, parent_id) VALUES (?, ?)",
                                (f"c{chain:02d}l{level}" + "q" * 60, parent)).lastrowid
    db.execute("COMMIT")
    login(client, make_user("rita"))
    calls = []
    real = search.highlight
    monkeypatch.setattr(search, "highlight", lambda text, terms: calls.append(text) or real(text, terms))
    response = client.get("/search", query_string={"q": "qqqq -c39"})
    assert response.status_code == 200
    # 156 categories match (those of chain 39 are excluded): 50 are shown, and only those are highlighted.
    assert len(calls) == 50 == response.data.count(b'href="/category/')
    assert calls == sorted(calls, key=str.casefold) and not any("c39l" in path for path in calls)
    assert calls[0].startswith("c00l0") and "<mark>qqqq</mark>" in response.get_data(as_text=True)


# PW2-3: a withdrawn proposal keeps no text, and proposing and withdrawing are rate limited


def _cycle(client, db, user, slug, content):
    assert client.post(f"/page/{slug}/propose-edit",
                       data={"title": slug, "content": content, "reason": "r" * 2000}).status_code == 302
    row = db.one("SELECT id FROM pending_contributions WHERE user_id = ? AND status = 'pending' "
                 "AND page_id = (SELECT id FROM pages WHERE slug = ?)", (user["id"], slug))
    assert row is not None
    return client.post(f"/page/{slug}/contribution/{row['id']}/withdraw")


def test_pw2_3_withdrawn_proposals_keep_no_text(app, client, db, make_user, login):
    from tests.features.pages_support import make_page, set_feature

    set_feature(app, "contributions", True)
    for n in range(8):
        make_page(app, f"Vp {n}", "short")
    user = make_user("plain")
    login(client, user)
    for n in range(8):
        assert _cycle(client, db, user, f"vp-{n}", "B" * 100_000).status_code == 302
    rows = db.all("SELECT status, length(content) + length(reason) AS size FROM pending_contributions "
                  "WHERE user_id = ?", (user["id"],))
    assert [row["status"] for row in rows] == ["withdrawn"] * 8
    assert sum(row["size"] for row in rows) == 0


def test_pw2_3_proposing_and_withdrawing_are_rate_limited(app, client, db, make_user, login, frozen_limiter_clock):
    from tests.features.pages_support import make_page, set_feature

    set_feature(app, "contributions", True)
    make_page(app, "Vp", "short")
    user = make_user("plain")
    login(client, user)
    # 30 proposals and withdrawals a minute in all.
    for _ in range(15):
        assert _cycle(client, db, user, "vp", "B").status_code == 302
    refused = client.post("/page/vp/propose-edit", data={"title": "vp", "content": "B", "reason": "r"})
    assert refused.status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM pending_contributions WHERE user_id = ? AND status = 'pending'",
                     (user["id"],)) == 0


def test_pw2_3_a_withdrawn_proposal_does_not_show_as_deleting_the_page(app, client, db, make_user, login):
    from tests.features.pages_support import make_page, set_feature

    set_feature(app, "contributions", True)
    make_page(app, "Vp", "first line\nsecond line")
    user = make_user("plain")
    login(client, user)
    assert _cycle(client, db, user, "vp", "first line\nchanged line").status_code == 302
    contribution_id = db.scalar("SELECT id FROM pending_contributions WHERE user_id = ?", (user["id"],))
    reviewer = app.test_client()
    login(reviewer, make_user("boss", role="admin"))
    response = reviewer.get(f"/admin/contributions/{contribution_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Its text is gone: no diff against an empty text (every line of the page deleted).
    assert "diff__row--del" not in body and "second line" not in body
    assert "withdrew this proposal" in body
