"""Re-audit pass 20 (AW1-x, AW2-x) regressions."""
import json


def _board_with_comments(app, db, login, author, texts):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'all' WHERE id = 1")
    client = app.test_client()
    login(client, author)
    assert client.post("/kanban/create", data={"title": "Pay"}).status_code == 302
    board = db.scalar("SELECT id FROM kanban_boards ORDER BY id DESC LIMIT 1")
    column = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order", (board,))
    ticket = client.post(f"/api/kanban/columns/{column}/tickets", json={"title": "Invoice"}).get_json()["id"]
    return [client.post(f"/api/kanban/tickets/{ticket}/comments", json={"content": text}).get_json()["id"]
            for text in texts]


def test_aw1_1_administrators_cannot_rewrite_others_kanban_comments_and_deletions_are_audited(
        app, make_user, login, db):
    owner, admin, member = make_user("olivia", role="owner"), make_user("ada", role="admin"), make_user("bob", role="editor")
    on_owner = _board_with_comments(app, db, login, owner, ["Do NOT pay invoice 42."])
    on_member = _board_with_comments(app, db, login, member, ["first", "second"])
    client = app.test_client()
    login(client, admin)
    assert client.put(f"/api/kanban/comments/{on_owner[0]}", json={"content": "Approved. -- olivia"}).status_code == 403
    assert client.delete(f"/api/kanban/comments/{on_owner[0]}").status_code == 403
    assert db.scalar("SELECT content FROM kanban_ticket_comments WHERE id = ?", (on_owner[0],)) == "Do NOT pay invoice 42."
    assert client.put(f"/api/kanban/comments/{on_member[0]}", json={"content": "forged"}).status_code == 403
    assert client.delete(f"/api/kanban/comments/{on_member[1]}").status_code == 200
    entry = db.one("SELECT * FROM audit_log WHERE action = 'kanban.comment_deleted'")
    assert entry["actor_id"] == admin["id"] and json.loads(entry["details"])["author_id"] == member["id"]
    # Authors still edit and delete their own.
    login(client, member)
    assert client.put(f"/api/kanban/comments/{on_member[0]}", json={"content": "edited"}).status_code == 200
