"""Regression tests for the IW1 findings of the full re-audit pass 4 (search word padding, kanban columns)."""

from __future__ import annotations

from bananawiki.wiki.features.pages import service
from tests.features.pages_support import make_page
from tests.security.test_fw1 import _traced
from tests.security.test_hw1 import _board_with_writer

# IW1-1: underscores do not make a short search word long enough to be a prefix


def test_iw1_1_padded_short_words_are_not_prefix_searches(app, admin_client, monkeypatch):
    make_page(app, "Garden notes", "an ab abacus")
    make_page(app, "Kitchen notes", "abacus only")
    statements = _traced(monkeypatch)
    for query in ("ab__", "_ab_", "__ab", "a_b"):
        statements.clear()
        response = admin_client.get("/api/sidebar/search", query_string={"q": query, "scope": "content"})
        assert "Kitchen notes" not in {page["title"] for page in response.get_json()["pages"]}, query
        assert admin_client.get("/search", query_string={"q": query}).status_code == 200
        matches = [sql for sql in statements if "MATCH" in sql]
        assert matches and not any('"ab"*' in sql or '"a"*' in sql or '"b"*' in sql for sql in matches), query
    assert service.fts_phrase(["a__"]) == '"a"' and service.fts_phrase(["abc_"]) == '"abc"*'


# IW1-2: a board's columns are capped like an import's


def test_iw1_2_columns_are_capped_on_create_and_revert(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.kanban import fields

    monkeypatch.setattr(fields, "MAX_COLUMNS_PER_BOARD", 6)
    board_id, _column_id = _board_with_writer(app, client, make_user, login, db)

    def columns():
        return db.column("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY id", (board_id,))

    while len(columns()) < 6:
        assert client.post(f"/api/kanban/{board_id}/columns", json={"title": "c"}).status_code in (200, 201)
    refused = client.post(f"/api/kanban/{board_id}/columns", json={"title": "one more"})
    assert refused.status_code == 409 and "at most 6 columns" in refused.get_json()["error"]
    assert len(columns()) == 6

    # A version with more columns than the cap (saved before it was lowered) is not restored over it.
    snapshot = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board_id,))
    monkeypatch.setattr(fields, "MAX_COLUMNS_PER_BOARD", 3)
    for column_id in columns()[3:]:
        assert client.delete(f"/api/kanban/columns/{column_id}").status_code == 200
    kept = columns()
    response = client.post(f"/kanban/{board_id}/revert/{snapshot}", follow_redirects=True)
    assert b"at most 3 columns" in response.data
    assert columns() == kept
