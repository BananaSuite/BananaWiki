"""Regression tests for the s2 findings of the full re-audit pass 13 (drafts and category deletion
memory, the "My drafts" list, audit entries for draft take-overs)."""

from __future__ import annotations

import json
import tracemalloc

from bananawiki.core.timeutil import now_sql
from tests.features.pages_support import in_app, make_category, make_page, restrict

DRAFTS = 20
BIG = 100_000  # characters of each large draft or page: DRAFTS of them make 2 MB
PEAK_LIMIT = 1_000_000  # what one request may hold at once: a few of them, never all


def _peak(fn, warm_up=None) -> int:
    """The most memory *fn* held at once, after *warm_up* (templates compiled, caches filled)."""
    if warm_up is not None:
        warm_up()
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _insert_drafts(db, user, pages, content: str) -> None:
    for page in pages:
        db.execute("INSERT INTO drafts (page_id, user_id, title, content, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (page["id"], user["id"], page["title"], content, now_sql()))


def _audit_rows(db, start: int) -> list[dict]:
    return [{**row, "details": json.loads(row["details"])} for row in
            db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? ORDER BY id",
                   (start,))]


def _last_audit_id(db) -> int:
    return db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")


# SW2-1: a rename (or an account merge) rewrites the drafts one at a time


def test_sw2_1_rename_rewrites_drafts_one_at_a_time(app, admin_client, admin, db):
    make_page(app, "Target")
    pages = [make_page(app, f"Linker {number}") for number in range(DRAFTS)]
    _insert_drafts(db, admin, pages, "see [t](/page/target) " + "x" * BIG)

    peak = _peak(lambda: admin_client.post("/page/target/rename", data={"new_slug": "moved"}),
                 lambda: admin_client.post("/page/linker-0/rename", data={"new_slug": "linker-0"}))

    assert db.scalar("SELECT slug FROM pages WHERE title = 'Target'") == "moved"
    assert db.scalar("SELECT COUNT(*) FROM drafts WHERE content LIKE 'see [t](/page/moved) %'") == DRAFTS
    assert peak < PEAK_LIMIT, f"the rename held {peak} bytes at once"


def test_sw2_1_account_merge_rewrites_drafts_one_at_a_time(app, admin, db):
    from bananawiki.wiki.features.pages import service

    pages = [make_page(app, f"Mentions {number}") for number in range(DRAFTS)]
    _insert_drafts(db, admin, pages, "thanks @oldname " + "x" * BIG)

    peak = _peak(lambda: in_app(app, lambda: service.rewrite_mentions("oldname", "@newname")))

    assert db.scalar("SELECT COUNT(*) FROM drafts WHERE content LIKE 'thanks @newname %'") == DRAFTS
    assert peak < PEAK_LIMIT, f"the merge held {peak} bytes at once"


# SW2-2: deleting a category checks its pages without reading their text


def test_sw2_2_refused_category_deletion_does_not_load_the_texts(app, client, make_user, login, db):
    category = make_category(app, "Mine")
    for number in range(DRAFTS):
        make_page(app, f"Big {number}", "x" * BIG, category_id=category["id"])
    editor = make_user("s2_editor", role="editor")  # default editor: category.delete, no page.delete
    login(client, editor)

    def attempt():
        return client.post(f"/category/{category['id']}/delete", data={"page_action": "delete"})

    peak = _peak(attempt, attempt)

    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (category["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category["id"],)) == DRAFTS
    assert peak < PEAK_LIMIT, f"the refusal held {peak} bytes at once"


def test_sw2_2_category_deletion_with_pages_still_deletes_them(app, admin_client, db):
    category = make_category(app, "Old")
    for number in range(DRAFTS):
        make_page(app, f"Old {number}", "x" * BIG, category_id=category["id"])
    start = _last_audit_id(db)

    warm = make_category(app, "Warm up")
    make_page(app, "Warm page", category_id=warm["id"])
    peak = _peak(lambda: admin_client.post(f"/category/{category['id']}/delete", data={"page_action": "delete"}),
                 lambda: admin_client.post(f"/category/{warm['id']}/delete", data={"page_action": "delete"}))

    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (category["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title LIKE 'Old %'") == 0
    deleted = [row for row in _audit_rows(db, start) if row["action"] == "page.deleted"
               and row["details"].get("title", "").startswith("Old ")]
    assert len(deleted) == DRAFTS
    assert peak < PEAK_LIMIT, f"the deletion held {peak} bytes at once"


# SW2-3: "My drafts" is paged, does not measure the drafts and is rate limited


def test_sw2_3_my_drafts_is_paged_and_does_not_measure_the_texts(app, client, make_user, login, db):
    from bananawiki.wiki.features.drafts import service

    editor = make_user("s2_drafter", role="editor")
    pages = [make_page(app, f"Draft page {number:02d}") for number in range(60)]
    _insert_drafts(db, editor, pages, "draft text")
    login(client, editor)

    first = client.get("/drafts").get_data(as_text=True)
    second = client.get("/drafts?page=2").get_data(as_text=True)
    assert first.count("/discard") == 50 and second.count("/discard") == 10
    assert "?page=2" in first
    assert len(client.get("/api/draft/mine", headers={"Accept": "application/json"}).get_json()) == 60
    rows = in_app(app, lambda: service.of_user(editor["id"], limit=5))
    assert len(rows) == 5 and all("size" not in row for row in rows)


def test_sw2_3_my_drafts_is_rate_limited(app, client, make_user, login):
    editor = make_user("s2_reloader", role="editor")
    login(client, editor)
    codes = [client.get("/drafts").status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


# SW2-5: taking over another account's draft is audited, also while impersonating


def _editor_draft(app, client, make_user, login, db, username: str, text: str):
    editor = make_user(username, role="editor")
    login(client, editor)
    assert client.post("/create", data={"title": "Notes", "content": "public text"}).status_code == 302
    page = db.one("SELECT id, revision FROM pages WHERE slug = 'notes'")
    response = client.post("/api/draft/save", json={"page_id": page["id"], "title": "Notes", "content": text,
                                                    "revision": page["revision"]})
    assert response.get_json()["status"] == "saved"
    return editor, page


def test_sw2_5_draft_takeover_is_audited(app, make_user, login, db):
    owner_client, admin_client = app.test_client(), app.test_client()
    editor, page = _editor_draft(app, owner_client, make_user, login, db, "s2_owner", "PRIVATE unsaved")
    admin = make_user("s2_admin", role="admin")
    login(admin_client, admin)
    start = _last_audit_id(db)

    body = {"page_id": page["id"], "from_user_id": editor["id"]}
    assert admin_client.post("/api/draft/transfer", json=body).status_code == 200
    owner_client.post("/api/draft/save", json={"page_id": page["id"], "title": "Notes", "content": "second"})
    assert admin_client.post(f"/drafts/{page['id']}/transfer",
                             data={"from_user_id": editor["id"]}).status_code == 302
    assert admin_client.post("/api/draft/transfer", json=body).status_code == 404  # nothing left: no entry

    rows = _audit_rows(db, start)
    assert [(row["action"], row["actor_id"], row["target_type"], row["target_id"]) for row in rows] == \
        [("draft.taken_over", str(admin["id"]), "page", str(page["id"]))] * 2
    assert rows[0]["details"] == {"title": "Notes", "from": "s2_owner", "to": "s2_admin",
                                  "size": len("PRIVATE unsaved")}
    assert rows[1]["details"]["size"] == len("second")
    assert "PRIVATE" not in json.dumps([row["details"] for row in rows])


def test_sw2_5_draft_takeover_while_impersonating_is_audited(app, make_user, login, db):
    owner_client, admin_client = app.test_client(), app.test_client()
    editor, page = _editor_draft(app, owner_client, make_user, login, db, "s2_victim", "PRIVATE unsaved")
    helper = make_user("s2_helper", role="editor")
    from bananawiki.wiki import permissions

    restrict(db, helper, keys={*permissions.defaults("editor"), "draft.transfer"})
    admin = make_user("s2_padmin", role="admin")
    login(admin_client, admin)
    assert admin_client.post(f"/admin/users/{helper['id']}/impersonate").status_code == 302
    start = _last_audit_id(db)

    body = {"page_id": page["id"], "from_user_id": editor["id"]}
    assert admin_client.post("/api/draft/transfer", json=body).status_code == 200

    rows = _audit_rows(db, start)
    assert [(row["action"], row["actor_id"]) for row in rows] == \
        [("draft.taken_over", str(admin["id"])), ("page.impersonated_action", str(admin["id"]))]
    assert rows[0]["details"]["as_user"] == "s2_helper" and rows[0]["details"]["to"] == "s2_helper"
    assert rows[1]["details"]["endpoint"] == "drafts.api_transfer"
