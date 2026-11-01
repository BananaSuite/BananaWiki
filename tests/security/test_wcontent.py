"""Regression tests for the content and collaboration findings of the security audit (W2-5..W2-13, W4-2..W4-6).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

import io
import json
import re

from bananawiki.wiki import permissions
from bananawiki.wiki.features.pages import service
from tests.features.pages_support import get_page, in_app, make_category, make_page, restrict, set_feature


def _protect(app, db, page, controller):
    """Page protection on and *page* controlled by *controller* (blocks everyone else, administrators too)."""
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE id = ?",
               (controller["id"], page["id"]))


# W2-5


def test_w2_5_rename_leaves_pages_the_renamer_may_not_edit(app, client, login, make_user, db):
    secret, open_category = make_category(app, "Secret"), make_category(app, "Open")
    controller = make_user("controller", role="editor")
    hidden = make_page(app, "Board minutes", "See [x](/page/target-x)", category_id=secret["id"])
    guarded = make_page(app, "Guarded", "See [x](/page/target-x)", category_id=open_category["id"])
    _protect(app, db, guarded, controller)
    free = make_page(app, "Free", "See [x](/page/target-x)", category_id=open_category["id"])
    mallory = make_user("mallory", role="editor")
    restrict(db, mallory, read=[open_category["id"]], write=[open_category["id"]])
    db.execute("INSERT INTO drafts (page_id, user_id, content) VALUES (?, ?, ?)",
               (hidden["id"], controller["id"], "Draft [x](/page/target-x)"))
    mine = make_page(app, "Target X", "hi", category_id=open_category["id"], author_id=mallory["id"])
    login(client, mallory)

    assert client.post(f"/page/{mine['slug']}/rename", data={"new_slug": "chosen"}).status_code == 302

    for page in (hidden, guarded):
        after = get_page(app, page["id"])
        assert after["content"] == "See [x](/page/target-x)" and after["last_edited_by"] is None
        assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ? AND edited_by = ?",
                         (page["id"], mallory["id"])) == 0
    assert db.scalar("SELECT content FROM drafts WHERE page_id = ?", (hidden["id"],)) == "Draft [x](/page/target-x)"
    # Pages the renamer may edit still follow the new address.
    assert get_page(app, free["id"])["content"] == "See [x](/page/chosen)"


# W2-6


def test_w2_6_category_delete_does_not_move_protected_pages_or_foreign_subcategories(
        app, client, login, make_user, db):
    team, target = make_category(app, "Team"), make_category(app, "Target")
    controller = make_user("controller", role="editor")
    locked = make_page(app, "Locked", "text", category_id=team["id"])
    _protect(app, db, locked, controller)
    mallory = make_user("mallory", role="editor")
    restrict(db, mallory, read=[team["id"], target["id"]], write=[team["id"], target["id"]])
    login(client, mallory)
    assert get_page(app, locked["id"])["category_id"] == team["id"]
    client.post(f"/page/{locked['slug']}/move", data={"category_id": target["id"]})  # refused on its own

    for form in ({"page_action": "move", "target_category_id": target["id"]}, {"page_action": "uncategorize"}):
        client.post(f"/category/{team['id']}/delete", data=form)
        assert get_page(app, locked["id"])["category_id"] == team["id"]
        assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (team["id"],)) == 1

    # Without the protection: uncategorising still needs the top level, and a
    # subcategory the editor cannot write does not move up.
    db.execute("UPDATE pages SET category_id = ?, protected_by = NULL WHERE id = ?", (target["id"], locked["id"]))
    make_page(app, "Plain", "text", category_id=team["id"])
    client.post(f"/category/{team['id']}/delete", data={"page_action": "uncategorize"})
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (team["id"],)) == 1
    secret = make_category(app, "Secret child", parent_id=target["id"])
    client.post(f"/category/{target['id']}/delete", data={"page_action": "move", "target_category_id": team["id"]})
    assert db.scalar("SELECT parent_id FROM categories WHERE id = ?", (secret["id"],)) == target["id"]
    # What the editor may change still goes.
    db.execute("DELETE FROM categories WHERE id = ?", (secret["id"],))
    client.post(f"/category/{target['id']}/delete", data={"page_action": "move", "target_category_id": team["id"]})
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (target["id"],)) == 0
    assert get_page(app, locked["id"])["category_id"] == team["id"]


# W2-7

_NO_METADATA = (permissions.defaults("editor") - {"page.edit_metadata"}) | {"history.revert", "contribution.review"}


def test_w2_7_history_revert_keeps_the_title_without_edit_metadata(app, client, login, make_user, db):
    set_feature(app, "page_history", True)
    page = make_page(app, "Original title", "v1")
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=None, title="Current title", content="v2"))
    first = db.scalar("SELECT MIN(id) FROM page_history WHERE page_id = ?", (page["id"],))
    eddie = make_user("eddie", role="editor")
    restrict(db, eddie, keys=_NO_METADATA)
    login(client, eddie)
    assert client.post(f"/page/{page['slug']}/edit/title", data={"title": "nope"}).status_code == 403

    assert client.post(f"/page/{page['slug']}/revert/{first}").status_code == 302

    after = get_page(app, page["id"])
    assert (after["title"], after["content"]) == ("Current title", "v1")


def test_w2_7_contribution_approval_keeps_the_title_without_edit_metadata(app, client, login, make_user, db):
    set_feature(app, "contributions", True)
    page = make_page(app, "Stable title", "text")
    login(client, make_user("proposer"))
    client.post(f"/page/{page['slug']}/propose-edit", data={"title": "Hijacked title", "content": "new text",
                                                            "reason": "x"})
    contribution_id = db.scalar("SELECT id FROM pending_contributions")
    reviewer = make_user("reviewer", role="editor")
    restrict(db, reviewer, keys=_NO_METADATA)
    client.post("/logout")
    login(client, reviewer)
    html = client.get(f"/admin/contributions/{contribution_id}").get_data(as_text=True)
    version = re.search(r'name="version" value="([^"]+)"', html).group(1)

    client.post(f"/admin/contributions/{contribution_id}/approve",
                data={"version": version, "page_revision": 1, "review_reason": ""})

    after = get_page(app, page["id"])
    assert (after["title"], after["content"]) == ("Stable title", "new text")


# W2-8


def test_w2_8_editor_revert_does_not_make_a_builder_page_public_again(app, client, login, make_user, db, admin):
    set_feature(app, "page_history", True)
    page = make_page(app, "Landing", "v1")
    # An administrator published a public builder version, then withdrew it from public view.
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"], content="public v",
                                       builder_json='{"v":1}', builder_public=True))
    public_entry = db.scalar("SELECT MAX(id) FROM page_history WHERE page_id = ?", (page["id"],))
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"], content="internal v",
                                       builder_json='{"v":2}', builder_public=False))
    eddie = make_user("eddie", role="editor")
    restrict(db, eddie, keys=permissions.defaults("editor") | {"history.revert"})
    login(client, eddie)

    assert client.post(f"/page/{page['slug']}/revert/{public_entry}").status_code == 302

    after = get_page(app, page["id"])
    assert after["content"] == "public v" and after["builder_public"] == 0
    # An administrator restoring it brings the public flag back.
    client.post("/logout")
    login(client, admin)
    client.post(f"/page/{page['slug']}/revert/{public_entry}")
    assert get_page(app, page["id"])["builder_public"] == 1


# W2-9


def test_w2_9_moving_a_page_into_the_docs_category_keeps_the_grace_period(app, client, login, make_user, db):
    set_feature(app, "deletion_slowdown", True)
    docs = make_category(app, "Guide")
    db.execute("UPDATE site_settings SET docs_category_id = ?, docs_bypass_deletion_slowdown = 1", (docs["id"],))
    page = make_page(app, "Important", "keep me")
    eddie = make_user("eddie", role="editor")
    restrict(db, eddie, keys=permissions.defaults("editor") | {"page.delete"})
    login(client, eddie)
    assert client.post(f"/page/{page['slug']}/move", data={"category_id": docs["id"]}).status_code == 302

    client.post(f"/page/{page['slug']}/delete")

    after = get_page(app, page["id"])
    assert after is not None and after["pending_deletion"] == 1


# W2-10


def test_w2_10_administrator_cannot_lift_a_protection_by_making_the_page_home(app, client, login, make_user, db,
                                                                               admin):
    controller = make_user("controller", role="editor")
    page = make_page(app, "Guarded", "text")
    _protect(app, db, page, controller)
    login(client, admin)

    client.post(f"/page/{page['slug']}/set-home")
    assert client.post(f"/api/pages/{page['id']}/home").status_code == 423

    assert get_page(app, page["id"])["is_home"] == 0
    client.post(f"/page/{page['slug']}/edit", data={"content": "admin text", "revision": 1})
    assert get_page(app, page["id"])["content"] == "text"


# W2-11


def test_w2_11_one_editor_cannot_protect_every_page(app, client, login, make_user, db, admin, frozen_limiter_clock):
    set_feature(app, "page_governance", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1, default_reserved_pages_quota = 3")
    slugs = [make_page(app, f"Page {i}", "text")["slug"] for i in range(30)]
    eddie = make_user("eddie", role="editor")
    login(client, eddie)

    for slug in slugs:
        client.post(f"/page/{slug}/protection", data={"action": "protect"})

    assert db.scalar("SELECT COUNT(*) FROM pages WHERE protected_by = ?", (eddie["id"],)) == 3
    # The route is rate limited as well.
    db.execute("UPDATE users SET reserved_pages_quota = -1 WHERE id = ?", (eddie["id"],))
    assert client.post(f"/page/{slugs[-1]}/protection", data={"action": "protect"}).status_code == 429


# W2-12


def test_w2_12_my_contributions_hides_pages_the_proposer_can_no_longer_read(app, client, login, make_user, db):
    set_feature(app, "contributions", True)
    public, secret = make_category(app, "Public"), make_category(app, "Secret")
    page = make_page(app, "Some page", "text", category_id=public["id"])
    reader = make_user("reader")
    restrict(db, reader, read=[public["id"]])
    login(client, reader)
    client.post(f"/page/{page['slug']}/propose-edit", data={"title": "", "content": "new", "reason": "fix"})
    in_app(app, lambda: service.move(service.get(page["id"]), secret["id"], actor_id=None))
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=None, title="Layoffs Q3 list"))
    db.execute("UPDATE pages SET slug = 'layoffs-q3' WHERE id = ?", (page["id"],))

    html = client.get("/my-contributions").get_data(as_text=True)

    assert "Layoffs Q3 list" not in html and "layoffs-q3" not in html
    assert "Some page" in html  # the proposal's own title stays


# W2-13


def test_w2_13_blocked_editor_cannot_delete_read_aloud_audio(app, client, login, make_user, db):
    page = make_page(app, "Guide", "This is a page about bananas and it has enough words to be read.")
    controller = make_user("controller", role="editor")
    login(client, make_user("reader"))
    assert client.post(f"/page/{page['slug']}/tts/generate", json={}).status_code == 202
    client.post("/logout")
    _protect(app, db, page, controller)
    login(client, make_user("eddie", role="editor"))

    assert client.post(f"/page/{page['slug']}/tts/cancel", json={}).status_code == 423

    assert db.scalar("SELECT COUNT(*) FROM tts_generations WHERE page_id = ?", (page["id"],)) == 1


# W4-2

_JSON = {"Accept": "application/json"}


def _private_group(client, login, make_user, name="Private"):
    alice = make_user(f"alice_{name.lower()}")
    make_user(f"bob_{name.lower()}")
    login(client, alice)
    group_id = client.post("/groups/new", data={"name": name}, headers=_JSON).json["group_id"]
    client.post(f"/groups/{group_id}/members/add", data={"username": f"bob_{name.lower()}"}, headers=_JSON)
    message_id = client.post(f"/groups/{group_id}/send", data={"content": "secret evidence"},
                             headers=_JSON).json["message_id"]
    client.post("/logout")
    return group_id, message_id


def test_w4_2_administrators_moderating_groups_they_are_not_in_are_audited(client, login, make_user, db):
    first, message_id = _private_group(client, login, make_user, "First")
    second, _ = _private_group(client, login, make_user, "Second")
    login(client, make_user("plainadmin", role="admin"))

    assert client.post(f"/groups/{first}/delete_message", data={"message_id": message_id},
                       headers=_JSON).status_code == 200
    assert client.post(f"/groups/{first}/clear", headers=_JSON).status_code == 200
    assert client.post(f"/groups/{first}/delete", headers=_JSON).status_code == 200
    assert client.post(f"/admin/groups/{second}/delete", headers=_JSON).status_code == 200

    entries = [(row["action"], row["target_id"]) for row in db.all(
        "SELECT action, target_id FROM audit_log WHERE action LIKE 'chat.group_%' ORDER BY id")]
    assert entries == [("chat.group_message_deleted", str(first)), ("chat.group_cleared", str(first)),
                       ("chat.group_deleted", str(first)), ("chat.group_deleted", str(second))]


# W4-3


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_w4_3_canvas_image_uploads_count_against_the_daily_upload_quota(app, client, login, make_user, db):
    from bananawiki.wiki.features.canvas import service as canvases

    owner = make_user("owner", role="editor")
    canvas = in_app(app, lambda: canvases.create("Board", "", owner["id"]))
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 2")
    login(client, owner)

    statuses = [client.post(f"/canvas/{canvas['slug']}/upload", data={"file": (io.BytesIO(_png()), "pic.png")},
                            content_type="multipart/form-data").status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    assert db.scalar("SELECT upload_window_count FROM users WHERE id = ?", (owner["id"],)) == 2


# W4-4


def test_w4_4_kanban_sharing_dialog_does_not_list_every_account_and_role(app, client, login, make_user, db):
    from bananawiki.wiki.features.kanban import service as kanban

    owner = make_user("boardowner", role="editor")
    make_user("someadmin", role="admin")
    quiet = make_user("quietuser")
    board = in_app(app, lambda: kanban.create_board(owner, "Plans"))
    login(client, owner)

    with_search = client.get(f"/kanban/{board['id']}").get_data(as_text=True)
    assert "quietuser" in with_search and "(Administrators)" not in with_search  # no role next to the names
    restrict(db, owner, keys=permissions.defaults("editor") - {"search.users"})
    without_search = client.get(f"/kanban/{board['id']}").get_data(as_text=True)
    assert "quietuser" not in without_search and 'name="username"' in without_search

    # A typed user name still shares the board.
    client.post(f"/kanban/{board['id']}/share", data={"action": "add_user", "username": "quietuser",
                                                      "access_level": "view"})
    assert db.column("SELECT target FROM kanban_board_shares WHERE board_id = ?", (board["id"],)) == [quiet["id"]]


# W4-6


def test_w4_6_reading_heartbeat_refuses_odd_bodies_and_follows_the_clock(app, client, login, make_user, db,
                                                                         monkeypatch):
    from types import SimpleNamespace

    from bananawiki.core import ratelimit

    clock = [1_000.0]
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    set_feature(app, "badges", True)
    login(client, make_user("reader"))

    assert client.post("/badges/reading", data=json.dumps([1]), content_type="application/json").status_code == 400
    clock[0] += 60
    # No reading-time badge: nothing is counted.
    assert client.post("/badges/reading", json={"seconds": 120}).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM badge_reading_time") == 0

    db.execute("INSERT INTO badge_types (name, description, icon, color, auto_trigger, trigger_type, "
               "trigger_threshold, enabled) VALUES ('Reader', '', '', '#123456', 1, 'reading_time', 1, 1)")
    for _ in range(3):  # one report per allowed window, each claiming the maximum
        clock[0] += 60
        db.execute("UPDATE badge_reading_time SET updated_at = datetime(updated_at, '-60 seconds')")
        assert client.post("/badges/reading", json={"seconds": 10**9}).status_code == 200
    # 120 s for the first report, then never more than the 60 s that passed in between.
    assert db.scalar("SELECT seconds FROM badge_reading_time") == 120 + 60 + 60
