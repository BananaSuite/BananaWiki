"""Regression tests for the m3 findings of the full re-audit pass 7 (canvas and kanban)."""

from __future__ import annotations

import logging

import pytest

# MW1-1: anonymous JSON requests to canvas views answer 401, not 500


@pytest.mark.parametrize(("url", "headers"), [
    ("/api/embed/canvas/nope", {}),
    ("/api/embed/canvas/nope/sync", {}),
    ("/canvas/nope", {"Accept": "application/json"}),
    ("/canvas/nope/data", {"Accept": "application/json"}),
    ("/canvas/nope/outline", {"Accept": "application/json"}),
    ("/canvas/nope/history", {"Accept": "application/json"}),
])
def test_mw1_1_anonymous_json_canvas_request_answers_401(app, db, caplog, url, headers):
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    with caplog.at_level(logging.ERROR):
        response = app.test_client().get(url, headers=headers)
    assert response.status_code == 401
    assert "error" in response.get_json()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_mw1_1_anonymous_html_canvas_request_still_redirects_to_login(app, db):
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    response = app.test_client().get("/canvas/nope")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# MW3-2: a canvas import counts its images against the daily upload quota


def _png(seed: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (seed % 256, 10, 20)).save(buffer, "PNG")
    return buffer.getvalue()


def _canvas_bundle(count: int) -> bytes:
    import io
    import json
    import zipfile

    nodes = [{"id": f"n{i}", "type": "image", "x": i * 10, "y": 0, "width": 100, "height": 100,
              "url": f"/static/uploads/img{i}.png"} for i in range(count)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        bundle.writestr("canvas.json", json.dumps({"title": "Imported", "data": {"nodes": nodes, "edges": []}}))
        for i in range(count):
            bundle.writestr(f"assets/img{i}.png", _png(i))
    return out.getvalue()


def _upload_files(app) -> list[str]:
    from pathlib import Path

    folder = Path(app.config["BW"].folders.uploads)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))


def _import(client, count: int):
    import io

    return client.post("/canvas/import", data={"import_file": (io.BytesIO(_canvas_bundle(count)), "x.canvas.zip")},
                       content_type="multipart/form-data")


def test_mw3_2_canvas_import_counts_its_images_against_the_daily_quota(app, client, make_user, login, db):
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 5, canvas_write_access = 'editor' WHERE id = 1")
    editor = make_user("eddie", role="editor")
    login(client, editor)
    before = _upload_files(app)

    # Three images fit and are counted.
    assert _import(client, 3).status_code == 302
    assert db.scalar("SELECT upload_window_count FROM users WHERE id = ?", (editor["id"],)) == 3
    assert len(_upload_files(app)) == len(before) + 3
    layouts = db.scalar("SELECT COUNT(*) FROM canvas__layouts")

    # Three more do not: the import is refused whole, stores nothing and counts nothing.
    _import(client, 3)
    assert db.scalar("SELECT upload_window_count FROM users WHERE id = ?", (editor["id"],)) == 3
    assert len(_upload_files(app)) == len(before) + 3
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == layouts


def test_mw3_2_canvas_import_obeys_the_daily_byte_quota(app, client, make_user, login, db):
    db.execute("UPDATE site_settings SET upload_quota_per_day_bytes = 1, canvas_write_access = 'editor' WHERE id = 1")
    editor = make_user("eddie", role="editor")
    login(client, editor)
    before = _upload_files(app)
    _import(client, 2)
    assert _upload_files(app) == before
    assert not db.scalar("SELECT upload_window_bytes FROM users WHERE id = ?", (editor["id"],))


def test_mw3_2_an_import_refused_when_the_canvas_is_created_costs_no_quota(app, client, make_user, login, db,
                                                                          monkeypatch):
    from bananawiki.wiki.features.canvas import model

    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 5, canvas_write_access = 'editor' WHERE id = 1")
    editor = make_user("eddie", role="editor")
    login(client, editor)
    before = _upload_files(app)
    # The document fits as exported; the longer names the stored images get push it over the limit only when
    # the canvas is created, after its images were stored and counted.
    nodes = [{"id": f"n{i}", "type": "image", "x": i * 10, "y": 0, "width": 100, "height": 100,
              "url": f"/static/uploads/img{i}.png"} for i in range(3)]  # as _canvas_bundle(3) builds them
    with app.app_context():
        size = len(model.serialize(model.clean_document({"nodes": nodes, "edges": []}, strict=True)).encode())
    monkeypatch.setattr(model, "MAX_DOCUMENT_BYTES", size + 20)
    assert _import(client, 3).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 0
    assert _upload_files(app) == before
    row = db.one("SELECT upload_window_count, upload_window_bytes FROM users WHERE id = ?", (editor["id"],))
    assert not row["upload_window_count"] and not row["upload_window_bytes"]


# MW3-3: board creation is rate limited and the board list costs one page of boards per view


def test_mw3_3_board_creation_is_rate_limited(app, client, make_user, login, db, frozen_limiter_clock):
    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    login(client, make_user("eddie", role="editor"))
    codes = [client.post("/kanban/create", data={"title": f"B{n}"}).status_code for n in range(12)]
    assert codes[:10] == [302] * 10
    assert codes[10:] == [429, 429]
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 10


def test_mw3_3_rest_board_creation_shares_the_per_account_limit(app, client, make_user, login, db,
                                                                frozen_limiter_clock):
    from tests.features.api_support import call, enable_api, issue

    db.execute("UPDATE site_settings SET kanban_access = 'all', kanban_write_access = 'editor' WHERE id = 1")
    enable_api(app, db)
    eddie = make_user("eddie", role="editor")
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (eddie["id"],))
    token = issue(app, db.one("SELECT id FROM users WHERE id = ?", (eddie["id"],)), scopes=("kanban",))
    api = app.test_client()
    codes = [call(api, "POST", "/kanban/boards", token, json={"title": f"B{n}"}).status_code for n in range(8)]
    assert codes == [201] * 8
    login(client, eddie)
    web = [client.post("/kanban/create", data={"title": f"W{n}"}).status_code for n in range(3)]
    assert web == [302, 302, 429]
    refused = call(api, "POST", "/kanban/boards", token, json={"title": "More"})
    assert refused.status_code == 429
    assert refused.get_json()["code"] == "rate_limited"
    assert refused.headers["Retry-After"] == "60"
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 10


def test_mw3_3_board_order_is_rate_limited(app, admin_client, frozen_limiter_clock):
    codes = [admin_client.post("/api/kanban/board-order", json={"board_ids": []}).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def _many_boards(app, owner, count, description=""):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.kanban import service

    with app.test_request_context(), connection_scope():
        return [service.create_board(owner, f"Board {n}", description, default_columns=False)["id"]
                for n in range(count)]


def test_mw3_3_board_list_is_paged_and_excerpts_are_kept(app, admin_client, admin, db, monkeypatch):
    from bananawiki.wiki import markdown
    from bananawiki.wiki.features.kanban import views

    monkeypatch.setattr(views, "LIST_PAGE_SIZE", 5)
    ids = _many_boards(app, admin, 12, "| a | b |\n|---|---|\n| 1 | 2 |")
    db.execute("UPDATE kanban_boards SET archived_at = '2026-01-01 00:00:00' WHERE id IN (?, ?)", (ids[0], ids[1]))
    markdown.clear_render_cache()
    rendered = []
    real = markdown._excerpt
    monkeypatch.setattr(markdown, "_excerpt", lambda text, length: rendered.append(text) or real(text, length))

    first = admin_client.get("/kanban").get_data(as_text=True)
    assert first.count('class="kanban-boards__item"') == 5
    assert 'data-move="-1"' in first and "page=2" in first
    # Every board's description is the same: rendered once, then kept.
    assert len(rendered) == 1
    admin_client.get("/kanban")
    assert len(rendered) == 1

    last = admin_client.get("/kanban?page=2").get_data(as_text=True)
    assert last.count('class="kanban-boards__item"') == 5
    assert 'data-move="-1"' not in last  # only the first page can be reordered
    assert admin_client.get("/kanban?page=99").get_data(as_text=True).count('class="kanban-boards__item"') == 5
    assert admin_client.get("/kanban?page=x").status_code == 200

    archived = admin_client.get("/kanban?archived=1").get_data(as_text=True)
    assert "Show archived boards (2)" in first
    assert archived.count("kanban-boards__item--archived") == 2


def test_mw3_3_the_archived_list_is_capped(app, admin_client, admin, db, monkeypatch):
    from bananawiki.wiki.features.kanban import views

    monkeypatch.setattr(views, "LIST_PAGE_SIZE", 3)
    _many_boards(app, admin, 5)
    db.execute("UPDATE kanban_boards SET archived_at = '2026-01-01 00:00:00'")
    page = admin_client.get("/kanban?archived=1").get_data(as_text=True)
    assert page.count("kanban-boards__item--archived") == 3
    assert "The first 3 of 5 are listed." in page


# MW3-4: an operation on a large canvas does not rewrite its whole history entry every time


def _canvas_with_editor(app, client, make_user, login, db):
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    login(client, make_user("owner1", role="editor"))
    assert client.post("/canvas/create", data={"title": "Big"}).status_code == 302
    return db.one("SELECT id, slug FROM canvas__layouts WHERE title = 'Big'")


def _node(i, x=0):
    return {"id": f"n{i}", "type": "text", "x": x, "y": i, "width": 200, "height": 100, "content": "word " * 100}


def _ops(client, slug, *nodes):
    return client.post(f"/canvas/{slug}/ops", json={"ops": [{"op": "upsert_node", "node": n} for n in nodes]})


def _latest_history(db, layout_id):
    return db.one("SELECT id, data FROM canvas__history WHERE layout_id = ? ORDER BY id DESC LIMIT 1", (layout_id,))


def test_mw3_4_large_canvas_history_is_rewritten_at_most_once_a_minute(app, client, make_user, login, db,
                                                                       monkeypatch, frozen_limiter_clock):
    import json

    from bananawiki.wiki.features.canvas import service

    monkeypatch.setattr(service, "LARGE_DOCUMENT_BYTES", 2000)
    canvas = _canvas_with_editor(app, client, make_user, login, db)
    created = _latest_history(db, canvas["id"])["id"]
    assert _ops(client, canvas["slug"], *(_node(i) for i in range(10))).status_code == 200
    first = _latest_history(db, canvas["id"])
    assert first["id"] != created and '"x":0' in first["data"]

    for x in (1, 2, 3):
        assert _ops(client, canvas["slug"], _node(0, x)).status_code == 200
    latest = _latest_history(db, canvas["id"])
    assert latest == first  # the layout changed three times, the history entry was not copied again
    assert json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (canvas["id"],)))["nodes"][0]["x"] == 3

    # A restore first writes what the entry had not recorded yet, so nothing is lost.
    assert client.post(f"/canvas/{canvas['slug']}/revert/{created}").status_code == 302
    flushed = db.scalar("SELECT data FROM canvas__history WHERE id = ?", (first["id"],))
    assert json.loads(flushed)["nodes"][0]["x"] == 3


def test_mw3_4_small_canvas_history_still_follows_every_edit(app, client, make_user, login, db,
                                                             frozen_limiter_clock):
    import json

    canvas = _canvas_with_editor(app, client, make_user, login, db)
    for x in (1, 2):
        assert _ops(client, canvas["slug"], _node(0, x)).status_code == 200
        assert json.loads(_latest_history(db, canvas["id"])["data"])["nodes"][0]["x"] == x


def test_mw3_4_large_canvas_ops_and_saves_have_a_lower_limit(app, client, make_user, login, db, monkeypatch,
                                                            frozen_limiter_clock):
    from bananawiki.wiki.features.canvas import service

    monkeypatch.setattr(service, "LARGE_DOCUMENT_BYTES", 2000)
    monkeypatch.setattr(service, "LARGE_OPS_PER_MINUTE", 3)
    monkeypatch.setattr(service, "LARGE_SAVES_PER_MINUTE", 1)
    canvas = _canvas_with_editor(app, client, make_user, login, db)
    assert _ops(client, canvas["slug"], *(_node(i) for i in range(10))).status_code == 200  # small until now
    codes = [_ops(client, canvas["slug"], _node(0, x)).status_code for x in range(4)]
    assert codes == [200, 200, 200, 429]
    data = {"nodes": [_node(i) for i in range(10)], "edges": []}
    saves = [client.post(f"/canvas/{canvas['slug']}/data", json={"data": data}).status_code for _ in range(2)]
    assert saves == [200, 429]


def test_mw3_4_large_canvas_budget_is_shared_with_the_rest_api(app, client, make_user, login, db, monkeypatch,
                                                              frozen_limiter_clock):
    from bananawiki.wiki.features.canvas import service
    from tests.features.api_support import call, enable_api, issue

    monkeypatch.setattr(service, "LARGE_DOCUMENT_BYTES", 2000)
    monkeypatch.setattr(service, "LARGE_OPS_PER_MINUTE", 3)
    monkeypatch.setattr(service, "LARGE_SAVES_PER_MINUTE", 2)
    enable_api(app, db)
    canvas = _canvas_with_editor(app, client, make_user, login, db)
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE username = 'owner1'")
    token = issue(app, db.one("SELECT id FROM users WHERE username = 'owner1'"), scopes=("canvas",))
    api = app.test_client()
    assert _ops(client, canvas["slug"], *(_node(i) for i in range(10))).status_code == 200  # small until now
    path = f"/canvas/{canvas['slug']}"
    codes = [_ops(client, canvas["slug"], _node(0, 1)).status_code,
             call(api, "POST", path + "/ops", token, json={"ops": [{"op": "upsert_node", "node": _node(0, 2)}]})
             .status_code,
             _ops(client, canvas["slug"], _node(0, 3)).status_code]
    assert codes == [200, 200, 200]
    refused = call(api, "POST", path + "/ops", token, json={"ops": [{"op": "upsert_node", "node": _node(0, 4)}]})
    assert refused.status_code == 429 and refused.json["code"] == "rate_limited"
    assert refused.headers["Retry-After"] == "60"
    assert _ops(client, canvas["slug"], _node(0, 5)).status_code == 429
    data = {"nodes": [_node(i) for i in range(10)], "edges": []}
    assert client.post(f"{path}/data", json={"data": data}).status_code == 200
    assert call(api, "PUT", path + "/document", token, json={"data": data}).status_code == 200
    assert call(api, "PUT", path + "/document", token, json={"data": data}).status_code == 429
    assert client.post(f"{path}/data", json={"data": data}).status_code == 429


# MW3-5: deleting an account keeps the private canvases it shared with others


def test_mw3_5_private_canvas_shared_with_a_person_survives_its_creators_deletion(app, client, make_user, login,
                                                                                  db):
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    boss = make_user("boss", role="admin")
    alice = make_user("alice", role="editor")
    bob = make_user("bobby", role="user")
    login(client, alice)
    for title in ("Team plan", "Diary"):
        assert client.post("/canvas/create", data={"title": title}).status_code == 302
    plan = db.one("SELECT id, slug, visibility FROM canvas__layouts WHERE title = 'Team plan'")
    diary = db.scalar("SELECT id FROM canvas__layouts WHERE title = 'Diary'")
    assert plan["visibility"] == "private"
    assert client.post(f"/canvas/{plan['slug']}/share", data={"action": "add_user", "user_id": bob["id"],
                                                               "permission": "edit"}).status_code == 302
    # A share that grants nothing does not make the diary anyone else's.
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'none')",
               (diary, bob["id"]))
    other = app.test_client()
    other.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    login(other, bob)
    assert _ops(other, plan["slug"], _node(1)).status_code == 200

    from bananawiki.wiki import accounts
    from tests.features.pages_support import in_app

    in_app(app, lambda: accounts.delete(accounts.by_id(alice["id"]), deleted_by=boss["id"]))
    assert db.scalar("SELECT creator_id FROM canvas__layouts WHERE id = ?", (plan["id"],)) == boss["id"]
    assert other.get(f"/canvas/{plan['slug']}").status_code == 200
    assert '"n1"' in db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (plan["id"],))
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts WHERE id = ?", (diary,)) == 0


def test_mw3_5_private_canvas_shared_with_a_role_survives_self_deletion(app, client, make_user, login, db):
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    boss = make_user("boss", role="admin")
    alice = make_user("alice", role="editor")
    login(client, alice)
    assert client.post("/canvas/create", data={"title": "Team plan"}).status_code == 302
    plan = db.scalar("SELECT id FROM canvas__layouts WHERE title = 'Team plan'")
    db.execute("INSERT INTO canvas__permissions (layout_id, role, permission) VALUES (?, 'user', 'view')", (plan,))

    from bananawiki.wiki import accounts
    from tests.features.pages_support import in_app

    in_app(app, lambda: accounts.delete(accounts.by_id(alice["id"]), deleted_by=alice["id"]))
    assert db.scalar("SELECT creator_id FROM canvas__layouts WHERE id = ?", (plan,)) == boss["id"]


def test_mw3_5_portal_removal_keeps_canvases_others_use(app, client, make_user, login, db):
    from bananawiki.ops import tenant_task

    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    boss = make_user("boss", role="admin")
    alice = make_user("alice", role="editor")
    bob = make_user("bobby", role="user")
    login(client, alice)
    for title in ("Team plan", "Shared plan", "Diary"):
        assert client.post("/canvas/create", data={"title": title}).status_code == 302
    plan = db.one("SELECT id, slug FROM canvas__layouts WHERE title = 'Team plan'")
    shared = db.scalar("SELECT id FROM canvas__layouts WHERE title = 'Shared plan'")
    assert client.post(f"/canvas/{plan['slug']}/share", data={"action": "add_user", "user_id": bob["id"],
                                                               "permission": "edit"}).status_code == 302
    db.execute("UPDATE canvas__layouts SET visibility = 'shared' WHERE id = ?", (shared,))

    assert tenant_task.remove_user(app.config["BW"], {"username": "alice"}) == {}
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (alice["id"],)) == 0
    kept = {row["title"]: row["creator_id"] for row in db.all("SELECT title, creator_id FROM canvas__layouts")}
    assert kept == {"Team plan": boss["id"], "Shared plan": boss["id"]}
    assert db.scalar("SELECT COUNT(*) FROM canvas__permissions WHERE layout_id = ? AND user_id = ?",
                     (plan["id"], bob["id"])) == 1


@pytest.mark.parametrize("remover", ["wiki", "portal"])
def test_mw3_5_private_canvas_under_open_access_survives_its_creators_deletion(app, client, make_user, login, db,
                                                                              remover):
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor', "
               "canvas_open_access = 1 WHERE id = 1")
    boss = make_user("boss", role="admin")
    alice = make_user("alice", role="editor")
    bob = make_user("bobby", role="user")
    login(client, alice)
    for title in ("Team plan", "Old diary"):
        assert client.post("/canvas/create", data={"title": title}).status_code == 302
    plan = db.one("SELECT id, slug, visibility FROM canvas__layouts WHERE title = 'Team plan'")
    assert plan["visibility"] == "private"
    # An archived canvas is the creator's alone again, even under open access.
    db.execute("UPDATE canvas__layouts SET is_archived = 1 WHERE title = 'Old diary'")
    other = app.test_client()
    other.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    login(other, bob)
    assert _ops(other, plan["slug"], _node(1)).status_code == 200

    if remover == "wiki":
        from bananawiki.wiki import accounts
        from tests.features.pages_support import in_app

        in_app(app, lambda: accounts.delete(accounts.by_id(alice["id"]), deleted_by=boss["id"]))
    else:
        from bananawiki.ops import tenant_task

        assert tenant_task.remove_user(app.config["BW"], {"username": "alice"}) == {}
    kept = {row["title"]: row["creator_id"] for row in db.all("SELECT title, creator_id FROM canvas__layouts")}
    assert kept == {"Team plan": boss["id"]}
    assert other.get(f"/canvas/{plan['slug']}").status_code == 200


def test_mw3_4_changes_skipped_by_the_throttle_reach_history_before_a_new_entry(app, client, make_user, login, db,
                                                                                 monkeypatch):
    import json

    from bananawiki.wiki.features.canvas import service

    monkeypatch.setattr(service, "LARGE_DOCUMENT_BYTES", 2000)
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor', "
               "canvas_open_access = 1 WHERE id = 1")
    alice, bob = make_user("alice", role="editor"), make_user("bobby", role="editor")
    login(client, alice)
    assert client.post("/canvas/create", data={"title": "Big"}).status_code == 302
    canvas = db.one("SELECT id, slug FROM canvas__layouts WHERE title = 'Big'")
    assert _ops(client, canvas["slug"], *(_node(i) for i in range(10))).status_code == 200
    assert _ops(client, canvas["slug"], _node(0, 1)).status_code == 200  # within the minute: not written yet
    assert _ops(client, canvas["slug"], _node(0, 2)).status_code == 200  # alice's final state
    # A day later bob deletes every node: a new entry, from the changed canvas.
    db.execute("UPDATE canvas__history SET created_at = datetime('now', '-1 day') WHERE layout_id = ?", (canvas["id"],))
    app.extensions["bananawiki.limiter"].reset()
    other = app.test_client()
    login(other, bob)
    assert other.post(f"/canvas/{canvas['slug']}/ops",
                      json={"ops": [{"op": "delete_node", "id": f"n{i}"} for i in range(10)]}).status_code == 200
    entries = db.all("SELECT edited_by, data FROM canvas__history WHERE layout_id = ? ORDER BY id", (canvas["id"],))
    alices = [json.loads(entry["data"]) for entry in entries if entry["edited_by"] == alice["id"]]
    # Alice's last state is in her entry, so restoring after bob's change gets it back.
    assert any(node["id"] == "n0" and node["x"] == 2 for node in alices[-1]["nodes"])
