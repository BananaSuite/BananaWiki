"""Deleting an account keeps what it added to kanban boards: tickets, comments and its own boards."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.ops import tenant_task
from bananawiki.wiki import accounts, plugins_external
from bananawiki.wiki.features.kanban import extras, service, signals, store
from bananawiki.wiki.features.users import merge
from bananawiki.wiki.markdown import render
from bananawiki.wiki.registry import Feature

from .pages_support import in_app, set_feature


def _board(app, owner, title="Roadmap"):
    return in_app(app, lambda: service.create_board(owner, title))


def _ticket(app, board, user, title="Task", **data):
    column = in_app(app, lambda: store.columns_of(board["id"]))[0]
    return in_app(app, lambda: service.create_ticket(board, column, user, {"title": title, **data}))


def _comment(app, ticket_id, user, text="Looks good"):
    return in_app(app, lambda: extras.add_comment(store.get_ticket(ticket_id), user, text))


@pytest.fixture
def people(make_user, db):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'all' WHERE id = 1")
    return {"admin": make_user("boss", role="admin"), "bob": make_user("bob"), "carol": make_user("carol")}


def test_admin_deleting_an_account_keeps_its_work_on_other_boards(app, client, login, db, people, monkeypatch):
    admin, bob, carol = people["admin"], people["bob"], people["carol"]
    board = _board(app, carol)
    own = _ticket(app, board, carol, "Carol's ticket", assignees=[bob["id"], admin["id"]])
    bobs = _ticket(app, board, bob, "Bob's ticket")
    _comment(app, own["id"], bob)
    _comment(app, own["id"], carol, "Thanks")
    seq = db.scalar("SELECT MAX(seq) FROM kanban_events WHERE board_id = ?", (board["id"],))

    login(client, admin)
    emitted = []
    monkeypatch.setattr(signals, "emit", lambda event, **payload: emitted.append((event, payload)))
    client.post(f"/admin/users/{bob['id']}/edit", data={"action": "delete"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None

    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (bobs["id"],)) == carol["id"]
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (own["id"],)) == carol["id"]
    comments = db.all("SELECT user_id, content FROM kanban_ticket_comments WHERE ticket_id = ? ORDER BY id",
                      (own["id"],))
    assert [(row["user_id"], row["content"]) for row in comments] == [
        (carol["id"], "*Written by bob, whose account was later deleted.*\n\nLooks good"), (carol["id"], "Thanks")]
    assert db.column("SELECT user_id FROM kanban_ticket_assignees WHERE ticket_id = ?", (own["id"],)) == [admin["id"]]
    assert db.scalar("SELECT assigned_to FROM kanban_tickets WHERE id = ?", (own["id"],)) == admin["id"]
    entry = db.one("SELECT user_id, details FROM kanban_activity_log WHERE board_id = ? AND action = 'account_deleted'",
                   (board["id"],))
    assert entry["user_id"] == admin["id"] and "bob" in entry["details"] and "carol" in entry["details"]
    assert db.scalar("SELECT op_type FROM kanban_events WHERE board_id = ? AND seq > ?", (board["id"], seq)) \
        == "board_reset"
    latest = db.one("SELECT edited_by, edit_message FROM kanban_board_history WHERE board_id = ? ORDER BY id DESC "
                    "LIMIT 1", (board["id"],))
    assert latest["edited_by"] == admin["id"] and "bob" in latest["edit_message"]
    assert [(payload["board"]["id"], payload["actor_id"]) for event, payload in emitted
            if event == "kanban.board.updated"] == [(board["id"], admin["id"])]


def test_self_deleted_owner_hands_the_board_to_an_administrator(app, client, login, db, people, make_user):
    admin, bob, carol = people["admin"], people["bob"], people["carol"]
    make_user("later_admin", role="admin")
    board = _board(app, bob, "Bob's board")
    in_app(app, lambda: service.add_share(board, "user", carol["id"], "write"))
    mine = _ticket(app, board, bob, "Bob's own")
    theirs = _ticket(app, board, carol, "Carol's work")
    _comment(app, theirs["id"], carol)
    _comment(app, theirs["id"], bob, "Reply")
    db.execute("INSERT INTO kanban_user_board_order (user_id, board_id, sort_order) VALUES (?, ?, 0)",
               (bob["id"], board["id"]))

    login(client, bob)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None

    kept = db.one("SELECT * FROM kanban_boards WHERE id = ?", (board["id"],))
    assert kept is not None and kept["created_by"] == admin["id"]  # the longest-standing administrator
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (mine["id"],)) == admin["id"]
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (theirs["id"],)) == carol["id"]
    assert db.column("SELECT user_id FROM kanban_ticket_comments WHERE ticket_id = ? ORDER BY id",
                     (theirs["id"],)) == [carol["id"], admin["id"]]
    assert db.scalar("SELECT COUNT(*) FROM kanban_board_shares WHERE board_id = ?", (board["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM kanban_user_board_order WHERE user_id = ?", (bob["id"],)) == 0
    entry = db.one("SELECT user_id, details FROM kanban_activity_log WHERE board_id = ? AND action = 'account_deleted'",
                   (board["id"],))
    assert entry["user_id"] is None and "boss" in entry["details"]


def test_work_is_kept_while_kanban_is_switched_off(app, db, people):
    admin, bob = people["admin"], people["bob"]
    board = _board(app, admin)
    ticket = _ticket(app, board, bob)
    set_feature(app, "kanban", False)
    in_app(app, lambda: accounts.delete(accounts.by_id(bob["id"]), deleted_by=admin["id"]))
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == admin["id"]


def test_merge_with_deletion_keeps_the_source_work(app, db, people, make_user):
    admin, bob = people["admin"], people["bob"]
    target = make_user("bob_new")
    board = _board(app, bob)
    ticket = _ticket(app, board, bob)
    in_app(app, lambda: merge.execute(bob, target, admin, delete_source=True))
    assert db.scalar("SELECT created_by FROM kanban_boards WHERE id = ?", (board["id"],)) == target["id"]
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == target["id"]


def test_without_another_administrator_only_the_own_boards_go(app, db, make_user):
    last, carol = make_user("last_admin", role="admin"), make_user("carol")
    own = _board(app, last, "Last's board")
    _ticket(app, own, carol, "Carol's ticket there")
    theirs = _board(app, carol, "Carol's board")
    ticket = _ticket(app, theirs, last, "Last's ticket")
    _comment(app, ticket["id"], last)
    in_app(app, lambda: accounts.delete(accounts.by_id(last["id"])))
    # A private board never passes to someone who could not open it: no administrator, no hand-over.
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards WHERE id = ?", (own["id"],)) == 0
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == carol["id"]
    comments = db.column("SELECT user_id FROM kanban_ticket_comments WHERE ticket_id = ?", (ticket["id"],))
    assert comments == [carol["id"]]


def test_an_account_only_assigned_on_a_board_is_logged_as_such(app, db, people):
    admin, bob, carol = people["admin"], people["bob"], people["carol"]
    board = _board(app, carol)
    _ticket(app, board, carol, assignees=[bob["id"]])
    in_app(app, lambda: accounts.delete(accounts.by_id(bob["id"]), deleted_by=admin["id"]))
    details = db.scalar("SELECT details FROM kanban_activity_log WHERE board_id = ? AND action = 'account_deleted'",
                        (board["id"],))
    assert "bob" in details and "assigned" in details and "carol" not in details


def test_removing_a_wiki_user_from_the_hosting_portal_keeps_its_work(app, db, people):
    admin, bob, carol = people["admin"], people["bob"], people["carol"]
    theirs = _board(app, carol)
    ticket = _ticket(app, theirs, bob, "Bob's ticket")
    _comment(app, ticket["id"], bob)
    own = _board(app, bob, "Bob's board")
    kept = _ticket(app, own, carol, "Carol's work")
    assert tenant_task.remove_user(app.config["BW"], {"username": "bob"}) == {}
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == carol["id"]
    comment = db.one("SELECT user_id, content FROM kanban_ticket_comments WHERE ticket_id = ?", (ticket["id"],))
    assert comment["user_id"] == carol["id"] and comment["content"].startswith("*Written by bob,")
    assert db.scalar("SELECT created_by FROM kanban_boards WHERE id = ?", (own["id"],)) == admin["id"]
    assert db.scalar("SELECT created_by FROM kanban_tickets WHERE id = ?", (kept["id"],)) == carol["id"]


def test_a_handed_over_comment_names_its_deleted_author(app, db, people, make_user):
    admin, carol = people["admin"], people["carol"]
    dan = make_user("_dan_")
    ticket = _ticket(app, _board(app, carol), carol)
    _comment(app, ticket["id"], dan, "My **own** words")
    db.execute("UPDATE site_settings SET interface_language = 'it' WHERE id = 1")  # the site's, not the deleter's
    in_app(app, lambda: accounts.delete(accounts.by_id(dan["id"]), deleted_by=admin["id"]))
    row = in_app(app, lambda: extras.comments(ticket["id"]))[0]
    assert row["author_name"] == "carol"
    assert row["content"] == "*Scritto da \\_dan\\_, il cui account è stato poi eliminato.*\n\nMy **own** words"
    html = in_app(app, lambda: render(row["content"]))
    assert "<em>Scritto da _dan_, il cui account è stato poi eliminato.</em>" in html
    assert "<strong>own</strong>" in html


def test_only_an_enabled_plugin_can_stop_an_account_deletion(app, db, people, monkeypatch, caplog):
    admin, bob, carol = people["admin"], people["bob"], people["carol"]
    reg = app.extensions["bananawiki.registry"]

    def broken(**_):
        raise RuntimeError("broken plugin")

    plugin = Feature(id="extplug", name="extplug", default_enabled=False)
    monkeypatch.setitem(reg.features, "extplug", plugin)
    monkeypatch.setitem(reg._interceptors, "user.delete", [*reg._interceptors["user.delete"], ("extplug", broken)])
    monkeypatch.setattr(plugins_external.Runtime, "loaded_ids", lambda self: ["extplug"])
    in_app(app, lambda: accounts.delete(accounts.by_id(bob["id"]), deleted_by=admin["id"]))
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (bob["id"],)) is None
    plugin.default_enabled = True
    with pytest.raises(RuntimeError):
        in_app(app, lambda: accounts.delete(accounts.by_id(carol["id"]), deleted_by=admin["id"]))
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (carol["id"],)) == 1
    assert "user.delete in feature extplug failed" in caplog.text
