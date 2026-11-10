"""Regression tests for the q3 findings of the full re-audit pass 11 (page history, attachments, custom pages)."""

from __future__ import annotations

import io
import re

import pytest

from bananawiki.wiki.db import connection_scope

# QW2-3: the history list measures only the versions it shows


@pytest.fixture
def length_calls(monkeypatch):
    """Count the calls of SQL ``length()`` made through the app's connections."""
    from bananawiki.core import sqlite as core_sqlite

    calls = {"n": 0}
    connect = core_sqlite.Database.connect

    def counting(self, *args, **kwargs):
        conn = connect(self, *args, **kwargs)

        def length(value):
            calls["n"] += 1
            return None if value is None else len(value)

        conn.create_function("length", 1, length, deterministic=True)
        return conn

    monkeypatch.setattr(core_sqlite.Database, "connect", counting)
    return calls


def _page_with_versions(app, db, count: int) -> int:
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        service.create("Big", "x", author_id=None)
    page_id = db.scalar("SELECT id FROM pages WHERE slug = 'big'")
    have = db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page_id,))
    for i in range(have, count):
        db.execute("INSERT INTO page_history (page_id, title, content) VALUES (?, 'Big', ?)", (page_id, "a" * (i + 1)))
    return page_id


def _deltas(html: str) -> list[str]:
    return re.findall(r'<td class="nowrap small hide-sm"><span[^>]*>([^<]+)</span>', html)


def test_qw2_3_history_list_measures_only_the_shown_versions(app, db, make_user, login, length_calls):
    _page_with_versions(app, db, 300)
    reader = app.test_client()
    login(reader, make_user("reader"))
    length_calls["n"] = 0
    response = reader.get("/page/big/history?page=3")
    assert response.status_code == 200
    assert length_calls["n"] <= 60, "only the 50 shown versions and the one before them are measured"


def test_qw2_3_history_list_still_shows_each_change_in_size(app, db, make_user, login):
    _page_with_versions(app, db, 60)
    reader = app.test_client()
    login(reader, make_user("reader"))
    first = _deltas(reader.get("/page/big/history").get_data(as_text=True))
    assert len(first) == 50 and set(first) == {"+1"}, "the oldest shown version compares with the one before it"
    second = _deltas(reader.get("/page/big/history?page=2").get_data(as_text=True))
    assert len(second) == 10 and second[-1] == "+1", "the first version shows its size"


def test_qw2_3_history_list_without_versions_still_answers(app, db, make_user, login):
    page_id = _page_with_versions(app, db, 1)
    db.execute("DELETE FROM page_history WHERE page_id = ?", (page_id,))
    reader = app.test_client()
    login(reader, make_user("reader"))
    assert reader.get("/page/big/history").status_code == 200
    assert reader.get("/page/big/history?page=9").status_code == 200


def test_qw2_3_history_list_has_a_per_route_limit(app, db, make_user, login, frozen_limiter_clock):
    _page_with_versions(app, db, 1)
    reader = app.test_client()
    login(reader, make_user("reader"))
    codes = [reader.get("/page/big/history").status_code for _ in range(31)]
    assert codes == [200] * 30 + [429]



# QW2-4: a page holds a bounded number of attachments, and the lists check delete rights once


def _attach_page(app):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        service.create("Docs", "body", author_id=None)


def _upload(client, names: list[str]):
    return client.post("/page/docs/attachments", content_type="multipart/form-data",
                       data={"files": [(io.BytesIO(b"y"), name) for name in names]})


def test_qw2_4_uploads_stop_at_the_per_page_limit(app, db, make_user, login, monkeypatch):
    from bananawiki.wiki.features.attachments import service

    monkeypatch.setattr(service, "MAX_PER_PAGE", 5)
    _attach_page(app)
    editor = app.test_client()
    login(editor, make_user("editorone", role="editor"))
    assert _upload(editor, [f"a{i}.txt" for i in range(8)]).status_code == 302
    page_id = db.scalar("SELECT id FROM pages WHERE slug = 'docs'")
    assert db.scalar("SELECT COUNT(*) FROM page_attachments WHERE page_id = ?", (page_id,)) == 5
    response = editor.post(f"/api/page/{page_id}/attachments", content_type="multipart/form-data",
                           data={"file": (io.BytesIO(b"y"), "late.txt")}, headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert response.get_json()["error"].startswith("This page has reached its limit of 5 attachments")
    assert db.scalar("SELECT COUNT(*) FROM page_attachments WHERE page_id = ?", (page_id,)) == 5


def test_qw2_4_page_view_lists_a_bounded_number_of_attachments(app, db, make_user, login, monkeypatch):
    from bananawiki.wiki.features.attachments import service

    _attach_page(app)
    editor = app.test_client()
    login(editor, make_user("editortwo", role="editor"))
    assert _upload(editor, ["first.txt"]).status_code == 302
    row = db.one("SELECT * FROM page_attachments LIMIT 1")
    for i in range(9):  # files planted before the limit existed
        db.execute("INSERT INTO page_attachments (page_id, filename, original_name, file_size, uploaded_by, uploaded_at)"
                   " VALUES (?, ?, ?, 1, ?, ?)", (row["page_id"], row["filename"], f"old{i}.txt", row["uploaded_by"],
                                                  row["uploaded_at"]))
    monkeypatch.setattr(service, "MAX_PER_PAGE", 4)
    checks = {"n": 0}
    may_change = service._may_change

    def counting(page, user):
        checks["n"] += 1
        return may_change(page, user)

    monkeypatch.setattr(service, "_may_change", counting)
    html = editor.get("/page/docs").get_data(as_text=True)
    assert html.count('class="attachments__item"') == 4
    assert "(10)" in html, "the heading still counts every file"
    assert html.count("btn--danger-quiet") >= 4, "the editor may still delete the files"
    assert checks["n"] <= 3, "edit rights are checked once per list, not once per file"


# QW2-5: custom pages created, changed and deleted are audited


def _audit_rows(db, start: int) -> list[dict]:
    import json

    return [{**row, "details": json.loads(row["details"])} for row in
            db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? ORDER BY id",
                   (start,))]


def _custom_form(url: str) -> dict:
    return {"path": "/sign-in-help", "title": "Help", "content_type": "redirect", "redirect_url": url,
            "is_published": "1"}


def test_qw2_5_custom_page_changes_are_audited(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    admin = make_user("admintwo", role="admin")
    client = app.test_client()
    login(client, admin)
    start = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    assert client.post("/admin/custom-pages/create", data=_custom_form("https://attacker.example/a"),
                       content_type="multipart/form-data").status_code == 302
    page_id = db.scalar("SELECT id FROM custom_pages WHERE path = '/sign-in-help'")
    assert client.post(f"/admin/custom-pages/{page_id}/edit", content_type="multipart/form-data",
                       data={**_custom_form("https://other.example/b"),
                             "file": (io.BytesIO(b"hello"), "note.txt")}).status_code == 302
    file_id = db.scalar("SELECT id FROM custom_page_files WHERE custom_page_id = ?", (page_id,))
    assert file_id is not None
    assert client.post(f"/admin/custom-pages/files/{file_id}/delete").status_code == 302
    assert client.post(f"/admin/custom-pages/{page_id}/delete").status_code == 302

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == ["custom_page.created", "custom_page.updated", "custom_page.file_added",
                                               "custom_page.file_deleted", "custom_page.deleted"]
    assert {(row["actor_id"], row["target_type"], row["target_id"]) for row in rows} == \
        {(str(admin["id"]), "custom_page", str(page_id))}
    created, updated = rows[0]["details"], rows[1]["details"]
    assert created == {"path": "/sign-in-help", "content_type": "redirect", "is_published": 1,
                       "redirect_host": "attacker.example"}
    assert updated["redirect_host"] == "other.example"
    assert updated["previous"] == {"redirect_host": "attacker.example"}
    assert rows[2]["details"]["name"] == "note.txt"


def test_qw2_5_custom_page_builder_saves_are_audited(app, db, make_user, login):
    from bananawiki.wiki.features.custom_pages import service
    from bananawiki.wiki.features.page_builder import document

    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    admin = make_user("adminthree", role="admin")
    with app.test_request_context(), connection_scope():
        page_id = service.create({"path": "/built", "title": "Built", "content_type": "wiki_page", "is_published": 0,
                                  "content": "", "builder_json": document.dump(document.empty())},
                                 created_by=admin["id"])
    start = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    with app.test_request_context(), connection_scope():
        service.save_builder_document(service.get(page_id), document.empty(), title="Built again")
    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == ["custom_page.updated"]
    assert "title" in rows[0]["details"]["changed"]


def test_qw2_5_an_impersonated_custom_page_change_names_both_accounts(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    superuser = make_user("superone", role="admin", is_superuser=1)
    member = make_user("admininner", role="admin")
    client = app.test_client()
    login(client, superuser)
    assert client.post(f"/admin/users/{member['id']}/impersonate").status_code == 302
    start = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    assert client.post("/admin/custom-pages/create", data=_custom_form("https://attacker.example/a"),
                       content_type="multipart/form-data").status_code == 302
    rows = [row for row in _audit_rows(db, start) if row["action"] == "custom_page.created"]
    assert len(rows) == 1
    assert rows[0]["actor_id"] == str(superuser["id"])
    assert rows[0]["details"]["as_user"] == "admininner"
