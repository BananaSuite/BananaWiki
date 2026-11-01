"""Full re-audit pass 11, group q4: Kanban history volume, import volume, error-log addresses, profile audit."""

from __future__ import annotations

import io
import json


def _history_rows(db, board_id):
    return db.scalar("SELECT COUNT(*) FROM kanban_board_history WHERE board_id = ?", (board_id,))


def _import_board(client, db, tickets, description=""):
    doc = {"title": "b", "columns": [{"title": "c", "tickets": [
        {"title": f"t{n}", "description": description} for n in range(tickets)]}]}
    answer = client.post("/kanban/import", content_type="multipart/form-data",
                         data={"import_file": (io.BytesIO(json.dumps(doc).encode()), "b.kanban.json")})
    assert answer.status_code == 302, answer.data[:300]
    board_id = db.scalar("SELECT MAX(id) FROM kanban_boards")
    ticket = db.scalar("SELECT t.id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
                       "WHERE c.board_id = ? ORDER BY t.id LIMIT 1", (board_id,))
    return board_id, ticket


def _editor(client, make_user, login, db):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    user = make_user("eve", role="editor")
    login(client, user)
    return user


# QW3-1: a board just under LARGE_BOARD_BYTES stored a full snapshot on every edit


def test_qw3_1_a_board_under_the_large_size_is_not_snapshotted_on_every_edit(client, make_user, login, db,
                                                                              monkeypatch):
    from bananawiki.wiki.features.kanban import history

    monkeypatch.setattr(history, "LARGE_BOARD_BYTES", 100_000)  # 98 KB stands for a 0.98 MB board
    _editor(client, make_user, login, db)
    board_id, ticket = _import_board(client, db, 10, "x" * 9_700)
    before = _history_rows(db, board_id)
    for n in range(20):
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"edit {n}"}).status_code == 200
    assert _history_rows(db, board_id) == before, "the import's entry is under a minute old"

    # A small board still gets an entry for each change.
    board_id, ticket = _import_board(client, db, 2)
    before = _history_rows(db, board_id)
    for n in range(3):
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"small {n}"}).status_code == 200
    assert _history_rows(db, board_id) == before + 3


# QW3-2: an administrator's direct edits of a member's public profile were not audited


def test_qw3_2_admin_profile_edits_are_audited_without_their_values(client, make_user, login, db):
    ada = make_user("ada", role="admin")
    bob = make_user("bob")
    login(client, ada)
    assert client.post("/admin/profile-fields", data={"action": "create", "label": "Office",
                                                      "field_type": "text"}).status_code == 302
    field = db.scalar("SELECT id FROM user_profile_fields__definitions ORDER BY id DESC LIMIT 1")
    since = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    posts = [
        (f"/admin/users/{bob['id']}/profile", {"action": "edit_profile", "real_name": "Bob",
                                                "bio": "I resign. -- bob", "birth_date": ""}),
        (f"/admin/users/{bob['id']}/profile", {"action": "publish_profile"}),
        (f"/admin/users/{bob['id']}/profile-fields", {f"value_{field}": "Basement", f"visible_{field}": "1"}),
        (f"/admin/users/{bob['id']}/tags", {"action": "add_tag", "tag_label": "liar", "tag_color": "#ff0000"}),
    ]
    for url, form in posts:
        assert client.post(url, data=form).status_code == 302
    rows = db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? "
                  "ORDER BY id", (since,))
    assert [(r["action"], r["actor_id"], r["target_type"], r["target_id"]) for r in rows] == [
        ("user.profile_moderated", ada["id"], "user", bob["id"])] * 4
    details = [json.loads(r["details"]) for r in rows]
    assert [d["action"] for d in details] == ["edit_profile", "publish_profile", "profile_fields", "add_tag"]
    assert details[1]["changed"] == "page_published"
    assert details[0]["changed"] == "real_name, bio" and details[2]["changed"] == "Office"
    assert "resign" not in str(details) and "Basement" not in str(details) and "liar" not in str(details)


def test_qw3_2_a_profile_edit_with_a_refused_avatar_is_audited(client, make_user, login, db):
    ada = make_user("ada", role="admin")
    bob = make_user("bob")
    login(client, ada)
    since = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    answer = client.post(f"/admin/users/{bob['id']}/profile", content_type="multipart/form-data", data={
        "action": "edit_profile", "real_name": "Bob", "bio": "I resign. -- bob", "birth_date": "",
        "avatar": (io.BytesIO(b"not an image at all"), "a.png")})
    assert answer.status_code == 302
    assert db.scalar("SELECT bio FROM user_profiles WHERE user_id = ?", (bob["id"],)) == "I resign. -- bob"
    rows = db.all("SELECT action, actor_id, target_id, details FROM audit_log WHERE id > ?", (since,))
    assert [(r["action"], r["actor_id"], r["target_id"]) for r in rows] == [
        ("user.profile_moderated", ada["id"], bob["id"])]
    assert json.loads(rows[0]["details"])["changed"] == "real_name, bio"
    # A refused change that saved nothing records nothing.
    since = db.scalar("SELECT MAX(id) FROM audit_log")
    assert client.post(f"/admin/users/{bob['id']}/profile", data={"action": "nonsense_profile"}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE id > ?", (since,)) == 0


# QW3-3: the error log showed plain administrators the sign-in addresses of owners and superusers


def test_qw3_3_the_error_log_hides_addresses_from_plain_administrators(app, make_user, login):
    from pathlib import Path

    from bananawiki.wiki.features.site_admin import server

    path = Path(app.config["BW"].log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "2026-10-28 09:52:45,354 INFO [bananawiki] BananaWiki 1.7.3 ready (database schema 18)\n"
        "2026-10-28 09:52:46,001 INFO [bananawiki.wiki] Failed sign-in for 'olivia' from 198.51.100.77\n"
        "2026-10-28 09:52:47,002 INFO [bananawiki.wiki] Wrong two-step code for 'olivia' from 2001:db8::7\n"
        "2026-10-28 09:52:48,003 ERROR [bananawiki.wiki] Upstream 203.0.113.5:8080 and [::ffff:192.0.2.1] failed\n",
        encoding="utf-8")
    make_user("olivia", role="owner")
    plain, owner = app.test_client(), app.test_client()
    login(plain, make_user("ada", role="admin"))
    login(owner, make_user("otto", role="owner"))
    for level in ("all", "error"):
        text = plain.get(f"/admin/error-log?level={level}").get_data(as_text=True)
        for address in ("198.51.100.77", "2001:db8::7", "203.0.113.5", "192.0.2.1"):
            assert address not in text
        assert "[address]" in text
    text = plain.get("/admin/error-log?level=all").get_data(as_text=True)
    assert "Failed sign-in for &#39;olivia&#39; from [address]" in text and "09:52:46,001" in text
    assert "1.7.3" in text
    assert "198.51.100.77" in owner.get("/admin/error-log?level=all").get_data(as_text=True)
    assert server.hide_addresses("at 10:11:12 v1.2.3 from fe80::1, ::1.") == "at 10:11:12 v1.2.3 from [address], [address]."


# QW3-4: Kanban and canvas imports had a per-minute limit only


def _small_kanban_budget(monkeypatch):
    from bananawiki.wiki.features.kanban import history

    monkeypatch.setattr(history, "STORED_MIB_PER_DAY", 1)  # 1 MB a day stands for 128 MB
    return history


def _kanban_stored(db, user):
    return db.scalar("SELECT COALESCE(SUM(length(CAST(snapshot AS BLOB))), 0) FROM kanban_board_history "
                     "WHERE edited_by = ?", (user["id"],))


def test_qw3_4_kanban_imports_are_bounded_per_day_by_their_size(client, make_user, login, db, monkeypatch):
    from bananawiki.core import ratelimit

    history = _small_kanban_budget(monkeypatch)
    _editor(client, make_user, login, db)
    doc = json.dumps({"title": "b", "columns": [{"title": "c", "tickets": [
        {"title": f"t{n}", "description": "x" * 9_900} for n in range(30)]}]}).encode()

    def refused() -> bool:
        answer = client.post("/kanban/import", content_type="multipart/form-data", follow_redirects=True,
                             data={"import_file": (io.BytesIO(doc), "b.kanban.json")})
        return "in the last 24 hours" in answer.get_data(as_text=True)

    _import_board(client, db, 30, "x" * 9_900)  # 300 KB, stored twice: 600 of the 1,024 KB
    # The second one's text fits (900 KB) and its first history entry, which does not, is still stored: a
    # board's first entry is about the board itself.
    _import_board(client, db, 30, "x" * 9_900)
    boards = db.scalar("SELECT COUNT(*) FROM kanban_boards")
    assert refused()
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == boards
    # Deleting what was imported gives nothing back; the day moving on does.
    db.execute("DELETE FROM kanban_boards")
    assert refused()
    db.execute("UPDATE rate_limit_hits SET hit_at = ? WHERE bucket = ?",
               (ratelimit.sql_in(hours=-25), history.STORED_BUCKET))
    assert not refused()


def _charged(db, bucket):
    return db.scalar("SELECT COALESCE(SUM(amount), 0) FROM rate_limit_hits WHERE bucket = ?", (bucket,))


def test_qw3_4_many_boards_filled_and_edited_through_the_api_share_the_daily_budget(app, client, make_user, login,
                                                                                   db, monkeypatch):
    from bananawiki.core.timeutil import sql_in

    history = _small_kanban_budget(monkeypatch)
    eve = _editor(client, make_user, login, db)
    boards = []
    for board in range(10):
        assert client.post("/kanban/create", data={"title": f"B{board}"}).status_code == 302
        board_id = db.scalar("SELECT MAX(id) FROM kanban_boards")
        boards.append(board_id)
        column = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY id LIMIT 1", (board_id,))
        for n in range(8):
            db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-120),))  # time passes
            answer = client.post(f"/api/kanban/columns/{column}/tickets",
                                 json={"title": f"t{n}", "description": "q" * 9_900})
            assert answer.status_code == 201, "a change is never refused for the history it stores"
    # Without the budget the boards' history held 10 x (10 + 20 + ... + 80 KB) = 3.6 MB; with it, 1 MB and,
    # past that, each board's history no larger than when the budget ran out or than the board itself.
    assert _charged(db, history.STORED_BUCKET) <= 1024 * 1024
    with app.app_context():
        boards_text = sum(history.board_bytes(board_id) for board_id in boards)
    assert _kanban_stored(db, eve) <= 1024 * 1024 + boards_text * 1.1
    assert _kanban_stored(db, eve) < 2_200_000
    # Every change still has its entry; one-field edits of a board do not grow its history any more.
    ticket = db.scalar("SELECT MAX(id) FROM kanban_tickets")
    stored = _kanban_stored(db, eve)
    for n in range(5):
        db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-120),))
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"edit{n}"}).status_code == 200
        assert db.scalar("SELECT snapshot FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC LIMIT 1",
                         (boards[-1],)).count(f'"edit{n}"') == 1
    assert _kanban_stored(db, eve) <= stored + 100


def test_qw3_4_entries_of_small_boards_are_charged_by_the_byte(client, make_user, login, db):
    from bananawiki.wiki.features.kanban import history

    eve = _editor(client, make_user, login, db)
    _board_id, ticket = _import_board(client, db, 3, "x" * 10_000)  # about 30 KB, no throttle
    for n in range(20):
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"e{n:02}"}).status_code == 200
    stored = _kanban_stored(db, eve)
    assert stored > 20 * 30_000
    # The import's text and every entry it stored, to the byte (entries under 64 KB cost nothing before).
    assert _charged(db, history.STORED_BUCKET) >= stored
    assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = ?", (history.STORED_BUCKET,)) == 1


def test_qw3_1_editing_a_large_board_all_day_is_never_refused(app, client, make_user, login, db, monkeypatch):
    from bananawiki.core.ratelimit import SqlLimiter
    from bananawiki.core.timeutil import sql_in
    from bananawiki.wiki.features.kanban import history

    # The real daily budget and board sizes; a history limit of 8 MB a board stands for the real 64 MB.
    monkeypatch.setattr(history, "HISTORY_MAX_BYTES", 8 * 1024 * 1024)
    eve = _editor(client, make_user, login, db)
    board_id, ticket = _import_board(client, db, 100, "x" * 10_000)  # about 1 MB: an entry a minute
    for minute in range(16):  # once a minute: the history fills up, then costs about nothing
        db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-61),))
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"e{minute:02}"}).status_code == 200
    assert _history_rows(db, board_id) == 8
    with app.app_context():
        board = history.board_bytes(board_id)
    assert _charged(db, history.STORED_BUCKET) <= board + 8.5 * 1024 * 1024
    # And with the day's budget used up (other boards, say), editing goes on, recorded, and the board's
    # history keeps its size.
    budget = history.STORED_MIB_PER_DAY * 1024 * 1024
    SqlLimiter(db).reserve_amount(f"user:{eve['id']}", history.STORED_BUCKET,
                                  budget - _charged(db, history.STORED_BUCKET), budget, 86400)
    stored = _kanban_stored(db, eve)
    for minute in range(5):
        db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-61),))
        assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": f"f{minute:02}"}).status_code == 200
    assert '"f04"' in db.scalar("SELECT snapshot FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC "
                                "LIMIT 1", (board_id,))
    assert _kanban_stored(db, eve) <= stored + 100


def _small_canvas_budget(monkeypatch, db):
    from bananawiki.wiki.features.canvas import service

    monkeypatch.setattr(service, "STORED_MIB_PER_DAY", 1)  # 1 MB a day stands for 128 MB
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    return service


def _canvas_data(nodes=150):  # about 300 KB
    return {"nodes": [{"id": f"n{n}", "type": "text", "content": "z" * 2_000} for n in range(nodes)], "edges": []}


def _canvas_bytes(db):
    return db.scalar("SELECT (SELECT COALESCE(SUM(length(CAST(data AS BLOB))), 0) FROM canvas__layouts) + "
                     "(SELECT COALESCE(SUM(length(CAST(data AS BLOB))), 0) FROM canvas__history)")


def test_qw3_4_canvas_imports_are_bounded_per_day_by_their_size(client, make_user, login, db, monkeypatch):
    _small_canvas_budget(monkeypatch, db)
    login(client, make_user("eve", role="editor"))
    doc = json.dumps({"title": "c", "description": "", "version": 1, "data": _canvas_data()}).encode()

    def post():
        return client.post("/canvas/import", content_type="multipart/form-data", follow_redirects=True,
                           data={"import_file": (io.BytesIO(doc), "c.canvas.json")})

    assert post().status_code == 200  # 300 KB, stored twice: 600 of the 1,024 KB
    # The second one's document fits (900 KB); its first history entry, which does not, is still stored.
    assert post().status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 2
    assert "in the last 24 hours" in post().get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 2
    # An empty canvas costs next to nothing.
    assert client.post("/canvas/create", data={"title": "Blank"}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 3


def test_qw3_4_empty_canvases_filled_by_saves_and_ops_share_the_daily_budget(client, make_user, login, db,
                                                                           monkeypatch):
    _small_canvas_budget(monkeypatch, db)
    login(client, make_user("eve", role="editor"))
    answers = []
    for n in range(6):
        slug = client.post("/canvas/create", data={"title": f"c{n}"}).headers["Location"].rstrip("/").split("/")[-1]
        answers.append(client.post(f"/canvas/{slug}/data", json={"data": _canvas_data(60)}).status_code)
        answers.append(client.post(f"/canvas/{slug}/ops", json={"ops": [
            {"op": "upsert_node", "node": node} for node in _canvas_data(60)["nodes"][:30]]}).status_code)
    assert 429 in answers, answers
    assert _canvas_bytes(db) <= 2 * 1024 * 1024 + 12 * 1024
    refused = client.post(f"/canvas/{slug}/data", json={"data": _canvas_data(60)})
    assert refused.status_code == 429 and "in the last 24 hours" in refused.json["error"]
    # What does not grow a canvas is still allowed.
    assert client.post(f"/canvas/{slug}/data", json={"data": {"nodes": [], "edges": []}}).status_code == 200


def test_qw3_4_rest_canvas_creation_with_data_shares_the_daily_budget(app, client, make_user, db, monkeypatch):
    from tests.features.api_support import call, enable_api, issue

    _small_canvas_budget(monkeypatch, db)
    enable_api(app, db)
    token = issue(app, make_user("eve", role="editor", api_access_enabled=1), ["canvas"])
    for title in ("One", "Two"):  # 300 KB and its first entry; the second's entry is past the budget
        assert call(client, "POST", "/canvas", token, json={"title": title, "data": _canvas_data()}).status_code == 201
    refused = call(client, "POST", "/canvas", token, json={"title": "Three", "data": _canvas_data()})
    assert refused.status_code == 429 and refused.json["code"] == "daily_limit", refused.json
    assert call(client, "POST", "/canvas", token, json={"title": "Empty"}).status_code == 201

def test_qw3_4_an_administrator_out_of_budget_can_still_delete_an_account(app, client, make_user, login, db):
    from bananawiki.core.ratelimit import SqlLimiter
    from bananawiki.core.timeutil import sql_in
    from bananawiki.wiki import accounts
    from bananawiki.wiki.features.kanban import history

    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    carol, bob = make_user("carol", role="editor"), make_user("bob", role="editor")
    login(client, carol)
    board_id, ticket = _import_board(client, db, 10, "y" * 9_900)  # about 100 KB
    db.execute("INSERT INTO kanban_ticket_assignees (ticket_id, user_id, assigned_at) VALUES (?, ?, ?)",
               (ticket, bob["id"], sql_in(seconds=0)))
    db.execute("UPDATE kanban_tickets SET created_by = ? WHERE id = ?", (bob["id"], ticket))
    db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-120),))
    assert client.put(f"/api/kanban/tickets/{ticket}", json={"title": "with bob"}).status_code == 200
    ada = make_user("ada", role="admin")
    used_up = history.STORED_MIB_PER_DAY * 1024 * 1024
    assert SqlLimiter(db).reserve_amount(f"user:{ada['id']}", history.STORED_BUCKET, used_up, used_up, 86400)
    used = db.scalar("SELECT SUM(amount) FROM rate_limit_hits WHERE bucket = ?", (history.STORED_BUCKET,))
    db.execute("UPDATE kanban_board_history SET created_at = ?", (sql_in(seconds=-120),))
    entries = _history_rows(db, board_id)
    with app.test_request_context():
        accounts.delete(db.one("SELECT * FROM users WHERE id = ?", (bob["id"],)), deleted_by=ada["id"])
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (bob["id"],)) == 0
    assert _history_rows(db, board_id) == entries + 1, "the hand-over is recorded"
    assert db.scalar("SELECT SUM(amount) FROM rate_limit_hits WHERE bucket = ?", (history.STORED_BUCKET,)) == used



def test_qw3_4_small_canvases_are_charged_by_the_byte(client, make_user, login, db):
    from bananawiki.wiki.features.canvas import service

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    login(client, make_user("eve", role="editor"))
    for n in range(10):  # 60 KB each, under the 64 KB that cost nothing before
        slug = client.post("/canvas/create", data={"title": f"c{n}"}).headers["Location"].rstrip("/").split("/")[-1]
        assert client.post(f"/canvas/{slug}/data", json={"data": _canvas_data(30)}).status_code == 200
    assert _canvas_bytes(db) > 1_200_000
    assert _charged(db, service.STORED_BUCKET) >= _canvas_bytes(db)
