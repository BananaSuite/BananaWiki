"""Re-audit pass 16 (VW1-x, VW2-x) regressions."""
import json

from bananawiki.wiki import permissions
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from tests.features.test_assessments import _create_quiz, _form, _questions, _submit_all


def test_vw1_1_deleting_a_category_does_not_move_pages_the_editor_may_not_move(app, client, make_user, login, db):
    admin = make_user("boss", role="admin")
    editor = make_user("editor1", role="editor")
    for access in ("read", "write"):
        db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, 0)",
                   (editor["id"], access))
    for key in set(permissions.defaults("editor")) - {"page.edit_metadata", "page.edit_all"}:
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (editor["id"], key))
    other = app.test_client()
    login(other, admin)
    other.post("/category/create", data={"name": "Minutes"})
    category = db.one("SELECT id FROM categories WHERE name = 'Minutes'")["id"]
    assert other.post("/create", data={"title": "Board minutes", "content": "x",
                                       "category_id": str(category)}).status_code == 302
    page = db.one("SELECT id, slug FROM pages WHERE title = 'Board minutes'")
    login(client, editor)
    assert client.post(f"/page/{page['slug']}/move", data={"category_id": ""}).status_code == 403
    for action in ("uncategorize", "move"):
        client.post(f"/category/{category}/delete", data={"page_action": action})
        assert db.one("SELECT category_id FROM pages WHERE id = ?", (page["id"],))["category_id"] == category
        assert db.one("SELECT 1 FROM categories WHERE id = ?", (category,))


def test_vw1_2_resaving_an_assessment_that_erases_answers_is_audited(app, client, make_user, login, db):
    with app.test_request_context(), connection_scope():
        page = pages.create("Fruit facts", "Bananas are berries.", author_id=None)
    editor = make_user("editor1", role="editor")
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    for name in ("reader1", "reader2"):
        login(client, make_user(name))
        assert _submit_all(client, page, rows).status_code == 302
        client.post("/logout")
    answers = db.scalar("SELECT COUNT(*) FROM assessment_answers")
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    login(client, editor)
    # The same quiz posted without question ids replaces every question, and their answers go with them.
    assert client.post(f"/page/{page['slug']}/assessment/manage", data=_form()).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM assessment_answers") == 0
    entry = db.one("SELECT * FROM audit_log WHERE id > ? AND action = 'assessment.saved'", (mark,))
    details = json.loads(entry["details"])
    assert entry["actor_id"] == editor["id"] and entry["target_id"] == str(page["id"])
    assert details["answers_removed"] == answers and details["questions_removed"] == len(rows)


def test_vw2_1_a_reset_after_a_change_of_owner_switches_off_webhooks(app, make_user, db):
    from bananawiki.ops import tenant_task
    from bananawiki.wiki.features.api_service import webhooks
    from tests.features.api_support import enable_api

    enable_api(app, db)
    make_user("wikiadmin", role="owner")
    backdoor = make_user("backdoor", role="admin")
    with app.test_request_context(), connection_scope():
        webhooks.create(url="https://previous-owner.example/hook?token=abc", events=["page.created"],
                        created_by=backdoor["id"])
    named = tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 67890"})
    assert named["webhooks_disabled"] == 0
    result = tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 67890",
                                                        "every_admin": True})
    assert result["webhooks_disabled"] == 1
    with app.test_request_context(), connection_scope():
        pages.create("New owner's secret plan", "body", author_id=None)
    assert db.scalar("SELECT active FROM api_service__webhooks") == 0
    assert db.scalar("SELECT COUNT(*) FROM api_service__webhook_deliveries") == 0
    entry = db.one("SELECT * FROM audit_log WHERE action = 'api_service.webhook_updated'")
    assert entry["target_type"] == "webhook" and "token" not in entry["details"]
    assert json.loads(entry["details"])["host"] == "previous-owner.example"
