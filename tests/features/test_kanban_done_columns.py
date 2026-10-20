"""Done columns: a column setting (off by default) whose tickets do not count as overdue or due soon."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki.features.kanban import fields, history
from bananawiki.wiki.features.kanban import service as kanban_service
from bananawiki.wiki.features.kanban import store as kanban_store

from .api_support import call, enable_api, issue
from .pages_support import in_app
from .test_kanban import as_client, board_id_from, columns, new_ticket, set_settings


@pytest.fixture
def board(admin_client):
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))


def _mark(client, column_id, done=True):
    return client.put(f"/api/kanban/columns/{column_id}", json={"done": done})


def test_columns_start_not_done_and_the_flag_is_saved_validated_and_synced(admin_client, board, db):
    cols = columns(admin_client, board)
    assert [column["done"] for column in cols] == [False, False, False]
    done_id = cols[2]["id"]
    start = admin_client.get(f"/api/kanban/{board}/state").get_json()["seq"]
    response = _mark(admin_client, done_id)
    assert response.status_code == 200 and response.get_json()["done"] is True
    assert db.scalar("SELECT is_done FROM kanban_columns WHERE id = ?", (done_id,)) == 1
    assert [column["done"] for column in columns(admin_client, board)] == [False, False, True]
    event = admin_client.get(f"/api/kanban/{board}/sync?since={start}").get_json()["events"][-1]
    assert event["op"] == "column_upsert" and event["payload"]["column"]["done"] is True
    activity = db.scalar("SELECT details FROM kanban_activity_log WHERE board_id = ? ORDER BY id DESC LIMIT 1",
                         (board,))
    assert json.loads(activity)["k"] == "kanban.log.column_done"

    for bad in ("yes", 1, None, "true"):
        assert _mark(admin_client, done_id, bad).status_code == 400, bad
    # Renaming or limiting keeps the flag; false clears it.
    admin_client.put(f"/api/kanban/columns/{done_id}", json={"title": "Shipped", "wip_limit": 4})
    assert columns(admin_client, board)[2]["done"] is True
    assert _mark(admin_client, done_id, False).get_json()["done"] is False
    assert db.scalar("SELECT is_done FROM kanban_columns WHERE id = ?", (done_id,)) == 0


def test_only_writers_mark_a_column_done(app, admin_client, db, board, make_user, login):
    set_settings(db, kanban_access="all")
    reader = as_client(app, login, make_user("reader"))
    column_id = columns(admin_client, board)[2]["id"]
    assert _mark(reader, column_id).status_code == 403
    assert db.scalar("SELECT is_done FROM kanban_columns WHERE id = ?", (column_id,)) == 0


def test_field_parsing():
    assert fields.done_flag(True) == 1 and fields.done_flag(False) == 0
    for bad in (1, 0, "true", None, [], {}):
        with pytest.raises(fields.KanbanError):
            fields.done_flag(bad)


def test_tickets_in_a_done_column_are_never_overdue_or_due_soon(admin_client, board, admin):
    cols = columns(admin_client, board)
    late = new_ticket(admin_client, cols[0]["id"], f"Late @{admin['username']} due:2000-01-01")
    shipped = new_ticket(admin_client, cols[2]["id"], f"Shipped @{admin['username']} due:2000-01-02")
    _mark(admin_client, cols[2]["id"])

    def shown(query):
        state = admin_client.get(f"/api/kanban/{board}/state?{query}").get_json()
        return sorted(int(key) for key in state["tickets"])

    assert shown("due=overdue") == [late["id"]]
    assert shown("due=soon") == [late["id"]]
    assert shown("") == sorted([late["id"], shipped["id"]])
    mine = admin_client.get("/api/kanban/my-tickets?due=overdue").get_json()["tickets"]
    assert [ticket["id"] for ticket in mine] == [late["id"]]

    # "My tickets" puts it under its own heading, with a plain due date instead of the overdue badge.
    page = admin_client.get("/kanban/mine").get_data(as_text=True)
    done_section = page.split('id="kanban-mine-done"', 1)[1]
    overdue_section = page.split('id="kanban-mine-overdue"', 1)[1].split("</section>", 1)[0]
    assert "Late" in overdue_section and "Shipped" not in overdue_section
    assert "Shipped" in done_section and "kanban-due--overdue" not in done_section
    assert "Due 2000-01-02" in done_section



def test_the_board_script_skips_the_due_state_in_done_columns():
    static = Path(kanban_store.__file__).with_name("static")
    text = (static / "kanban-board.js").read_text(encoding="utf-8")
    assert re.search(r"K\.dueStateIn = function \(ticket\)", text)
    assert "var due = K.dueStateIn(ticket);" in text
    assert '"done-column"' in text and "aria-pressed" in text
    assert "K.dueStateIn(ticket)" in (static / "kanban-filter.js").read_text(encoding="utf-8")


def test_history_revert_export_and_import_keep_the_flag(admin_client, board, db):
    column_id = columns(admin_client, board)[2]["id"]
    before = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    _mark(admin_client, column_id)
    entry_id = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board,))
    assert entry_id != before
    state = history.decode(db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?", (entry_id,)))
    assert [column.get("done", False) for column in state["columns"]] == [False, False, True]
    # Columns that are not done keep the snapshot as it was, so unchanged boards record nothing new.
    assert "done" not in state["columns"][0]
    page = admin_client.get(f"/kanban/{board}/history/{entry_id}").get_data(as_text=True)
    assert "marked the column" in page or "Done column" in page

    admin_client.post(f"/kanban/{board}/revert/{before}")
    assert columns(admin_client, board)[2]["done"] is False
    admin_client.post(f"/kanban/{board}/revert/{entry_id}")
    assert columns(admin_client, board)[2]["done"] is True

    document = json.loads(admin_client.get(f"/kanban/{board}/export?format=json").data)
    assert [column.get("done", False) for column in document["columns"]] == [False, False, True]
    document["columns"][0]["done"] = "junk"
    imported = board_id_from(admin_client.post(
        "/kanban/import", data={"import_file": (io.BytesIO(json.dumps(document).encode()), "b.kanban.json")},
        content_type="multipart/form-data"))
    assert [column["done"] for column in columns(admin_client, imported)] == [False, False, True]


def test_the_rest_api_reads_and_sets_the_flag(app, client, db, make_user):
    enable_api(app, db)
    admin = make_user("boss", role="admin")
    board = in_app(app, lambda: kanban_service.create_board(admin, "Roadmap", "Plans"))
    column = in_app(app, lambda: kanban_store.columns_of(board["id"]))[2]
    token = issue(app, admin, ["kanban"])
    path = f"/kanban/columns/{column['id']}"
    answer = call(client, "PUT", path, token, json={"done": True})
    assert answer.status_code == 200 and answer.json["column"]["done"] is True
    listed = call(client, "GET", f"/kanban/boards/{board['id']}", token).json["board"]["columns"]
    assert [item["done"] for item in listed] == [False, False, True]
    for value in ("true", 1, None):
        assert call(client, "PUT", path, token, json={"done": value}).status_code == 400, value


def test_the_done_toggle_is_an_icon_so_the_column_tools_keep_one_row():
    # At the default column width (250 px) a third text button would push Delete onto a second row,
    # so the done toggle is an icon button named by its aria-label.
    static = Path(kanban_store.__file__).with_name("static")
    script = (static / "kanban-board.js").read_text(encoding="utf-8")
    tools = script.split('var tools = el("div", "kanban-column__tools");', 1)[1].split("head.appendChild(tools);", 1)[0]
    buttons = re.findall(r'el\("button", "([^"]+)", ([^)]*\)?)\)', tools)
    icons = [content for classes, content in buttons if "btn--icon" in classes.split()]
    texts = [content for classes, content in buttons if "btn--icon" not in classes.split()]
    assert len(icons) == 2 and texts == ['BW.t("kanban.wip_button")', 'BW.t("kanban.archive_column")']
    done = next(classes for classes, _ in buttons if "kanban-column__done" in classes)
    assert "btn--icon" in done.split()
    assert 'finished.appendChild(icon("done"));' in tools
    fill = script.split("function fillColumn", 1)[1].split("\n  }\n", 1)[0]
    assert 'doneButton.setAttribute("aria-label", BW.t("kanban.done_label", { title: col.title }));' in fill
    assert 'doneButton.title = BW.t("kanban.done_set", { title: col.title });' in fill
    css = (static / "kanban.css").read_text(encoding="utf-8")
    # No text drawn into the toggle (the readers' mark keeps its check and words).
    assert not re.search(r"\.kanban-column__done\[aria-pressed=\"true\"\]::before", css)
    assert ".kanban-column__done-mark::before" in css
    for lang in ("en", "it"):
        catalogue = json.loads((static.parent / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "{title}" in catalogue["js.kanban.done_label"]
        assert max(len(catalogue["js.kanban.wip_button"]), len(catalogue["js.kanban.archive_column"])) <= 8
