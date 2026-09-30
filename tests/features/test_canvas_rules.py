"""Canvas: registry events, server-side locks, connector sides and the export/guide front-end hooks."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import model, presets, service
from bananawiki.wiki.features.pages import service as pages

from .api_support import call, enable_api, issue

STATIC = Path(service.__file__).parent / "static"


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def owner(make_user):
    return make_user("owner1", role="editor", api_access_enabled=1)


@pytest.fixture
def canvas(app, owner):
    return run(app, lambda: service.create("Board", "About the board", owner["id"]))


@pytest.fixture
def editor(client, login, owner):
    login(client, owner)
    return client


@pytest.fixture
def events(app):
    """Every canvas event the registry delivers, as (name, canvas id, slug, actor_id)."""
    seen: list[tuple[str, int, str, str | None]] = []
    handlers = app.extensions["bananawiki.registry"]._handlers
    added = []
    for name in ("canvas.created", "canvas.updated", "canvas.deleted"):
        def record(canvas, actor_id, _name=name):
            seen.append((_name, canvas["id"], canvas["slug"], actor_id))

        handlers.setdefault(name, []).append(("canvas", record))
        added.append((name, ("canvas", record)))
    yield seen
    for name, entry in added:
        handlers[name].remove(entry)


@pytest.fixture
def creators(db):
    db.execute("UPDATE site_settings SET canvas_access = 'editor', canvas_write_access = 'editor' WHERE id = 1")


def ops(client, canvas, *items):
    return client.post(f"/canvas/{canvas['slug']}/ops", json={"ops": list(items)},
                       headers={"X-Canvas-Session": "s1"})


def stored(db, canvas):
    return json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (canvas["id"],)))


def node(node_id, **fields):
    return {"id": node_id, "type": "text", "x": 0, "y": 0, "content": node_id, **fields}


# ── Events ────────────────────────────────────────────────────────────────────


def test_web_actions_emit_created_updated_and_deleted(app, editor, owner, events, db, make_user, creators):
    editor.post("/canvas/create", data={"title": "Plan", "template": "flowchart"})
    layout_id = db.scalar("SELECT id FROM canvas__layouts WHERE slug = 'plan'")
    assert events == [("canvas.created", layout_id, "plan", owner["id"])]
    layout = {"id": layout_id, "slug": "plan"}
    # One event per batch of operations, however many operations it holds.
    ops(editor, layout, {"op": "upsert_node", "node": node("a")}, {"op": "upsert_node", "node": node("b")})
    # A batch that changes nothing emits nothing.
    ops(editor, layout, {"op": "delete_node", "id": "missing"})
    editor.post("/canvas/plan/edit", data={"title": "Plan B", "description": ""})
    editor.post("/canvas/plan/share", data={"action": "set_visibility", "visibility": "public"})
    friend = make_user("friend1", role="editor")
    editor.post("/canvas/plan/share", data={"action": "add_user", "username": "friend1", "permission": "edit"})
    editor.post("/canvas/plan/share", data={"action": "remove_user", "user_id": friend["id"]})
    entry = db.scalar("SELECT MIN(id) FROM canvas__history WHERE layout_id = ?", (layout_id,))
    editor.post(f"/canvas/plan/revert/{entry}")
    editor.post("/canvas/plan/delete")
    names = [event[0] for event in events]
    assert names == ["canvas.created"] + ["canvas.updated"] * 6 + ["canvas.deleted"]
    assert all(event[1] == layout_id and event[3] == owner["id"] for event in events)
    assert events[-1][2] == "plan"


def test_import_emits_created(editor, canvas, events, db, creators, owner):
    export = editor.get(f"/canvas/{canvas['slug']}/export?format=json")
    response = editor.post("/canvas/import", data={"import_file": (BytesIO(export.data), "b.canvas.json")},
                           content_type="multipart/form-data")
    assert response.status_code == 302
    imported = db.scalar("SELECT id FROM canvas__layouts WHERE id != ?", (canvas["id"],))
    assert [(event[0], event[1], event[3]) for event in events] == [("canvas.created", imported, owner["id"])]


def test_rest_api_paths_emit_the_same_events(app, db, client, owner, events, creators):
    enable_api(app, db)
    token = issue(app, owner, ["canvas"])
    created = call(client, "POST", "/canvas", token, json={"title": "Api board"})
    slug = created.json["canvas"]["slug"]
    call(client, "POST", f"/canvas/{slug}/ops", token, json={"ops": [{"op": "upsert_node", "node": node("a")}]})
    call(client, "PUT", f"/canvas/{slug}/document", token,
         json={"data": {"nodes": [node("a"), node("b")], "edges": []}})
    call(client, "PUT", f"/canvas/{slug}", token, json={"visibility": "public"})
    call(client, "DELETE", f"/canvas/{slug}", token)
    assert [event[0] for event in events] == ["canvas.created"] + ["canvas.updated"] * 3 + ["canvas.deleted"]
    assert {event[3] for event in events} == {owner["id"]}


def test_page_link_updates_emit_without_a_request_user(app, canvas, owner, events):
    page = run(app, lambda: pages.create("Linked", "text", author_id=None))
    run(app, lambda: service.apply(canvas, [{"op": "upsert_node", "node": {"id": "w", "type": "wiki_page",
                                                                           "page_id": page["id"]}}],
                                   user_id=owner["id"], session_id=""))
    events.clear()
    run(app, lambda: service.on_page_deleted(page))
    assert events == [("canvas.updated", canvas["id"], canvas["slug"], None)]


def test_failing_handlers_never_break_a_save(app, editor, canvas, db):
    def broken(**_):
        raise RuntimeError("boom")

    handlers = app.extensions["bananawiki.registry"]._handlers
    handlers.setdefault("canvas.updated", []).append(("canvas", broken))
    try:
        assert ops(editor, canvas, {"op": "upsert_node", "node": node("a")}).status_code == 200
    finally:
        handlers["canvas.updated"].remove(("canvas", broken))
    assert [n["id"] for n in stored(db, canvas)["nodes"]] == ["a"]


# ── Locks ─────────────────────────────────────────────────────────────────────


def test_lock_violation_rules():
    locked = model.clean_node(node("a", locked=True, x=10))
    assert not model.lock_violation(None, locked)
    assert not model.lock_violation(model.clean_node(node("a")), model.clean_node(node("a", x=99)))
    assert model.lock_violation(locked, None)
    assert model.lock_violation(locked, model.clean_node(node("a", locked=True, x=11)))
    # Unlocking and moving in one operation counts as moving.
    assert model.lock_violation(locked, model.clean_node(node("a", x=11)))
    for allowed in (node("a", x=10), node("a", x=10, locked=True, layer=7), node("a", x=10, locked=True, group="g")):
        assert not model.lock_violation(locked, model.clean_node(allowed))
    # Wiki-page titles and slugs come from the page (and are hidden from some readers): not an edit.
    page = model.clean_node({"id": "w", "type": "wiki_page", "page_id": 3, "label": "Title", "page_slug": "t",
                             "locked": True})
    assert not model.lock_violation(page, model.clean_node({"id": "w", "type": "wiki_page", "page_id": 3,
                                                            "locked": True, "restricted": True}))


def test_editor_ops_skip_changes_to_locked_nodes_and_say_why(editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True)},
        {"op": "upsert_node", "node": node("b")},
        {"op": "upsert_edge", "edge": {"id": "e", "from": "a", "to": "b"}})
    response = ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True, x=50)},
                   {"op": "delete_node", "id": "a"},
                   {"op": "upsert_node", "node": node("b", x=70)})
    body = response.get_json()
    assert response.status_code == 200
    assert body["rejected"] == [{"op": "upsert_node", "id": "a", "reason": "locked"},
                                {"op": "delete_node", "id": "a", "reason": "locked"}]
    assert "unlock them first" in body["error"] and "a" in body["error"]
    assert [op["node"]["id"] for op in body["applied"]] == ["b"]
    doc = stored(db, canvas)
    assert {n["id"]: n["x"] for n in doc["nodes"]} == {"a": 0, "b": 70} and len(doc["edges"]) == 1


def test_unlocking_first_in_the_same_batch_allows_the_change(editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True)})
    response = ops(editor, canvas, {"op": "upsert_node", "node": node("a")},
                   {"op": "upsert_node", "node": node("a", x=40)})
    assert response.get_json().get("rejected") is None
    assert stored(db, canvas)["nodes"][0]["x"] == 40
    ops(editor, canvas, {"op": "upsert_node", "node": node("a", x=40, locked=True)})
    assert ops(editor, canvas, {"op": "upsert_node", "node": node("a", x=40)},
               {"op": "delete_node", "id": "a"}).status_code == 200
    assert stored(db, canvas)["nodes"] == []


def test_lock_toggling_and_restacking_stay_allowed(editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a")})
    assert ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True)}).status_code == 200
    body = ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True, layer=5, group="g1")}).get_json()
    assert "rejected" not in body
    assert stored(db, canvas)["nodes"][0] == model.clean_node(node("a", locked=True, layer=5, group="g1"))


def test_service_refuses_the_whole_batch_by_default(app, canvas, owner, db):
    run(app, lambda: service.apply(canvas, [{"op": "upsert_node", "node": node("a", locked=True)}],
                                   user_id=owner["id"], session_id=""))
    with pytest.raises(service.CanvasError) as refused:
        run(app, lambda: service.apply(canvas, [{"op": "upsert_node", "node": node("b")},
                                                {"op": "delete_node", "id": "a"}],
                                       user_id=owner["id"], session_id=""))
    assert refused.value.key == "canvas.error.locked" and refused.value.values["ids"] == "a"
    assert [n["id"] for n in stored(db, canvas)["nodes"]] == ["a"]


def test_whole_document_saves_respect_locks(app, editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a", locked=True)})
    slug = canvas["slug"]
    moved = editor.post(f"/canvas/{slug}/data", json={"data": {"nodes": [node("a", locked=True, x=5)], "edges": []}})
    assert moved.status_code == 400 and "unlock" in moved.get_json()["error"]
    dropped = editor.post(f"/canvas/{slug}/data", json={"data": {"nodes": [], "edges": []}})
    assert dropped.status_code == 400
    kept = editor.post(f"/canvas/{slug}/data",
                       json={"data": {"nodes": [node("a", locked=True), node("b")], "edges": []}})
    assert kept.status_code == 200 and len(stored(db, canvas)["nodes"]) == 2


def test_rest_api_reports_locked_elements(app, db, client, owner, canvas, creators):
    enable_api(app, db)
    token = issue(app, owner, ["canvas"])
    slug = canvas["slug"]
    call(client, "POST", f"/canvas/{slug}/ops", token,
         json={"ops": [{"op": "upsert_node", "node": node("a", locked=True)}]})
    refused = call(client, "POST", f"/canvas/{slug}/ops", token, json={"ops": [{"op": "delete_node", "id": "a"}]})
    assert refused.status_code == 400 and refused.json["code"] == "locked"
    replaced = call(client, "PUT", f"/canvas/{slug}/document", token, json={"data": {"nodes": [], "edges": []}})
    assert replaced.status_code == 400 and replaced.json["code"] == "locked"
    assert [n["id"] for n in stored(db, canvas)["nodes"]] == ["a"]


def test_history_restore_is_not_blocked_by_locks(app, editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a")})
    first = db.scalar("SELECT MAX(id) FROM canvas__history WHERE layout_id = ?", (canvas["id"],))
    db.execute("UPDATE canvas__history SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (first,))
    ops(editor, canvas, {"op": "upsert_node", "node": node("a", x=30, locked=True)})
    assert editor.post(f"/canvas/{canvas['slug']}/revert/{first}").status_code == 302
    assert stored(db, canvas)["nodes"][0]["x"] == 0


def test_viewers_cannot_touch_locks(app, canvas, make_user, login, client, db):
    viewer = make_user("viewer1", role="editor")
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (canvas["id"], viewer["id"]))
    login(client, viewer)
    assert ops(client, canvas, {"op": "upsert_node", "node": node("a", locked=True)}).status_code == 403


# ── Connector sides ───────────────────────────────────────────────────────────


def test_edge_sides_are_whitelisted_and_default_to_auto():
    doc = model.clean_document({
        "nodes": [node("a"), node("b")],
        "edges": [{"id": "e1", "from": "a", "to": "b", "from_side": "top", "to_side": "left"},
                  {"id": "e2", "from": "b", "to": "a", "from_side": "auto", "to_side": "<script>"},
                  {"id": "e3", "from": "a", "to": "b", "from_side": ["top"], "to_side": 3}],
    })
    sides = {edge["id"]: (edge.get("from_side"), edge.get("to_side")) for edge in doc["edges"]}
    assert sides == {"e1": ("top", "left"), "e2": (None, None), "e3": (None, None)}


def test_edge_sides_survive_ops_and_sync(editor, canvas, db):
    ops(editor, canvas, {"op": "upsert_node", "node": node("a")}, {"op": "upsert_node", "node": node("b")},
        {"op": "upsert_edge", "edge": {"id": "e", "from": "a", "to": "b", "from_side": "bottom",
                                       "to_side": "right", "route": "elbow"}})
    assert stored(db, canvas)["edges"][0]["from_side"] == "bottom"
    events = editor.get(f"/canvas/{canvas['slug']}/sync?since=0", headers={"X-Canvas-Session": "s2"}).get_json()
    assert events["events"][-1]["payload"]["edge"]["to_side"] == "right"


def test_flowchart_loop_back_uses_the_sides(app):
    doc = run(app, lambda: presets.document("flowchart"))
    loop = next(edge for edge in doc["edges"] if edge["from"] == "io" and edge["to"] == "step")
    assert (loop["from_side"], loop["to_side"], loop["route"]) == ("top", "right", "elbow")
    main = [edge for edge in doc["edges"] if edge is not loop]
    assert all("from_side" not in edge and "to_side" not in edge for edge in main)


def test_edge_dialog_offers_side_choices(editor, canvas):
    html = editor.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    for marker in ('name="from_side"', 'name="to_side"', 'value="auto"', 'value="left"', "guide lines"):
        assert marker in html, marker


# ── Front-end hooks (the JavaScript itself is checked with node --check) ─────


def test_front_end_implements_guides_sides_and_export_placeholders():
    core = (STATIC / "canvas-core.js").read_text()
    editor_js = (STATIC / "canvas-editor.js").read_text()
    export = (STATIC / "canvas-export.js").read_text()
    assert "from_side" in core and "elbowPath" in core
    assert "function Guides" in editor_js and "lowerBound" in editor_js and "event.altKey" in editor_js
    assert "res.rejected" in editor_js and "from_side" in editor_js
    assert 'crossOrigin = "anonymous"' in export and "placeholder" in export
    # No request to arbitrary URLs through this wiki: only uploads are fetched from our own origin.
    assert "fetch(url" in export and "/static\\/uploads\\//.test(url)" in export
    for text in (core, editor_js, export):
        assert "eval(" not in text and "innerHTML = " not in text.replace("body.innerHTML = rendering.html", "")


def test_new_strings_exist_in_both_languages():
    folder = STATIC.parent / "translations"
    english = json.loads((folder / "en.json").read_text())
    italian = json.loads((folder / "it.json").read_text())
    for key in ("canvas.error.locked", "canvas.edge.from_side", "canvas.edge.side_left", "canvas.keys.guides",
                "js.canvas.export_placeholder", "js.canvas.status.exported_placeholders"):
        assert english[key] and italian[key] and english[key] != italian[key]
