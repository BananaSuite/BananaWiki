"""Regression tests for the findings of the final full re-audit of the Wiki core and features (FW1-1..FW1-4).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

import re

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import search, service
from bananawiki.wiki.features.temporary_accounts import service as temporary
from tests.features.pages_support import get_page, make_category, make_page, restrict, set_feature

# FW1-1


def _traced(monkeypatch) -> list[str]:
    """Every SQL statement run from now on, with its parameters filled in."""
    from bananawiki.core.sqlite import Database

    statements: list[str] = []
    connect = Database.connect

    def traced(self, *args, **kwargs):
        conn = connect(self, *args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "connect", traced)
    return statements


_EXCLUDED = " ".join(f"-zq{a}{a}" for a in "abcdefghijklmnopqrstuvw")


def test_fw1_1_punctuation_query_does_not_scan_page_text(app, admin_client, monkeypatch):
    make_page(app, "Salaries 2026", "confidential numbers.")
    statements = _traced(monkeypatch)
    for query in (f". {_EXCLUDED}", " ".join(["."] * 24), "title:.. -#"):
        statements.clear()
        assert admin_client.get("/search", query_string={"q": query}).status_code == 200
        scans = [sql for sql in statements if "FROM pages p" in sql and "LIKE" in sql]
        assert not scans, (query, scans)


def test_fw1_1_like_fallback_caps_the_terms(app, admin_client, monkeypatch):
    make_page(app, "Salaries 2026", "confidential numbers")
    monkeypatch.setattr(service, "fts_available", lambda: False)
    statements = _traced(monkeypatch)
    response = admin_client.get("/search", query_string={"q": f"confidential {_EXCLUDED}"})
    assert response.status_code == 200 and b"Salaries 2026" in response.data
    counted = [sql for sql in statements if sql.startswith("SELECT COUNT(*) FROM pages p")]
    assert counted
    assert len(set(re.findall(r"zq\w\w", counted[0]))) <= search.MAX_TERMS


def test_fw1_1_punctuation_is_ignored_but_filters_still_work(app, admin_client):
    hidden = make_category(app, "Hidden")
    make_page(app, "Salaries 2026", "confidential numbers", category_id=hidden["id"])
    make_page(app, "Payroll", "monthly runs", category_id=hidden["id"])
    make_page(app, "Elsewhere", "confidential too")

    def found(query: str) -> set[str]:
        response = admin_client.get("/search", query_string={"q": query, "type": "page"})
        assert response.status_code == 200
        return set(re.findall(r'class="search-result__title"[^>]*>([^<]*)<', response.get_data(as_text=True)))

    assert found(".") == set()
    assert found("category:Hidden .") == {"Salaries 2026", "Payroll"}
    assert found("category:Hidden -confidential") == {"Payroll"}
    assert found("confidential . -numbers") == {"Elsewhere"}
    assert found("confidential") == {"Salaries 2026", "Elsewhere"}


# FW1-2

_QUIZ = {
    "title": "Quiz", "description": "",
    "questions-0-prompt": "2 + 2 = ?", "questions-0-type": "single_choice",
    "questions-0-options": "3\n4\n5", "questions-0-correct": "4",
    "questions-0-points_correct": "1", "questions-0-points_incorrect": "0", "questions-0-required": "1",
}


def _quiz_with_attempt(app, client, login, make_user, db):
    """A page with a quiz written by *controller* and one attempt by a reader."""
    set_feature(app, "assessments", True)
    controller = make_user("controller", role="editor")
    reader = make_user("reader1")
    page = make_page(app, "Guarded", "text")
    login(client, controller)
    assert client.post(f"/page/{page['slug']}/assessment/manage", data=_QUIZ).status_code == 302
    client.post("/logout")
    question = db.one("SELECT id FROM assessment_questions")
    login(client, reader)
    client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{question['id']}": "1"})
    client.post("/logout")
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 1
    return page, controller, reader


def _assert_assessment_untouched(client, db, page, reader):
    slug = page["slug"]
    response = client.post(f"/page/{slug}/assessment/manage", data={**_QUIZ, "title": "Hijacked"})
    assert response.status_code == 302 and "/assessment/manage" not in response.headers["Location"]
    client.post(f"/page/{slug}/assessment/reset/{reader['id']}")
    client.post(f"/page/{slug}/assessment/delete")
    assert db.scalar("SELECT title FROM assessments") == "Quiz"
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 1
    # Reading the results stays allowed.
    assert client.get(f"/page/{slug}/assessment/results").status_code == 200


def test_fw1_2_protection_blocks_assessment_changes(app, client, login, make_user, db, admin):
    page, controller, reader = _quiz_with_attempt(app, client, login, make_user, db)
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE id = ?",
               (controller["id"], page["id"]))
    for intruder in (make_user("other", role="editor"), admin):
        login(client, intruder)
        _assert_assessment_untouched(client, db, page, reader)
        client.post("/logout")
    # The controller still manages it.
    login(client, controller)
    client.post(f"/page/{page['slug']}/assessment/manage", data={**_QUIZ, "title": "Renamed"})
    assert db.scalar("SELECT title FROM assessments") == "Renamed"


def test_fw1_2_check_out_and_pending_deletion_block_assessment_changes(app, client, login, make_user, db, admin):
    page, controller, reader = _quiz_with_attempt(app, client, login, make_user, db)
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_reservations_enabled = 1")
    login(client, controller)
    client.post(f"/page/{page['slug']}/reserve")
    assert db.scalar("SELECT COUNT(*) FROM page_reservations WHERE released_at IS NULL") == 1
    client.post("/logout")
    login(client, make_user("other", role="editor"))
    _assert_assessment_untouched(client, db, page, reader)
    client.post("/logout")
    # Pages pending deletion are frozen; holders of page.delete (here an administrator) still open them.
    db.execute("DELETE FROM page_reservations")
    db.execute("UPDATE pages SET pending_deletion = 1, pending_deletion_at = '2025-01-01 00:00:00' WHERE id = ?",
               (page["id"],))
    login(client, admin)
    _assert_assessment_untouched(client, db, page, reader)


# FW1-3


def _protect(app, db, page, controller):
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE id = ?",
               (controller["id"], page["id"]))


_HIDE = {"state": "hide", "expires_at": "2099-01-01T00:00"}


def test_fw1_3_temporary_visibility_respects_protection_and_pending_deletion(app, client, login, make_user, db,
                                                                            admin):
    set_feature(app, "temporary_accounts", True)
    guarded, doomed = make_page(app, "Guarded", "text"), make_page(app, "Doomed", "text")
    _protect(app, db, guarded, make_user("controller", role="editor"))
    db.execute("UPDATE pages SET pending_deletion = 1, pending_deletion_at = '2025-01-01 00:00:00' WHERE id = ?",
               (doomed["id"],))
    login(client, admin)
    for page in (guarded, doomed):
        client.post(f"/page/{page['slug']}/deindex")
        assert get_page(app, page["id"])["is_deindexed"] == 0
        client.post("/admin/temporary/visibility", data={"page_slug": page["slug"], **_HIDE})
        assert get_page(app, page["id"])["is_deindexed"] == 0
    assert db.scalar("SELECT COUNT(*) FROM temp_page_index_state") == 0


def test_fw1_3_visibility_restore_waits_while_the_page_is_protected(app, client, login, make_user, db, admin):
    set_feature(app, "temporary_accounts", True)
    page = make_page(app, "Guarded", "text")
    login(client, admin)
    client.post("/admin/temporary/visibility", data={"page_slug": page["slug"], **_HIDE})
    assert get_page(app, page["id"])["is_deindexed"] == 1
    _protect(app, db, page, make_user("controller", role="editor"))
    db.execute("UPDATE temp_page_index_state SET expires_at = ?", (sql_in(minutes=-1),))
    with app.app_context(), connection_scope():
        temporary.run_expiry()
    assert get_page(app, page["id"])["is_deindexed"] == 1
    assert db.scalar("SELECT COUNT(*) FROM temp_page_index_state") == 1
    db.execute("UPDATE pages SET protected_by = NULL, protected_at = NULL WHERE id = ?", (page["id"],))
    with app.app_context(), connection_scope():
        temporary.run_expiry()
    assert get_page(app, page["id"])["is_deindexed"] == 0
    assert db.scalar("SELECT COUNT(*) FROM temp_page_index_state") == 0


# FW1-4


def _stale_check_out(app, client, login, make_user, db):
    """Mallory checks out a page, which then moves into a category she cannot read and is renamed."""
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_reservations_enabled = 1")
    open_category, secret = make_category(app, "Open"), make_category(app, "Secret")
    mallory = make_user("mallory", role="editor")
    restrict(db, mallory, read=[open_category["id"]], write=[open_category["id"]])
    page = make_page(app, "Draft plan", "text", category_id=open_category["id"])
    login(client, mallory)
    client.post(f"/page/{page['slug']}/reserve")
    assert db.scalar("SELECT COUNT(*) FROM page_reservations WHERE user_id = ?", (mallory["id"],)) == 1
    db.execute("UPDATE pages SET category_id = ?, title = 'Layoffs Q3 list', slug = 'layoffs-q3-list' WHERE id = ?",
               (secret["id"], page["id"]))
    assert client.get("/page/layoffs-q3-list").status_code in (403, 404)
    return page, mallory


def test_fw1_4_quota_page_hides_check_outs_of_unreadable_pages(app, client, login, make_user, db):
    _stale_check_out(app, client, login, make_user, db)
    body = client.get("/settings/reservation-quota").get_data(as_text=True)
    assert "Layoffs Q3 list" not in body and "layoffs-q3-list" not in body


def test_fw1_4_stale_check_out_stops_blocking_and_admin_still_releases_it(app, client, login, make_user, db,
                                                                         admin):
    page, mallory = _stale_check_out(app, client, login, make_user, db)
    client.post("/logout")
    login(client, make_user("other", role="editor"))
    client.post("/page/layoffs-q3-list/edit", data={"content": "changed", "revision": 1})
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "changed"
    client.post("/logout")
    # Administrators still see the row and end it.
    login(client, admin)
    assert "Layoffs Q3 list" in client.get("/admin/checkouts").get_data(as_text=True)
    assert client.post(f"/admin/checkouts/{page['id']}/release").status_code == 302
    assert db.scalar("SELECT released_at FROM page_reservations WHERE page_id = ?", (page["id"],)) is not None
