"""Regression tests for the PW3 findings of the full re-audit pass 10 (account merge requests, group chats,
Kanban snapshot size and actions taken while impersonating)."""

from __future__ import annotations

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.users import merge
from tests.conftest import PASSWORD

# PW3-1: account merge requests are bounded per day, and the closed ones listed are bounded


def test_pw3_1_merge_requests_are_bounded_per_day(app, client, make_user, login, db):
    alice = make_user("alice")
    victim = make_user("victim")
    login(client, alice)
    form = {"other_username": "victim", "direction": "into_other", "reason": "x" * 600, "password": PASSWORD}
    for _ in range(merge.MAX_REQUESTS_PER_DAY):
        assert client.post("/settings/merge-request", data=form).status_code == 302
        merge_id = db.scalar("SELECT MAX(id) FROM account_merge_requests")
        assert client.post(f"/settings/merge/cancel/{merge_id}").status_code == 302
    refused = client.post("/settings/merge-request", data=form)
    assert refused.status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests WHERE target_user_id = ?",
                     (victim["id"],)) == merge.MAX_REQUESTS_PER_DAY
    # Requests older than a day no longer count.
    db.execute("UPDATE account_merge_requests SET created_at = '2000-01-01 00:00:00'")
    assert client.post("/settings/merge-request", data=form).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests WHERE status = 'pending'") == 1


def test_pw3_1_merge_page_lists_a_bounded_history(app, client, make_user, login, db):
    alice = make_user("alice")
    victim = make_user("victim")
    for n in range(merge.HISTORY_LIMIT + 20):
        db.execute("INSERT INTO account_merge_requests (source_user_id, target_user_id, created_by, status, "
                   "source_approved, target_approved, request_reason, created_at) VALUES (?, ?, ?, 'cancelled', 1, 0, "
                   "'', ?)", (alice["id"], victim["id"], alice["id"], f"2001-01-01 00:{n // 60:02d}:{n % 60:02d}"))
    db.execute("INSERT INTO account_merge_requests (source_user_id, target_user_id, created_by, status, "
               "source_approved, target_approved, request_reason, created_at) VALUES (?, ?, ?, 'pending', 1, 0, "
               "'still-open-reason', '2000-01-01 00:00:00')", (alice["id"], victim["id"], alice["id"]))
    login(client, victim)
    page = client.get("/settings/merge/pending").get_data(as_text=True)
    assert "still-open-reason" in page
    with app.test_request_context(), connection_scope():
        active, finished = merge.for_user(victim["id"])
    assert len(active) == 1 and len(finished) == merge.HISTORY_LIMIT
    assert finished[0]["created_at"].startswith("2001-01-01 00:01:09")


# PW3-2: group creation is bounded per day, and /groups is paged

JSON = {"Accept": "application/json"}


def test_pw3_2_group_creation_is_bounded_per_day(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.chat import groups

    monkeypatch.setattr(groups, "MAX_CREATES_PER_DAY", 3)
    mallory = make_user("mallory")
    make_user("victim")
    login(client, mallory)
    for n in range(3):
        response = client.post("/groups/new", data={"name": f"g{n}", "description": "d" * 500}, headers=JSON)
        assert response.status_code == 200, response.data[:200]
    refused = client.post("/groups/new", data={"name": "g3"}, headers=JSON)
    assert refused.status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM group_chats WHERE creator_id = ?", (mallory["id"],)) == 3
    # Groups created more than a day ago no longer count.
    db.execute("UPDATE group_chats SET created_at = '2000-01-01 00:00:00'")
    db.execute("UPDATE rate_limit_hits SET hit_at = '2000-01-01 00:00:00' WHERE bucket = ?", (groups.CREATE_BUCKET,))
    assert client.post("/groups/new", data={"name": "g4"}, headers=JSON).status_code == 200


def test_pw3_2_transferring_groups_away_does_not_free_the_creation_budget(app, client, make_user, login, db,
                                                                        monkeypatch):
    from bananawiki.wiki.features.chat import groups

    monkeypatch.setattr(groups, "MAX_CREATES_PER_DAY", 3)
    mallory = make_user("mallory")
    victim = make_user("victim")
    login(client, mallory)
    for n in range(3):
        assert client.post("/groups/new", data={"name": f"g{n}", "description": "d" * 500},
                           headers=JSON).status_code == 200
        group_id = db.scalar("SELECT MAX(id) FROM group_chats")
        assert client.post(f"/groups/{group_id}/members/add", data={"username": "victim"},
                           headers=JSON).status_code == 200
        assert client.post(f"/groups/{group_id}/transfer", data={"user_id": victim["id"]},
                           headers=JSON).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM group_chats WHERE creator_id = ?", (mallory["id"],)) == 0
    assert client.post("/groups/new", data={"name": "g3"}, headers=JSON).status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM group_members WHERE user_id = ?", (victim["id"],)) == 3


def test_pw3_2_group_list_is_paged(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.chat import groups

    monkeypatch.setattr(groups, "LIST_PAGE_SIZE", 5)
    mallory = make_user("mallory")
    victim = make_user("victim")
    for n in range(12):
        group_id = db.execute("INSERT INTO group_chats (name, creator_id, invite_code, created_at) VALUES (?, ?, ?, ?)",
                              (f"spam-group-{n:02d}", mallory["id"], f"code{n:08d}",
                               f"2001-01-01 00:00:{n:02d}")).lastrowid
        for user_id, role in ((mallory["id"], "owner"), (victim["id"], "member")):
            db.execute("INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)", (group_id, user_id, role))
    login(client, victim)
    first = client.get("/groups").get_data(as_text=True)
    assert first.count("spam-group-") == 5 and "spam-group-11" in first and "spam-group-06" not in first
    last = client.get("/groups?page=3").get_data(as_text=True)
    assert last.count("spam-group-") == 2 and "spam-group-00" in last
    assert client.get("/groups?page=999").get_data(as_text=True).count("spam-group-") == 2


# PW3-3: a board's stored snapshot stays about the size the board cap measures


def _board(app, db, owner):
    from bananawiki.wiki.features.kanban import service

    with app.test_request_context(), connection_scope():
        board = service.create_board(owner, "big")
    column = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order LIMIT 1", (board["id"],))
    return board["id"], column


def test_pw3_3_ticket_text_drops_control_characters(app, client, make_user, login, db):
    boss = make_user("boss", role="admin")
    login(client, boss)
    board_id, column = _board(app, db, boss)
    response = client.post(f"/api/kanban/columns/{column}/tickets",
                           json={"title": "a\x02b", "description": "\x01" * 5000 + "one\ttwo\nthree"})
    assert response.status_code == 201, response.data[:200]
    row = db.one("SELECT id, title, description FROM kanban_tickets ORDER BY id DESC LIMIT 1")
    assert (row["title"], row["description"]) == ("ab", "one\ttwo\nthree")
    response = client.put(f"/api/kanban/tickets/{row['id']}", json={"description": "x\x1f\x00y"})
    assert response.status_code == 200, response.data[:200]
    assert db.scalar("SELECT description FROM kanban_tickets WHERE id = ?", (row["id"],)) == "xy"


def test_pw3_3_snapshot_is_stored_as_utf8(app, client, make_user, db):
    from bananawiki.wiki.db import db as wiki_db
    from bananawiki.wiki.features.kanban import history, service, store

    boss = make_user("boss", role="admin")
    board_id, column = _board(app, db, boss)
    with app.test_request_context(), connection_scope():
        board, target = store.get_board(board_id), store.get_column(column)
        for n, letter in enumerate(("é", "\U0001f600", "中")):
            service.create_ticket(board, target, boss, {"title": f"t{n}", "description": letter * 3000})
        with wiki_db.transaction():
            history.record(board_id, boss["id"], "x", force=True)
        measured = history.board_bytes(board_id)
    stored = db.scalar("SELECT length(CAST(snapshot AS BLOB)) FROM kanban_board_history WHERE board_id = ? "
                       "ORDER BY id DESC LIMIT 1", (board_id,))
    # Escaped as ASCII the three descriptions alone took 108,000 bytes against 27,000 measured.
    assert stored < measured + 2000


def test_pw3_3_title_and_label_growth_counts_against_the_cap(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.kanban import fields, history

    boss = make_user("boss", role="admin")
    login(client, boss)
    board_id, column = _board(app, db, boss)
    assert client.post(f"/api/kanban/columns/{column}/tickets", json={"title": "t"}).status_code == 201
    ticket_id = db.scalar("SELECT MAX(id) FROM kanban_tickets")
    with app.test_request_context(), connection_scope():
        monkeypatch.setattr(fields, "MAX_BOARD_BYTES", history.board_bytes(board_id) + 50)
    response = client.put(f"/api/kanban/tickets/{ticket_id}", json={"title": "t" * 200})
    assert response.status_code == 409
    response = client.put(f"/api/kanban/tickets/{ticket_id}", json={"labels": [f"label{n}" for n in range(20)]})
    assert response.status_code == 409
    assert db.one("SELECT title, labels FROM kanban_tickets WHERE id = ?", (ticket_id,)) == {"title": "t", "labels": ""}
    # Shortening stays allowed on a full board.
    assert client.put(f"/api/kanban/tickets/{ticket_id}", json={"title": "u"}).status_code == 200


def test_pw3_3_wider_characters_at_the_same_length_count_against_the_cap(app, client, make_user, login, db,
                                                                         monkeypatch):
    from bananawiki.wiki.features.kanban import fields, history

    boss = make_user("boss", role="admin")
    bo = make_user("bob")
    longname = make_user("a_much_longer_username_than_this")
    login(client, boss)
    board_id, column = _board(app, db, boss)
    for person in (bo, longname):
        client.post(f"/kanban/{board_id}/share", data={"action": "add_user", "user_id": person["id"]})
    created = client.post(f"/api/kanban/columns/{column}/tickets",
                          json={"title": "t" * 30, "description": "a" * 300, "labels": ["abc"],
                                "assignees": [bo["id"]]})
    assert created.status_code == 201
    ticket_id = created.get_json()["id"]
    item = client.post(f"/api/kanban/tickets/{ticket_id}/checklist", json={"text": "c" * 100})
    assert item.status_code == 201
    item_id = db.scalar("SELECT MAX(id) FROM kanban_ticket_checklist")
    with app.test_request_context(), connection_scope():
        monkeypatch.setattr(fields, "MAX_BOARD_BYTES", history.board_bytes(board_id) + 20)
    wide = "\U0001f600"
    # Labels are ASCII only, so only their length can grow (the test above).
    for change in ({"title": wide * 30}, {"description": wide * 300}, {"assignees": [longname["id"]]}):
        assert client.put(f"/api/kanban/tickets/{ticket_id}", json=change).status_code == 409, change
    assert client.put(f"/api/kanban/checklist/{item_id}", json={"text": wide * 100}).status_code == 409
    assert db.one("SELECT title, description, labels FROM kanban_tickets WHERE id = ?", (ticket_id,)) == {
        "title": "t" * 30, "description": "a" * 300, "labels": '["abc"]'}
    assert db.scalar("SELECT text FROM kanban_ticket_checklist WHERE id = ?", (item_id,)) == "c" * 100
    # The same characters again, or narrower ones, are still accepted on a full board.
    assert client.put(f"/api/kanban/tickets/{ticket_id}", json={"description": "b" * 300}).status_code == 200
    assert client.put(f"/api/kanban/checklist/{item_id}", json={"text": "d" * 100}).status_code == 200


# PW3-4: proposals, quota requests and federation shares made while impersonating are audited


def _impersonated_entries(db, since: int, admin_id: str) -> list[dict]:
    return db.all("SELECT details FROM audit_log WHERE id > ? AND actor_id = ? AND action = 'page.impersonated_action'",
                  (since, admin_id))


def test_pw3_4_impersonated_proposals_quota_requests_and_shares_are_audited(app, make_user, login, db, monkeypatch):
    import time

    from bananawiki.wiki.features.federation import sync
    from tests.features.pages_support import make_category, make_page, set_feature

    set_feature(app, "audit", True)
    set_feature(app, "contributions", True)
    monkeypatch.setattr(sync, "active", lambda: True)
    page = make_page(app, "Payroll", "salaries")
    inbox = make_category(app, "Inbox")
    db.insert("federation_peers", {"wiki_id": "peer-1", "name": "Partner", "base_url": "http://127.0.0.1:9",
                                   "secret": "s", "audience_category": inbox["id"], "generation": "g",
                                   "last_success": time.time()})
    bob = make_user("bob")
    eve = make_user("eve", role="editor")
    ada = make_user("ada", role="admin")
    browser = app.test_client()
    login(browser, ada)
    assert browser.post(f"/admin/users/{bob['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    assert browser.post(f"/page/{page['slug']}/propose-edit",
                        data={"title": "Payroll", "content": "salaries: approved by bob", "reason": "ok"}).status_code == 302
    assert db.scalar("SELECT user_id FROM pending_contributions") == bob["id"]
    assert browser.post("/my-contributions/quota-request",
                        data={"requested_quota": "5", "reason": "please"}).status_code == 302
    assert browser.post("/my-contributions/quota-request/cancel").status_code == 302
    endpoints = [entry["details"] for entry in _impersonated_entries(db, mark, ada["id"])]
    for endpoint in ("contributions.propose", "contributions.quota_request", "contributions.cancel_quota_request"):
        assert any(f'"endpoint": "{endpoint}"' in details and '"as_user": "bob"' in details for details in endpoints), \
            endpoint

    other = app.test_client()
    login(other, ada)
    assert other.post(f"/admin/users/{eve['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    assert other.post("/federation/sharing", data={"peer_id": "peer-1", "slug": page["slug"]}).status_code == 302
    assert db.scalar("SELECT granted_by FROM federation_shares") == eve["id"]
    entries = _impersonated_entries(db, mark, ada["id"])
    assert len(entries) == 1 and '"endpoint": "federation.sharing"' in entries[0]["details"] \
        and '"as_user": "eve"' in entries[0]["details"]
