"""Personal data export (user_data_export feature)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from .pages_support import restrict
from .people_support import image_bytes, in_app, make_category, make_page, set_feature


def _archive(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.data[:300]
    assert response.mimetype == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.get_data()))


def test_own_export_contains_account_data_without_secrets(app, client, make_user, login, db):
    bob = make_user("bob", role="editor")
    make_page(app, "Doc", "my words", author_id=bob["id"])
    login(client, bob)
    client.post("/settings/profile", data={"real_name": "Bob", "avatar": (io.BytesIO(image_bytes()), "a.png")},
                content_type="multipart/form-data")
    response = client.get("/settings/export")
    assert response.is_streamed
    archive = _archive(response)
    names = archive.namelist()
    account = json.loads(archive.read("account.json"))
    assert account["username"] == "bob" and "password" not in account
    history = json.loads(archive.read("data/page_history.json"))
    assert [row["content"] for row in history] == ["my words"]
    sessions = json.loads(archive.read("data/user_sessions.json"))
    assert sessions and "token_hash" not in sessions[0]
    assert json.loads(archive.read("profile/profile.json"))["real_name"] == "Bob"
    assert any(name.startswith("files/avatars/") for name in names)
    assert b"correct horse" not in b"".join(archive.read(n) for n in names)
    assert json.loads(archive.read("manifest.json"))["format"] == "bananawiki-user-data-export"


def test_export_leaves_out_other_accounts(app, client, make_user, login):
    bob, eve = make_user("bob", role="editor"), make_user("eve", role="editor")
    make_page(app, "Eve's page", "secret of eve", author_id=eve["id"])
    login(client, bob)
    archive = _archive(client.get("/settings/export"))
    assert b"secret of eve" not in b"".join(archive.read(n) for n in archive.namelist())


def test_legacy_export_url(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/account/export").headers["Location"].endswith("/settings/export")


def test_admin_exports_a_member(admin_client, make_user):
    bob = make_user("bob")
    archive = _archive(admin_client.get(f"/admin/users/{bob['id']}/export"))
    assert json.loads(archive.read("account.json"))["id"] == bob["id"]
    assert admin_client.get("/admin/users/nobody/export").status_code == 404


def test_admins_cannot_export_owners_or_protected_accounts(admin_client, make_user):
    owner = make_user("the_owner", role="owner")
    keeper = make_user("keeper", is_superuser=1)
    assert admin_client.get(f"/admin/users/{owner['id']}/export").status_code == 403
    assert admin_client.get(f"/admin/users/{keeper['id']}/export").status_code == 403


def test_members_cannot_export_others(client, make_user, login):
    bob = make_user("bob")
    login(client, make_user("eve"))
    assert client.get(f"/admin/users/{bob['id']}/export").status_code == 403


def test_exports_are_rate_limited(client, make_user, login):
    login(client, make_user("bob"))
    for _ in range(5):
        assert client.get("/settings/export").status_code == 200
    assert client.get("/settings/export").status_code == 429


def test_feature_switch(app, client, make_user, login):
    set_feature(app, "user_data_export", False)
    login(client, make_user("bob"))
    assert client.get("/settings/export").status_code == 404
    assert b"Download my data" not in client.get("/settings").data


def _everything(archive: zipfile.ZipFile) -> bytes:
    return b"".join(archive.read(name) for name in archive.namelist())


def test_every_table_about_accounts_is_exported_or_left_out_on_purpose(app):
    """The export is an allow-list: a new table with a users foreign key must be decided on."""
    from bananawiki.wiki.features.user_data_export import service

    tables = {table for table, _columns in in_app(app, service.user_tables)}
    exported = {spec.table for spec in service.EXPORTED}
    assert tables - exported - set(service.NOT_EXPORTED) == set()
    assert not exported & set(service.NOT_EXPORTED)
    def columns(spec):
        cursor = service.db.execute(spec.sql + " LIMIT 0", ["x"] * spec.sql.count("?"))
        return {column[0] for column in cursor.description}

    for spec in service.EXPORTED:
        assert not {"password", "token_hash", "credential_stamp", "secret", "invite_code"} & in_app(
            app, lambda spec=spec: columns(spec)), spec.table


def test_export_leaves_out_what_administrators_keep_to_themselves(app, client, make_user, login, db):
    bob, boss = make_user("bob"), make_user("boss", role="admin")
    db.executemany(
        "INSERT INTO suspension_audit (user_id, action, reason, reason_visible, time_visible, duration, "
        "suspended_until, performed_by, created_at) VALUES (?, 'suspend', ?, ?, ?, '1d', ?, ?, '2026-01-01 00:00:00')",
        [(bob["id"], "internal: suspected sockpuppet", 0, 0, "2031-05-05 00:00:00", boss["id"]),
         (bob["id"], "Spam", 1, 1, "2030-01-01 00:00:00", boss["id"])])
    db.execute("INSERT INTO impersonation_logs (admin_id, target_user_id, started_at) VALUES (?, ?, "
               "'2026-01-02 00:00:00')", (boss["id"], bob["id"]))
    db.execute("INSERT INTO role_history (user_id, old_role, new_role, changed_by, changed_at) VALUES "
               "(?, 'user', 'editor', ?, '2026-01-03 00:00:00')", (bob["id"], boss["id"]))
    db.execute("INSERT INTO user_custom_tags (user_id, label, color, sort_order) VALUES (?, 'Watch list', '#ff0000', 0)",
               (bob["id"],))
    db.execute("UPDATE users SET suspended_until = '2031-05-05 00:00:00', suspend_time_visible = 0 WHERE id = ?",
               (bob["id"],))
    login(client, bob)
    archive = _archive(client.get("/settings/export"))
    everything = _everything(archive)
    for hidden in (b"sockpuppet", b"Watch list", boss["id"].encode(), b"2031-05-05"):
        assert hidden not in everything, hidden
    audit = json.loads(archive.read("data/suspension_audit.json"))
    assert [row["reason"] for row in audit] == [None, "Spam"]
    assert json.loads(archive.read("data/role_history.json"))[0]["new_role"] == "editor"
    assert "data/impersonation_logs.json" not in archive.namelist()
    assert json.loads(archive.read("account.json"))["suspended_until"] is None


def test_export_leaves_out_credentials_and_stored_api_answers(app, client, make_user, login, db):
    from .api_support import enable_api, issue

    enable_api(app, db)
    bob = make_user("bob", role="admin")
    issue(app, bob, ["tokens"])
    db.execute("INSERT INTO api_service__idempotency (user_id, token_id, idempotency_key, request_hash, status_code, "
               "response_body, created_at) VALUES (?, 1, 'k', 'x', 201, '{\"token\": \"raw-token-value\"}', "
               "datetime('now'))", (bob["id"],))
    login(client, bob)
    archive = _archive(client.get("/settings/export"))
    tokens = json.loads(archive.read("data/api_service__tokens.json"))
    assert len(tokens) == 1 and not {"token_hash", "credential_stamp"} & set(tokens[0])
    assert "data/api_service__idempotency.json" not in archive.namelist()
    assert b"raw-token-value" not in _everything(archive)


def test_export_keeps_content_only_where_the_account_can_still_read_it(app, client, make_user, login, db):
    bob, eve = make_user("bob", role="editor"), make_user("eve", role="editor")
    open_category, secret_category = make_category(app, "Open"), make_category(app, "Secret")
    make_page(app, "Open doc", "open words", author_id=bob["id"], category_id=open_category["id"])
    hidden = make_page(app, "Hidden doc", "hidden words", author_id=bob["id"], category_id=secret_category["id"])
    folder = Path(app.config["BW"].folders.attachments)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stored.txt").write_bytes(b"attached file")
    db.execute("INSERT INTO page_attachments (page_id, filename, original_name, file_size, uploaded_by) "
               "VALUES (?, 'stored.txt', 'plans.txt', 13, ?)", (hidden["id"], bob["id"]))
    # A board of eve's that bob could comment on while it was shared with him.
    board = db.insert("kanban_boards", {"title": "Layoffs", "created_by": eve["id"], "visibility": "private"})
    column = db.insert("kanban_columns", {"board_id": board, "title": "To do"})
    ticket = db.insert("kanban_tickets", {"column_id": column, "title": "Who goes", "created_by": eve["id"]})
    db.insert("kanban_ticket_comments", {"ticket_id": ticket, "user_id": bob["id"], "content": "board words"})
    # A group bob was banned from.
    group = db.insert("group_chats", {"name": "Plotters", "invite_code": "abc123", "creator_id": eve["id"]})
    db.insert("group_members", {"group_id": group, "user_id": bob["id"], "banned": 1})
    db.insert("group_messages", {"group_id": group, "sender_id": bob["id"], "content": "group words"})
    restrict(db, bob, read=[open_category["id"]], write=[open_category["id"]])
    login(client, bob)
    archive = _archive(client.get("/settings/export"))
    history = {row["page_id"]: row for row in json.loads(archive.read("data/page_history.json"))}
    assert len(history) == 2 and history[hidden["id"]]["content"] is None and history[hidden["id"]]["title"] is None
    assert [row["content"] for row in history.values() if row["page_id"] != hidden["id"]] == ["open words"]
    attachment = json.loads(archive.read("data/page_attachments.json"))[0]
    assert attachment["page_id"] == hidden["id"] and attachment["original_name"] is None
    comment = json.loads(archive.read("data/kanban_ticket_comments.json"))[0]
    assert comment["board_id"] == board and comment["content"] is None
    assert json.loads(archive.read("data/group_messages.json"))[0]["content"] is None
    everything = _everything(archive)
    for secret in (b"hidden words", b"Hidden doc", b"plans.txt", b"attached file", b"board words", b"group words"):
        assert secret not in everything, secret
    assert not any(name.startswith("files/page_attachments/") for name in archive.namelist())
