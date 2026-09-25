"""Regression tests for the collaboration security fixes.

Each class covers one audit finding: kanban read and write checks, category
deletion, the page.delete permission, group bans, chat and personal data
exports, contribution access, temporary roles, rate limits on expensive GETs,
attachment download types and API token revocation.
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

import config
import db


def _make_user(name, role="user", password=None):
    """Create *name* with password ``<name>pw1234`` (or *password*)."""
    return db.create_user(name, generate_password_hash(password or f"{name}pw1234"), role=role)


def _client(username, password=None, ip="127.0.0.1"):
    """Return a fresh test client logged in as *username*."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    c.post("/login", data={"username": username, "password": password or f"{username}pw1234"})
    return c


def _zip_members(response):
    """Return ``{name: bytes}`` for a ZIP download."""
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    return {name: archive.read(name) for name in archive.namelist()}


def _export_text(response):
    """Return the whole body of an export, unpacked when it is a ZIP."""
    body = response.get_data()
    if body[:2] == b"PK":
        return b"".join(_zip_members(response).values())
    return body


def _active_token_count(user_id):
    with db.get_db_context() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM api_service__tokens WHERE user_id=? AND active=1",
            (user_id,),
        ).fetchone()[0]


def _new_board(title, owner_id, visibility="public"):
    board_id = db.kanban_create_board(title, "", owner_id)
    db.kanban_create_column(board_id, "To Do", 0)
    db.kanban_create_column(board_id, "Done", 1)
    db.kanban_set_board_visibility(board_id, visibility)
    return board_id


def _first_column(board_id):
    return db.kanban_list_columns(board_id)[0]["id"]


# ---------------------------------------------------------------------------
# Kanban: one read check for every path (embed, tickets, comments, sync)
# ---------------------------------------------------------------------------

class TestKanbanReadCheck:
    def test_embed_hidden_from_user_without_kanban_access(self, admin_user):
        _make_user("plain")
        board_id = _new_board("Secret Board", admin_user)
        db.kanban_create_ticket(_first_column(board_id), "SALARY-TICKET", "confidential", admin_user)

        c = _client("plain")
        assert c.get(f"/api/embed/kanban/{board_id}").status_code == 403
        sync = c.get(f"/api/embed/kanban/{board_id}/sync?since=0")
        assert sync.status_code == 403
        assert b"SALARY-TICKET" not in sync.data

    def test_embed_works_for_user_with_global_access(self, admin_user):
        _make_user("ed1", role="editor")
        db.update_site_settings(kanban_access="editor")
        board_id = _new_board("Team Board", admin_user)
        db.kanban_create_ticket(_first_column(board_id), "TEAM-TICKET", "", admin_user)

        c = _client("ed1")
        resp = c.get(f"/api/embed/kanban/{board_id}")
        assert resp.status_code == 200
        assert b"TEAM-TICKET" in resp.data

    def test_embed_closed_when_kanban_plugin_disabled(self, admin_user):
        board_id = _new_board("Board", admin_user)
        db.kanban_create_ticket(_first_column(board_id), "STILL-HERE", "", admin_user)
        c = _client("admin", "admin123")
        assert c.get(f"/api/embed/kanban/{board_id}").status_code == 200

        db.disable_plugin("kanban")
        resp = c.get(f"/api/embed/kanban/{board_id}")
        assert resp.status_code in (403, 404)
        assert b"STILL-HERE" not in resp.data

    def test_one_share_does_not_open_other_public_boards(self, admin_user):
        sharee = _make_user("sharee")
        board_a = _new_board("Board A", admin_user, visibility="private")
        board_b = _new_board("Board B", admin_user)
        db.kanban_set_board_share(board_a, "user", sharee, "view")
        ticket_b = db.kanban_create_ticket(_first_column(board_b), "B-SECRET", "", admin_user)
        ticket_a = db.kanban_create_ticket(_first_column(board_a), "A-SHARED", "", admin_user)

        c = _client("sharee")
        # The shared board still works.
        assert c.get(f"/api/kanban/tickets/{ticket_a}").status_code == 200
        assert c.get(f"/api/embed/kanban/{board_a}").status_code == 200
        # The unrelated public board does not.
        resp = c.get(f"/api/kanban/tickets/{ticket_b}")
        assert resp.status_code == 403
        assert b"B-SECRET" not in resp.data
        resp = c.post(f"/api/kanban/tickets/{ticket_b}/comments", json={"content": "hi"})
        assert resp.status_code == 403
        assert db.kanban_list_ticket_comments(ticket_b) == []
        assert c.get(f"/api/kanban/{board_b}/activity").status_code == 403
        assert c.get(f"/api/kanban/{board_b}/sync").status_code == 403
        assert c.get(f"/api/embed/kanban/{board_b}").status_code == 403

    def test_global_user_cannot_read_private_board_through_side_routes(self, admin_user):
        _make_user("ed2", role="editor")
        db.update_site_settings(kanban_access="editor")
        board_id = _new_board("Priv", admin_user, visibility="private")
        ticket_id = db.kanban_create_ticket(_first_column(board_id), "PRIV-TICKET", "", admin_user)

        c = _client("ed2")
        assert c.get(f"/kanban/{board_id}").status_code == 302
        for url in (
            f"/api/embed/kanban/{board_id}",
            f"/api/embed/kanban/{board_id}/sync?since=0",
            f"/api/kanban/tickets/{ticket_id}",
            f"/api/kanban/{board_id}/activity",
            f"/api/kanban/{board_id}/sync",
        ):
            resp = c.get(url)
            assert resp.status_code == 403, url
            assert b"PRIV-TICKET" not in resp.data, url


# ---------------------------------------------------------------------------
# Kanban: global write access only reaches boards the user can view
# ---------------------------------------------------------------------------

class TestKanbanWriteNeedsView:
    def test_global_writer_cannot_change_hidden_private_board(self, admin_user):
        owner = _make_user("eda", role="editor")
        _make_user("edb", role="editor")
        db.update_site_settings(kanban_access="editor", kanban_write_access="editor")
        board_id = _new_board("A private", owner, visibility="private")
        columns = db.kanban_list_columns(board_id)
        ticket_id = db.kanban_create_ticket(columns[0]["id"], "A-SECRET", "", owner)

        c = _client("edb")
        assert c.put(f"/api/kanban/tickets/{ticket_id}", json={"title": "defaced"}).status_code == 403
        assert db.kanban_get_ticket(ticket_id)["title"] == "A-SECRET"
        c.post(f"/kanban/{board_id}/edit", data={"title": "renamed-by-B"})
        assert db.kanban_get_board(board_id)["title"] == "A private"
        assert c.delete(f"/api/kanban/columns/{columns[1]['id']}").status_code == 403
        assert len(db.kanban_list_columns(board_id)) == 2

    def test_global_writer_still_edits_boards_it_can_view(self, admin_user):
        owner = _make_user("eda", role="editor")
        _make_user("edb", role="editor")
        db.update_site_settings(kanban_access="editor", kanban_write_access="editor")
        board_id = _new_board("Open board", owner)
        ticket_id = db.kanban_create_ticket(_first_column(board_id), "Old", "", owner)

        c = _client("edb")
        assert c.put(f"/api/kanban/tickets/{ticket_id}", json={"title": "New"}).status_code == 200
        assert db.kanban_get_ticket(ticket_id)["title"] == "New"

    def test_write_share_on_private_board_still_works(self, admin_user):
        owner = _make_user("eda", role="editor")
        writer = _make_user("edw", role="editor")
        db.update_site_settings(kanban_access="editor", kanban_write_access="admin")
        board_id = _new_board("Shared", owner, visibility="private")
        db.kanban_set_board_share(board_id, "user", writer, "write")
        ticket_id = db.kanban_create_ticket(_first_column(board_id), "Old", "", owner)

        c = _client("edw")
        assert c.put(f"/api/kanban/tickets/{ticket_id}", json={"title": "Edited"}).status_code == 200


# ---------------------------------------------------------------------------
# Category deletion goes through the per-page delete rules
# ---------------------------------------------------------------------------

def _editor_permissions(extra=()):
    """Return the default editor permissions plus *extra*."""
    from helpers._permissions import get_default_permissions
    return set(get_default_permissions("editor")) | set(extra)


class TestCategoryDelete:
    def test_restricted_editor_cannot_delete_foreign_category(self, admin_user):
        editor = _make_user("ed", role="editor")
        own = db.create_category("EditorArea")
        legal = db.create_category("Legal")
        db.set_user_permissions(
            editor, _editor_permissions({"page.delete"}),
            write_restricted=True, write_category_ids=[own],
        )
        page_id = db.create_page("Contract", "contract", "LEGAL TEXT", category_id=legal, user_id=admin_user)

        c = _client("ed")
        for action in ("delete", "uncategorize"):
            c.post(f"/category/{legal}/delete", data={"page_action": action})
            assert db.get_category(legal) is not None
            page = db.get_page(page_id)
            assert page is not None and page["category_id"] == legal
            assert not page["pending_deletion"]

    def test_restricted_editor_cannot_move_pages_into_foreign_category(self, admin_user):
        editor = _make_user("ed", role="editor")
        own = db.create_category("EditorArea")
        legal = db.create_category("Legal")
        db.set_user_permissions(
            editor, _editor_permissions(), write_restricted=True, write_category_ids=[own],
        )
        page_id = db.create_page("Mine", "mine", "x", category_id=own, user_id=admin_user)

        c = _client("ed")
        c.post(f"/category/{own}/delete", data={"page_action": "move", "target_category_id": str(legal)})
        assert db.get_category(own) is not None
        assert db.get_page(page_id)["category_id"] == own

    def test_deleting_pages_needs_page_delete(self, admin_user):
        _make_user("ed", role="editor")
        cat = db.create_category("Notes")
        page_id = db.create_page("Note", "note", "x", category_id=cat, user_id=admin_user)

        c = _client("ed")
        c.post(f"/category/{cat}/delete", data={"page_action": "delete"})
        assert db.get_category(cat) is not None
        assert db.get_page(page_id) is not None

        # Keeping the pages only needs category.delete, as before.
        c.post(f"/category/{cat}/delete", data={"page_action": "uncategorize"})
        assert db.get_category(cat) is None
        assert db.get_page(page_id)["category_id"] is None

    def test_protected_page_blocks_category_delete(self, admin_user):
        editor = _make_user("ed", role="editor")
        db.set_user_permissions(editor, _editor_permissions({"page.delete"}))
        db.update_site_settings(page_protection_enabled=1)
        legal = db.create_category("Legal")
        protected = db.create_page("Contract", "contract", "LEGAL", category_id=legal, user_id=admin_user)
        other = db.create_page("Other", "other-legal", "x", category_id=legal, user_id=admin_user)
        db.set_page_protection(protected, admin_user)

        c = _client("ed")
        c.post("/page/contract/delete")
        assert db.get_page(protected) is not None
        c.post(f"/category/{legal}/delete", data={"page_action": "delete"})
        assert db.get_category(legal) is not None
        for page_id in (protected, other):
            page = db.get_page(page_id)
            assert page is not None and not page["pending_deletion"]

    def test_scheduled_page_blocks_category_delete(self, logged_in_admin, admin_user):
        cat = db.create_category("Events")
        page_id = db.create_page("Party", "party", "x", category_id=cat, user_id=admin_user)
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        db.set_page_expiry(page_id, future)

        logged_in_admin.post(f"/category/{cat}/delete", data={"page_action": "delete"})
        assert db.get_category(cat) is not None
        assert db.get_page(page_id) is not None

    def test_category_delete_uses_deletion_slowdown(self, logged_in_admin, admin_user):
        assert db.is_plugin_enabled("deletion_slowdown")
        cat = db.create_category("Old")
        page_ids = [
            db.create_page(f"Old {i}", f"old-{i}", "x", category_id=cat, user_id=admin_user)
            for i in range(2)
        ]
        logged_in_admin.post(f"/category/{cat}/delete", data={"page_action": "delete"})

        assert db.get_category(cat) is None
        pending = {row["id"] for row in db.list_pending_deletions()}
        for page_id in page_ids:
            page = db.get_page(page_id)
            assert page is not None and page["pending_deletion"] == 1
            assert page_id in pending

    def test_category_delete_without_slowdown_deletes_pages(self, logged_in_admin, admin_user):
        db.disable_plugin("deletion_slowdown")
        cat = db.create_category("Gone")
        page_id = db.create_page("Gone", "gone", "x", category_id=cat, user_id=admin_user)
        logged_in_admin.post(f"/category/{cat}/delete", data={"page_action": "delete"})
        assert db.get_category(cat) is None
        assert db.get_page(page_id) is None


# ---------------------------------------------------------------------------
# page.delete on the web delete route and button
# ---------------------------------------------------------------------------

class TestPageDeletePermission:
    def test_editor_without_page_delete_cannot_delete(self, admin_user, editor_user):
        page_id = db.create_page("Victim", "victim", "x", user_id=admin_user)
        c = _client("editor", "editor123")
        c.post("/page/victim/delete")
        page = db.get_page(page_id)
        assert page is not None and not page["pending_deletion"]

    def test_delete_button_follows_permission(self, admin_user, editor_user):
        db.create_page("Victim", "victim", "x", user_id=admin_user)
        c = _client("editor", "editor123")
        assert b'action="/page/victim/delete"' not in c.get("/page/victim").data

        db.set_user_permissions(editor_user, _editor_permissions({"page.delete"}))
        assert b'action="/page/victim/delete"' in c.get("/page/victim").data

    def test_editor_with_page_delete_can_delete(self, admin_user, editor_user):
        page_id = db.create_page("Victim", "victim", "x", user_id=admin_user)
        db.set_user_permissions(editor_user, _editor_permissions({"page.delete"}))
        c = _client("editor", "editor123")
        c.post("/page/victim/delete")
        page = db.get_page(page_id)
        # deletion_slowdown is enabled in tests, so the page is queued.
        assert page is None or page["pending_deletion"] == 1


# ---------------------------------------------------------------------------
# Group bans end read and moderation access
# ---------------------------------------------------------------------------

class TestGroupBan:
    @pytest.fixture
    def banned_group(self, admin_user, tmp_path, monkeypatch):
        folder = tmp_path / "chat_attachments"
        folder.mkdir()
        monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", str(folder))
        owner = _make_user("owner1")
        member = _make_user("mem1")
        moderator = _make_user("mod1")
        group = db.create_group_chat("G", owner)
        gid = group["id"]
        db.add_group_member(gid, member)
        db.add_group_member(gid, moderator)
        db.set_group_member_role(gid, moderator, "moderator")
        oc = _client("owner1")
        oc.post(f"/groups/{gid}/kick", data={"user_id": member, "permanent": "1"})
        oc.post(f"/groups/{gid}/kick", data={"user_id": moderator, "permanent": "1"})
        assert db.is_group_member_banned(gid, member)
        assert db.is_group_member_banned(gid, moderator)
        oc.post(
            f"/groups/{gid}/send",
            data={"content": "SECRET-AFTER-BAN", "attachment": (io.BytesIO(b"filedata"), "s.txt")},
            content_type="multipart/form-data",
        )
        attachments = [a for m in db.get_group_messages(gid) for a in (m.get("attachments") or [])]
        assert attachments
        return {"gid": gid, "group": group, "moderator": moderator, "attachment": attachments[0]["id"]}

    def test_banned_member_cannot_read(self, banned_group):
        gid = banned_group["gid"]
        c = _client("mem1")
        resp = c.get(f"/groups/{gid}")
        assert resp.status_code == 302
        assert b"SECRET-AFTER-BAN" not in resp.data
        resp = c.get(f"/groups/{gid}/messages")
        assert resp.status_code == 403
        assert b"SECRET-AFTER-BAN" not in resp.data
        assert c.get(f"/groups/attachments/{banned_group['attachment']}/download").status_code == 403
        assert gid not in [g["id"] for g in db.get_user_groups(db.get_user_by_username("mem1")["id"])]

    def test_banned_moderator_loses_moderation(self, banned_group):
        gid = banned_group["gid"]
        c = _client("mod1")
        c.post(f"/groups/{gid}/clear")
        assert "SECRET-AFTER-BAN" in [m["content"] for m in db.get_group_messages(gid)]
        c.post(f"/groups/{gid}/unban", data={"user_id": banned_group["moderator"]})
        assert db.is_group_member_banned(gid, banned_group["moderator"])
        c.post("/groups/join", data={"invite_code": banned_group["group"]["invite_code"]})
        assert not db.is_group_member(gid, banned_group["moderator"])
        assert db.is_group_member_banned(gid, banned_group["moderator"])

    def test_owner_can_still_unban(self, banned_group):
        gid = banned_group["gid"]
        oc = _client("owner1")
        oc.post(f"/groups/{gid}/unban", data={"user_id": banned_group["moderator"]})
        assert not db.is_group_member_banned(gid, banned_group["moderator"])

    def test_banned_user_keeps_own_messages_in_personal_export(self, admin_user):
        owner = _make_user("owner1")
        member = _make_user("mem1")
        group = db.create_group_chat("G", owner)
        db.add_group_member(group["id"], member)
        db.send_group_message(group["id"], member, "MY-OWN-WORDS")
        db.ban_group_member(group["id"], member)

        members = _zip_members(_client("mem1").get("/settings/export"))
        exported = json.loads(members["group_messages.json"])
        assert [row["content"] for row in exported] == ["MY-OWN-WORDS"]


# ---------------------------------------------------------------------------
# Exports: no peer IP addresses, no retracted DM text, no revoked boards
# ---------------------------------------------------------------------------

class TestExportsPrivacy:
    def test_dm_export_has_no_peer_ip(self, admin_user):
        alice = _make_user("alice")
        bob = _make_user("bob")
        chat_id = db.get_or_create_chat(alice, bob)["id"]
        _client("bob", ip="203.0.113.77").post(f"/chats/{chat_id}/send", data={"content": "hello"})
        assert db.get_chat_messages(chat_id)[0]["ip_address"] == "203.0.113.77"

        resp = _client("alice").get(f"/chats/{chat_id}/export")
        assert resp.status_code == 200
        body = _export_text(resp)
        assert b"hello" in body
        assert b"203.0.113.77" not in body

    def test_group_export_ips_only_for_site_admins(self, admin_user):
        owner = _make_user("gowner")
        member = _make_user("gmem")
        group = db.create_group_chat("G", owner)
        db.add_group_member(group["id"], member)
        _client("gmem", ip="198.51.100.9").post(f"/groups/{group['id']}/send", data={"content": "hi"})

        resp = _client("gowner").get(f"/groups/{group['id']}/export")
        assert resp.status_code == 200
        assert b"198.51.100.9" not in _export_text(resp)

        resp = _client("admin", "admin123").get(f"/groups/{group['id']}/export")
        assert resp.status_code == 200
        assert b"198.51.100.9" in _export_text(resp)

    def test_personal_export_hides_other_users_deleted_dm_text(self, admin_user):
        alice = _make_user("alice")
        bob = _make_user("bob")
        chat_id = db.get_or_create_chat(alice, bob)["id"]
        bc = _client("bob")
        bc.post(f"/chats/{chat_id}/send", data={"content": "REGRETTED-MESSAGE"})
        ac = _client("alice")
        ac.post(f"/chats/{chat_id}/send", data={"content": "ALICE-RETRACTED"})
        by_content = {m["content"]: m["id"] for m in db.get_chat_messages(chat_id)}
        bc.post(f"/chats/{chat_id}/delete_message", data={"message_id": by_content["REGRETTED-MESSAGE"]})
        ac.post(f"/chats/{chat_id}/delete_message", data={"message_id": by_content["ALICE-RETRACTED"]})

        members = _zip_members(ac.get("/settings/export"))
        dm = json.loads(members["dm_messages.json"])
        contents = [row["content"] for row in dm]
        assert "REGRETTED-MESSAGE" not in contents
        # The user's own deleted message is still their data.
        assert "ALICE-RETRACTED" in contents
        assert all(row["is_deleted"] for row in dm)

    def test_personal_export_drops_boards_after_share_removed(self, admin_user):
        member = _make_user("exmember")
        board_id = _new_board("HR", admin_user, visibility="private")
        db.kanban_set_board_share(board_id, "user", member, "view")
        column = _first_column(board_id)
        old_ticket = db.kanban_create_ticket(column, "old ticket", "", admin_user)
        xc = _client("exmember")
        assert xc.post(f"/api/kanban/tickets/{old_ticket}/comments",
                       json={"content": "my one comment"}).status_code in (200, 201)

        db.kanban_remove_board_share(board_id, "user", member)
        db.kanban_create_ticket(column, "NEW-CONFIDENTIAL-TICKET", "salary data", admin_user)

        members = _zip_members(xc.get("/settings/export"))
        kanban = b"".join(data for name, data in members.items() if name.startswith("kanban/"))
        assert b"NEW-CONFIDENTIAL-TICKET" not in kanban
        assert b"salary data" not in kanban
        # The user's own comment is still exported.
        assert b"my one comment" in kanban

    def test_personal_export_follows_canvas_permissions(self, admin_user):
        member = _make_user("member")
        layout_id = db.canvas_create_layout(
            "Plans", admin_user, description="CANVAS-SECRET-DESC", visibility="private",
        )
        db.canvas_set_permission(layout_id, "view", user_id=member)
        mc = _client("member")
        exported = b"".join(_zip_members(mc.get("/settings/export")).values())
        assert b"CANVAS-SECRET-DESC" in exported

        # Taking the permission away also takes the layout out of the export.
        db.canvas_set_permission(layout_id, "none", user_id=member)
        exported = b"".join(_zip_members(mc.get("/settings/export")).values())
        assert b"CANVAS-SECRET-DESC" not in exported

    def test_personal_export_keeps_boards_still_shared(self, admin_user):
        member = _make_user("member")
        board_id = _new_board("Team", admin_user, visibility="private")
        db.kanban_set_board_share(board_id, "user", member, "view")
        ticket = db.kanban_create_ticket(_first_column(board_id), "TEAM-TICKET", "", admin_user)
        mc = _client("member")
        mc.post(f"/api/kanban/tickets/{ticket}/comments", json={"content": "note"})

        members = _zip_members(mc.get("/settings/export"))
        assert any(name.startswith("kanban/boards/") for name in members)
        kanban = b"".join(data for name, data in members.items() if name.startswith("kanban/"))
        assert b"TEAM-TICKET" in kanban


# ---------------------------------------------------------------------------
# Proposing an edit needs the same access as viewing the page
# ---------------------------------------------------------------------------

class TestProposeEditVisibility:
    def test_propose_edit_refuses_deindexed_page(self, admin_user, regular_user):
        db.update_site_settings(contribution_approval_enabled=1)
        page_id = db.create_page("HR plan", "hr-plan", "DEINDEXED-SECRET-CONTENT", user_id=admin_user)
        db.set_page_deindexed(page_id, True)

        c = _client("user", "user123")
        resp = c.get("/page/hr-plan/propose-edit")
        assert resp.status_code == 403
        assert b"DEINDEXED-SECRET-CONTENT" not in resp.data

    def test_propose_edit_still_works_on_visible_page(self, admin_user, regular_user):
        db.update_site_settings(contribution_approval_enabled=1)
        db.create_page("Open", "open-page", "VISIBLE-CONTENT", user_id=admin_user)
        resp = _client("user", "user123").get("/page/open-page/propose-edit")
        assert resp.status_code == 200
        assert b"VISIBLE-CONTENT" in resp.data


# ---------------------------------------------------------------------------
# Owner status and temporary role schedules
# ---------------------------------------------------------------------------

class TestOwnerToggleWithTemporaryRole:
    def test_toggle_owner_refused_while_role_schedule_exists(self, admin_user):
        victim = _make_user("victimadm", role="admin")
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        db.set_role_expiry(victim, "user", expires_at=future, set_by=admin_user)

        c = _client("victimadm")
        c.post("/settings", data={"action": "toggle_owner", "password": "victimadmpw1234"})
        assert db.get_user_by_id(victim)["role"] == "admin"

    def test_toggle_owner_works_without_schedule(self, admin_user):
        other = _make_user("otheradm", role="admin")
        c = _client("otheradm")
        c.post("/settings", data={"action": "toggle_owner", "password": "otheradmpw1234"})
        assert db.get_user_by_id(other)["role"] == "owner"


# ---------------------------------------------------------------------------
# Rate limits on expensive GETs apply to browser requests too
# ---------------------------------------------------------------------------

class TestExpensiveGetRateLimits:
    def test_chat_export_limited_with_html_accept(self, admin_user):
        alice = _make_user("alice")
        bob = _make_user("bob")
        chat_id = db.get_or_create_chat(alice, bob)["id"]
        db.send_chat_message(chat_id, bob, "hi", "1.2.3.4")
        c = _client("alice")
        codes = [
            c.get(f"/chats/{chat_id}/export", headers={"Accept": "text/html"}).status_code
            for _ in range(12)
        ]
        assert codes[:10] == [200] * 10
        assert 429 in codes[10:]

    def test_personal_export_limited(self, admin_user):
        _make_user("alice")
        c = _client("alice")
        codes = [
            c.get("/settings/export", headers={"Accept": "text/html"}).status_code
            for _ in range(7)
        ]
        assert 429 in codes

    def test_page_renders_keep_html_exemption(self, admin_user):
        _make_user("alice")
        c = _client("alice")
        # /settings is limited to 10 per minute for its POSTs; browser
        # page loads are not counted against that.
        codes = [
            c.get("/settings", headers={"Accept": "text/html"}).status_code
            for _ in range(15)
        ]
        assert codes == [200] * 15


# ---------------------------------------------------------------------------
# Attachment downloads only keep display-only content types
# ---------------------------------------------------------------------------

class TestAttachmentDownloadTypes:
    def test_chat_attachment_script_served_as_octet_stream(self, admin_user, tmp_path, monkeypatch):
        folder = tmp_path / "chat"
        folder.mkdir()
        monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", str(folder))
        alice = _make_user("alice")
        bob = _make_user("bob")
        chat_id = db.get_or_create_chat(alice, bob)["id"]
        message_id = db.send_chat_message(chat_id, bob, "files")
        (folder / "a.bin").write_bytes(b"alert(1)")
        (folder / "b.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
        js = db.add_chat_attachment(message_id, "a.bin", "evil.js", 8)
        png = db.add_chat_attachment(message_id, "b.bin", "pic.png", 8)

        c = _client("alice")
        resp = c.get(f"/chats/attachments/{js}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert c.get(f"/chats/attachments/{png}/download").mimetype == "image/png"

    def test_group_attachment_stylesheet_served_as_octet_stream(self, admin_user, tmp_path, monkeypatch):
        folder = tmp_path / "chat"
        folder.mkdir()
        monkeypatch.setattr(config, "CHAT_ATTACHMENT_FOLDER", str(folder))
        owner = _make_user("owner1")
        group = db.create_group_chat("G", owner)
        message_id = db.send_group_message(group["id"], owner, "files")
        (folder / "c.bin").write_bytes(b"body{}")
        css = db.add_group_attachment(message_id, "c.bin", "evil.css", 6)

        resp = _client("owner1").get(f"/groups/attachments/{css}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"

    def test_kanban_attachment_script_served_as_octet_stream(self, admin_user, tmp_path, monkeypatch):
        folder = tmp_path / "kanban"
        folder.mkdir()
        monkeypatch.setattr(config, "KANBAN_ATTACHMENT_FOLDER", str(folder))
        board_id = _new_board("Files", admin_user)
        ticket_id = db.kanban_create_ticket(_first_column(board_id), "T", "", admin_user)
        (folder / "k.bin").write_bytes(b"alert(1)")
        (folder / "i.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
        js = db.kanban_add_ticket_attachment(ticket_id, "k.bin", "evil.js", 8, admin_user)
        png = db.kanban_add_ticket_attachment(ticket_id, "i.bin", "pic.png", 8, admin_user)

        c = _client("admin", "admin123")
        resp = c.get(f"/api/kanban/attachments/{js}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/octet-stream"
        assert c.get(f"/api/kanban/attachments/{png}/download").mimetype == "image/png"


# ---------------------------------------------------------------------------
# API tokens die with the password
# ---------------------------------------------------------------------------

class TestApiTokenRevocation:
    def test_own_password_change_revokes_tokens(self, admin_user):
        alice = _make_user("alice")
        bystander = _make_user("bob")
        db.create_api_token(alice, name="one")
        db.create_api_token(alice, name="two")
        db.create_api_token(bystander, name="other")

        c = _client("alice")
        c.post("/settings", data={
            "action": "change_password",
            "current_password": "alicepw1234",
            "new_password": "alice-new-pw-1",
            "confirm_password": "alice-new-pw-1",
        })
        assert _active_token_count(alice) == 0
        assert _active_token_count(bystander) == 1

    def test_failed_password_change_keeps_tokens(self, admin_user):
        alice = _make_user("alice")
        db.create_api_token(alice, name="one")
        _client("alice").post("/settings", data={
            "action": "change_password",
            "current_password": "wrong-password",
            "new_password": "alice-new-pw-1",
            "confirm_password": "alice-new-pw-1",
        })
        assert _active_token_count(alice) == 1

    @pytest.mark.parametrize("action", ["change_password", "set_temp_password", "revert_password"])
    def test_admin_password_reset_revokes_tokens(self, logged_in_admin, action):
        alice = _make_user("alice")
        if action == "revert_password":
            db.update_user(alice, original_password_backup=generate_password_hash("old-pw-123"))
        db.create_api_token(alice, name="one")
        logged_in_admin.post(f"/admin/users/{alice}/edit", data={
            "action": action,
            "password": "reset-pw-1234",
            "confirm_password": "reset-pw-1234",
        })
        assert _active_token_count(alice) == 0

    def test_logout_all_sessions_revokes_tokens(self, admin_user):
        alice = _make_user("alice")
        db.create_api_token(alice, name="one")
        _client("alice").post("/settings/sessions/logout-all")
        assert _active_token_count(alice) == 0

    def test_forced_password_change_revokes_tokens(self, admin_user):
        alice = _make_user("alice")
        db.update_user(alice, force_password_change=1)
        db.create_api_token(alice, name="one")
        _client("alice").post("/force-change-password", data={
            "action": "change_password",
            "new_password": "alice-new-pw-1",
            "confirm_password": "alice-new-pw-1",
        })
        assert db.get_user_by_id(alice)["force_password_change"] == 0
        assert _active_token_count(alice) == 0


# ---------------------------------------------------------------------------
# Variations of the attacks above, and legitimate use that must keep working
# ---------------------------------------------------------------------------

class TestKanbanVariants:
    def test_single_share_does_not_open_side_routes_of_public_board(self, admin_user, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KANBAN_ATTACHMENT_FOLDER", str(tmp_path))
        sharee = _make_user("sharee")
        board_a = _new_board("A", admin_user, visibility="private")
        board_b = _new_board("B", admin_user)
        db.kanban_set_board_share(board_a, "user", sharee, "view")
        ticket_b = db.kanban_create_ticket(_first_column(board_b), "B-SECRET", "", admin_user)
        (tmp_path / "f.bin").write_bytes(b"B-FILE")
        attachment = db.kanban_add_ticket_attachment(ticket_b, "f.bin", "b.txt", 6, admin_user)
        comment = db.kanban_add_ticket_comment(ticket_b, admin_user, "ADMIN-COMMENT")

        c = _client("sharee")
        assert c.get(f"/api/kanban/attachments/{attachment}/download").status_code == 403
        assert c.get(f"/api/kanban/tickets/{ticket_b}/attachments").status_code == 403
        assert c.get(f"/api/kanban/tickets/{ticket_b}/comments").status_code == 403
        assert c.get(f"/api/kanban/tickets/{ticket_b}/history").status_code == 403
        assert c.put(f"/api/kanban/comments/{comment}", json={"content": "x"}).status_code == 403
        assert c.delete(f"/api/kanban/comments/{comment}").status_code == 403
        assert c.get(f"/kanban/{board_b}/history").status_code == 302
        assert b"B-SECRET" not in c.get(f"/kanban/{board_b}/export").data

    def test_open_access_does_not_reach_private_boards(self, admin_user):
        owner = _make_user("own")
        _make_user("other")
        db.update_site_settings(kanban_open_access=1)
        private = _new_board("P", owner, visibility="private")
        public = _new_board("Q", owner)
        ticket_p = db.kanban_create_ticket(_first_column(private), "P-T", "", owner)
        ticket_q = db.kanban_create_ticket(_first_column(public), "Q-T", "", owner)

        c = _client("other")
        assert c.put(f"/api/kanban/tickets/{ticket_p}", json={"title": "x"}).status_code == 403
        assert c.post(f"/api/kanban/{private}/columns", json={"title": "new"}).status_code == 403
        assert c.post(f"/api/kanban/{private}/tickets/bulk",
                      json={"ticket_ids": [ticket_p], "action": "delete"}).status_code == 403
        assert db.kanban_get_ticket(ticket_p)["title"] == "P-T"
        # Open access still means write access to the boards everyone sees.
        assert c.put(f"/api/kanban/tickets/{ticket_q}", json={"title": "edited"}).status_code == 200

    def test_signed_out_public_access_stays_read_only(self, admin_user, monkeypatch):
        monkeypatch.setattr(config, "FORBID_PUBLIC_MODE", False, raising=False)
        db.update_site_settings(public_mode=1, kanban_public_access_enabled=1)
        public = _new_board("Pub", admin_user)
        private = _new_board("Priv", admin_user, visibility="private")
        ticket = db.kanban_create_ticket(_first_column(public), "PUB-TICKET", "", admin_user)
        db.kanban_create_ticket(_first_column(private), "PRIV-TICKET", "", admin_user)

        from app import app
        c = app.test_client()
        resp = c.get(f"/kanban/{public}")
        assert resp.status_code == 200 and b"PUB-TICKET" in resp.data
        assert b"PRIV-TICKET" not in c.get(f"/kanban/{private}").data
        for url in (
            f"/api/kanban/tickets/{ticket}",
            f"/api/kanban/tickets/{ticket}/comments",
            f"/api/kanban/{public}/activity",
            f"/api/embed/kanban/{public}",
            f"/kanban/{public}/history",
        ):
            assert c.get(url).status_code in (302, 401, 403), url

    def test_assignment_alone_does_not_export_the_board(self, admin_user):
        assignee = _make_user("assignee")
        board_id = _new_board("HR", admin_user, visibility="private")
        ticket = db.kanban_create_ticket(_first_column(board_id), "ASSIGNED", "", admin_user)
        db.kanban_set_ticket_assignees(ticket, [assignee])
        db.kanban_create_ticket(_first_column(board_id), "OTHER-CONFIDENTIAL", "salary", admin_user)

        members = _zip_members(_client("assignee").get("/settings/export"))
        assert not any(name.startswith("kanban/boards/") for name in members)
        kanban = b"".join(data for name, data in members.items() if name.startswith("kanban/"))
        assert b"OTHER-CONFIDENTIAL" not in kanban


class TestGroupBanVariants:
    def test_banned_member_cannot_leave_to_drop_the_ban(self, admin_user):
        owner = _make_user("owner1")
        member = _make_user("mem1")
        group = db.create_group_chat("G", owner)
        db.add_group_member(group["id"], member)
        db.ban_group_member(group["id"], member)

        c = _client("mem1")
        c.post(f"/groups/{group['id']}/leave")
        assert db.is_group_member_banned(group["id"], member)
        c.post("/groups/join", data={"invite_code": group["invite_code"]})
        assert not db.is_group_member(group["id"], member)

    def test_banned_moderator_cannot_kick_or_time_out(self, admin_user):
        owner = _make_user("owner1")
        moderator = _make_user("mod1")
        other = _make_user("other1")
        group = db.create_group_chat("G", owner)
        for user_id in (moderator, other):
            db.add_group_member(group["id"], user_id)
        db.set_group_member_role(group["id"], moderator, "moderator")
        db.ban_group_member(group["id"], moderator)

        c = _client("mod1")
        c.post(f"/groups/{group['id']}/kick", data={"user_id": other})
        c.post(f"/groups/{group['id']}/timeout", data={"user_id": other, "duration": "10"})
        assert db.is_group_member(group["id"], other)
        assert not db.is_group_member_timed_out(group["id"], other)

    def test_banned_site_admin_can_still_take_over(self, admin_user):
        owner = _make_user("owner1")
        group = db.create_group_chat("G", owner)
        db.add_group_member(group["id"], admin_user)
        db.ban_group_member(group["id"], admin_user)

        c = _client("admin", "admin123")
        assert c.get(f"/groups/{group['id']}").status_code == 200
        c.post(f"/groups/{group['id']}/admin_takeover")
        assert db.get_group_member_role(group["id"], admin_user) == "owner"
        assert not db.is_group_member_banned(group["id"], admin_user)


class TestCategoryDeleteVariants:
    def test_reserved_page_blocks_category_delete(self, admin_user):
        editor = _make_user("ed", role="editor")
        holder = _make_user("ed2", role="editor")
        db.set_user_permissions(editor, _editor_permissions({"page.delete"}))
        db.update_site_settings(page_reservations_enabled=1)
        cat = db.create_category("Work")
        page_id = db.create_page("Draft", "draft-x", "x", category_id=cat, user_id=admin_user)
        db.reserve_page(page_id, holder)

        _client("ed").post(f"/category/{cat}/delete", data={"page_action": "delete"})
        assert db.get_category(cat) is not None
        page = db.get_page(page_id)
        assert page is not None and not page["pending_deletion"]

    def test_already_queued_pages_stay_restorable(self, logged_in_admin, admin_user):
        cat = db.create_category("Mixed")
        queued = db.create_page("P1", "p1", "x", category_id=cat, user_id=admin_user)
        fresh = db.create_page("P2", "p2", "x", category_id=cat, user_id=admin_user)
        db.mark_page_pending_deletion(queued, admin_user)

        logged_in_admin.post(f"/category/{cat}/delete", data={"page_action": "delete"})
        assert db.get_category(cat) is None
        pending = {row["id"] for row in db.list_pending_deletions()}
        assert {queued, fresh} <= pending


class TestTemporaryRoleSuperuser:
    def test_superuser_cannot_be_given_a_temporary_role(self, admin_user):
        superuser = _make_user("superadm", role="admin")
        db.update_user(superuser, is_superuser=1)
        other = _make_user("normaladm", role="admin")
        future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

        c = _client("admin", "admin123")
        for user_id in (superuser, other):
            c.post("/admin/temporary/role", data={
                "user_id": user_id, "original_role": "user", "expires_at": future,
            })
        assert db.get_role_expiry(superuser) is None
        assert db.get_role_expiry(other) is not None


@pytest.mark.parametrize("name,expected", [
    ("evil.JS", "application/octet-stream"),
    ("evil.mjs", "application/octet-stream"),
    ("page.html", "application/octet-stream"),
    ("image.svg", "application/octet-stream"),
    ("style.css", "application/octet-stream"),
    ("data.xml", "application/octet-stream"),
    ("no-extension", "application/octet-stream"),
    ("pic.png.js", "application/octet-stream"),
    ("pic.PNG", "image/png"),
    ("song.mp3", "audio/mpeg"),
    ("notes.txt", "text/plain"),
])
def test_attachment_download_mimetype(name, expected):
    from routes.chat import attachment_download_mimetype
    assert attachment_download_mimetype(name) == expected
