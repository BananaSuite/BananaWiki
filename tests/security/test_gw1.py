"""Regression tests for the findings of the second full re-audit of the Wiki core (GW1-1).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

from bananawiki.wiki.features.pages import service
from tests.features.pages_support import make_page
from tests.security.test_fw1 import _traced

# GW1-1


def test_gw1_1_short_words_are_not_prefix_searches(app, admin_client, monkeypatch):
    make_page(app, "Garden notes", "an ab abacus abbey")
    make_page(app, "Kitchen notes", "abacus only")
    statements = _traced(monkeypatch)

    def found(query):
        response = admin_client.get("/api/sidebar/search", query_string={"q": query, "scope": "content"})
        return {page["title"] for page in response.get_json()["pages"]}

    assert found("ab") == {"Garden notes"}
    assert admin_client.get("/search", query_string={"q": "ab"}).status_code == 200
    matches = [sql for sql in statements if "MATCH" in sql]
    assert matches and not any('"ab"*' in sql for sql in matches)
    # Three letters and more still find longer words.
    assert found("aba") == {"Garden notes", "Kitchen notes"}
    assert service.fts_phrase(["a"]) == '"a"' and service.fts_phrase(["abc"]) == '"abc"*'


def test_gw1_1_excerpts_are_made_for_the_rows_shown_only(app, admin_client, monkeypatch):
    for number in range(25):
        make_page(app, f"Report {number:02d}", "quarterly figures and notes")
    statements = _traced(monkeypatch)
    for url, query in (("/search", {"q": "quarterly", "page": 2}),
                       ("/api/sidebar/search", {"q": "quarterly", "scope": "content"})):
        statements.clear()
        assert admin_client.get(url, query_string=query).status_code == 200
        # Excerpts are made in Python from the start of the pages shown (LW1-1: FTS5 snippet() was quadratic).
        assert not any("snippet(" in sql for sql in statements)
        excerpts = [sql for sql in statements if "substr(content, 1, " in sql]
        # Sorting with the excerpt in the result made SQLite compute it for every matching page.
        assert excerpts and not any("ORDER BY" in sql for sql in excerpts), excerpts
        assert all("WHERE id IN" in sql for sql in excerpts)
    page_two = admin_client.get("/search", query_string={"q": "quarterly", "page": 2}).get_data(as_text=True)
    assert "Report" in page_two and "quarterly" in page_two
