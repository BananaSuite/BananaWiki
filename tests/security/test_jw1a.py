"""Regression tests for the JW1 findings of the full re-audit pass 5 (search words the tokenizer drops, kanban
restores, protection after a demotion)."""

from __future__ import annotations

from bananawiki.wiki.features.pages import service
from tests.features.pages_support import make_page
from tests.security.test_fw1 import _traced
from tests.security.test_hw1 import _board_with_writer, _protected_page

# JW1-3: letters newer than the tokenizer's Unicode 6.1 do not make a short search word long enough to be a prefix

# New Tai Lue vowel signs and Vedic signs: letters to Python, separators to SQLite's unicode61 tokenizer.
_DROPPED = ("ᦰ", "ᧀ", "ᧈ", "ᳲ", "ᳳ")


def test_jw1_3_letters_the_index_ignores_do_not_pad_short_words(app, admin_client, monkeypatch):
    make_page(app, "Garden notes", "an ab abacus")
    make_page(app, "Kitchen notes", "abacus only")
    statements = _traced(monkeypatch)
    for char in _DROPPED:
        query = "ab" + char * 2
        statements.clear()
        response = admin_client.get("/api/sidebar/search", query_string={"q": query, "scope": "content"})
        assert "Kitchen notes" not in {page["title"] for page in response.get_json()["pages"]}, ascii(query)
        assert admin_client.get("/search", query_string={"q": query}).status_code == 200
        matches = [sql for sql in statements if "MATCH" in sql]
        assert matches and not any('"ab"*' in sql or '"a"*' in sql for sql in matches), ascii(query)
        assert service.fts_phrase(["a" + char * 2]) == '"a"'
    # Ordinary words are split and folded exactly as before.
    assert service.fts_phrase(["a__"]) == '"a"' and service.fts_phrase(["Abc_"]) == '"abc"*'
    assert service.fts_phrase(["Héllo wörld"]) == '"hello world"*'
    assert service.fts_phrase([_DROPPED[0] * 3]) == '""'


def test_jw1_3_the_words_are_split_with_the_index_tokenizer(app, db):
    assert service.FTS_TOKENIZE in db.scalar("SELECT sql FROM sqlite_master WHERE name = 'pages_fts'")


# JW1-4: restoring a board version is rate limited like an import or an export


def test_jw1_4_kanban_restores_are_rate_limited(app, client, make_user, login, db):
    board_id, _column_id = _board_with_writer(app, client, make_user, login, db)
    entry_id = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board_id,))
    statuses = [client.post(f"/kanban/{board_id}/revert/{entry_id}").status_code for _ in range(11)]
    assert statuses[:10] == [302] * 10 and statuses[10] == 429


# JW1-5: a demotion ends the controller's protections for good


def test_jw1_5_demoting_and_promoting_the_controller_does_not_restore_protection(app, client, login, make_user, db):
    from tests.features.pages_support import get_page

    controller = make_user("controller", role="editor")
    administrator = make_user("plainadmin", role="admin")
    page = _protected_page(app, db, controller)
    login(client, administrator)
    client.post(f"/page/{page['slug']}/edit", data={"content": "blocked text", "revision": 1})
    assert get_page(app, page["id"])["content"] == "text"

    assert client.post(f"/admin/users/{controller['id']}/assign-role", data={"identity": "std:user"}).status_code == 302
    client.post(f"/page/{page['slug']}/edit", data={"content": "admin text", "revision": 1})
    assert client.post(f"/admin/users/{controller['id']}/assign-role",
                       data={"identity": "std:editor"}).status_code == 302

    row = db.one("SELECT protected_by, protected_at FROM pages WHERE id = ?", (page["id"],))
    assert row == {"protected_by": None, "protected_at": None}
    lapsed = db.one("SELECT actor_id, details FROM audit_log WHERE action = 'page_governance.protection_lapsed'")
    assert lapsed["actor_id"] == administrator["id"] and page["slug"] in lapsed["details"]
