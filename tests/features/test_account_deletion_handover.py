import pytest

from bananawiki.wiki import accounts
from tests.features.pages_support import in_app


def _board(db, owner, title, visibility):
    board_id = db.execute("INSERT INTO kanban_boards (title, created_by, visibility) VALUES (?, ?, ?)",
                          (title, owner["id"], visibility)).lastrowid
    column_id = db.execute("INSERT INTO kanban_columns (board_id, title) VALUES (?, 'To do')", (board_id,)).lastrowid
    return board_id, column_id


def _ticket(db, owner, column_id, title):
    return db.execute("INSERT INTO kanban_tickets (column_id, title, created_by) VALUES (?, ?, ?)",
                      (column_id, title, owner["id"])).lastrowid


def _canvas(db, owner, slug, visibility):
    return db.execute("INSERT INTO canvas__layouts (slug, title, creator_id, visibility) VALUES (?, ?, ?, ?)",
                      (slug, slug, owner["id"], visibility)).lastrowid


@pytest.fixture
def leaver(app, db, make_user):
    admin = make_user("keeper", role="admin")
    member = make_user("leaver")
    shared, shared_column = _board(db, member, "Shared plan", "shared")
    private, private_column = _board(db, member, "Private notes", "private")
    _, admin_column = _board(db, admin, "Admin board", "public")
    return {
        "admin": admin, "member": member,
        "shared_board": shared, "private_board": private,
        "own_ticket": _ticket(db, member, shared_column, "On a shared board"),
        "ticket_elsewhere": _ticket(db, member, admin_column, "On the admin's board"),
        "shared_canvas": _canvas(db, member, "team-map", "public"),
        "private_canvas": _canvas(db, member, "scratch", "private"),
    }


def _exists(db, table, row_id):
    return db.scalar(f"SELECT COUNT(*) FROM {table} WHERE id = ?", (row_id,)) == 1


def test_deleting_an_account_keeps_boards_and_shared_canvases_and_removes_private_canvases(app, db, leaver):
    in_app(app, lambda: accounts.delete(accounts.by_id(leaver["member"]["id"]), deleted_by=leaver["admin"]["id"]))

    assert _exists(db, "kanban_boards", leaver["shared_board"])
    assert db.scalar("SELECT created_by FROM kanban_boards WHERE id = ?", (leaver["shared_board"],)) == \
        leaver["admin"]["id"]
    assert _exists(db, "kanban_tickets", leaver["own_ticket"])
    assert _exists(db, "kanban_tickets", leaver["ticket_elsewhere"])
    assert _exists(db, "canvas__layouts", leaver["shared_canvas"])
    assert db.scalar("SELECT creator_id FROM canvas__layouts WHERE id = ?", (leaver["shared_canvas"],)) == \
        leaver["admin"]["id"]
    # Kanban keeps every board an account created (kanban.service.release); private canvases go with it.
    assert db.scalar("SELECT created_by FROM kanban_boards WHERE id = ?", (leaver["private_board"],)) == \
        leaver["admin"]["id"]
    assert not _exists(db, "canvas__layouts", leaver["private_canvas"])


def test_a_member_leaving_on_their_own_hands_shared_work_to_an_administrator(app, db, leaver):
    in_app(app, lambda: accounts.delete(accounts.by_id(leaver["member"]["id"]),
                                        deleted_by=leaver["member"]["id"]))

    assert db.scalar("SELECT created_by FROM kanban_boards WHERE id = ?", (leaver["shared_board"],)) == \
        leaver["admin"]["id"]
    assert _exists(db, "canvas__layouts", leaver["shared_canvas"])
