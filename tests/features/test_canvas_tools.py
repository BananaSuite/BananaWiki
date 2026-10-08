"""Canvas: groups, locks, edge paths, new shapes, templates and the text outline."""

from __future__ import annotations

import json

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import model, presets, service
from bananawiki.wiki.features.pages import categories
from bananawiki.wiki.features.pages import service as pages


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def owner(make_user):
    return make_user("owner1", role="editor")


@pytest.fixture
def canvas(app, owner):
    return run(app, lambda: service.create("Board", "About the board", owner["id"]))


@pytest.fixture
def editor(client, login, owner):
    login(client, owner)
    return client


def ops(client, canvas, *items):
    return client.post(f"/canvas/{canvas['slug']}/ops", json={"ops": list(items)},
                       headers={"X-Canvas-Session": "s1"})


def stored(db, canvas):
    return json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (canvas["id"],)))


def apply(app, canvas, owner, *items):
    return run(app, lambda: service.apply(canvas, list(items), user_id=owner["id"], session_id=""))


# ── Model ─────────────────────────────────────────────────────────────────────


def test_groups_locks_routes_and_shapes_are_whitelisted():
    doc = model.clean_document({
        "nodes": [
            {"id": "a", "type": "text", "group": "g_1", "locked": True, "shape": "hexagon"},
            {"id": "b", "type": "text", "group": "bad group!", "locked": "yes", "shape": "parallelogram"},
            {"id": "c", "type": "text", "group": {"x": 1}, "locked": 1, "shape": "star"},
        ],
        "edges": [
            {"id": "e1", "from": "a", "to": "b", "route": "elbow"},
            {"id": "e2", "from": "b", "to": "c", "route": "curved"},
            {"id": "e3", "from": "a", "to": "c", "route": "<svg>"},
        ],
    })
    a, b, c = doc["nodes"]
    assert a["group"] == "g_1" and a["locked"] is True and a["shape"] == "hexagon"
    assert "group" not in b and "locked" not in b and b["shape"] == "parallelogram"
    assert "group" not in c and "locked" not in c and "shape" not in c
    routes = {edge["id"]: edge.get("route") for edge in doc["edges"]}
    assert routes == {"e1": "elbow", "e2": None, "e3": None}


def test_older_documents_without_the_new_fields_load_unchanged():
    legacy = {"nodes": [{"id": "n1", "type": "note", "x": 5, "y": 6, "shape": "diamond"}],
              "edges": [{"id": "e", "source": "n1", "target": "n1"}], "viewport": {"zoom": 2}}
    doc = model.clean_document(json.dumps(legacy))
    assert doc["nodes"][0]["type"] == "text" and doc["nodes"][0]["shape"] == "diamond"
    assert "group" not in doc["nodes"][0] and "locked" not in doc["nodes"][0] and doc["edges"] == []


def test_new_fields_survive_ops_and_reach_other_sessions(editor, canvas, db):
    response = ops(editor, canvas,
                   {"op": "upsert_node", "node": {"id": "a", "type": "text", "group": "g1", "locked": True}},
                   {"op": "upsert_node", "node": {"id": "b", "type": "text", "group": "g1"}},
                   {"op": "upsert_edge", "edge": {"id": "e", "from": "a", "to": "b", "route": "straight"}})
    assert response.status_code == 200
    doc = stored(db, canvas)
    assert doc["nodes"][0]["locked"] is True and {n["group"] for n in doc["nodes"]} == {"g1"}
    assert doc["edges"][0]["route"] == "straight"
    events = editor.get(f"/canvas/{canvas['slug']}/sync?since=0", headers={"X-Canvas-Session": "s2"}).get_json()
    payloads = [event["payload"] for event in events["events"]]
    assert payloads[0]["node"]["locked"] is True and payloads[2]["edge"]["route"] == "straight"


# ── Templates ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", presets.names())
def test_every_template_is_a_valid_document(app, name):
    doc = run(app, lambda: presets.document(name))
    assert doc["nodes"] and model.serialize(doc)
    ids = {node["id"] for node in doc["nodes"]}
    assert all(edge["from"] in ids and edge["to"] in ids for edge in doc["edges"])
    assert all(node.get("content") or node.get("display_text") for node in doc["nodes"])
    assert presets.document("nope") is None and presets.document(None) is None


def test_create_from_template(client, login, admin, db):
    login(client, admin)
    response = client.post("/canvas/create", data={"title": "Flow", "template": "flowchart"})
    assert response.status_code == 302
    row = db.one("SELECT * FROM canvas__layouts WHERE slug = 'flow'")
    doc = json.loads(row["data"])
    assert {node["shape"] for node in doc["nodes"]} >= {"pill", "diamond", "parallelogram"}
    assert any(edge.get("route") == "elbow" for edge in doc["edges"])
    assert db.scalar("SELECT COUNT(*) FROM canvas__history WHERE layout_id = ?", (row["id"],)) == 1
    client.post("/canvas/create", data={"title": "Retro", "template": "retrospective"})
    retro = json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE slug = 'retro'"))
    frames = [node for node in retro["nodes"] if node.get("locked")]
    assert len(frames) == 3 and frames[0]["display_text"] == "What went well"
    # An unknown template creates nothing; a blank choice creates an empty canvas.
    client.post("/canvas/create", data={"title": "Bad", "template": "../../etc"})
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts WHERE slug = 'bad'") == 0
    client.post("/canvas/create", data={"title": "Blank", "template": ""})
    assert json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE slug = 'blank'"))["nodes"] == []
    assert b'name="template"' in client.get("/canvas").data


def test_template_labels_follow_the_language(app):
    from flask import g

    with app.test_request_context(), connection_scope():
        g.lang = "it"
        doc = presets.document("swot")
    titles = {node.get("display_text") for node in doc["nodes"]}
    assert {"Punti di forza", "Minacce"} <= titles


def test_creating_from_a_template_needs_create_rights(client, login, make_user, db):
    login(client, make_user("plain1"))
    assert client.post("/canvas/create", data={"title": "X", "template": "swot"}).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 0


# ── Text outline ──────────────────────────────────────────────────────────────


def build_outline_canvas(app, canvas, owner):
    apply(app, canvas, owner,
          {"op": "upsert_node", "node": {"id": "late", "type": "text", "x": 0, "y": 400, "content": "Last *step*"}},
          {"op": "upsert_node", "node": {"id": "first", "type": "text", "x": 300, "y": 0, "content": "First",
                                         "display_text": "Kick-off", "group": "g1", "locked": True}},
          {"op": "upsert_node", "node": {"id": "left", "type": "text", "x": 0, "y": 20, "content": "Left <b>",
                                         "group": "g1"}},
          {"op": "upsert_node", "node": {"id": "link", "type": "external_link", "x": 0, "y": 800,
                                         "url": "javascript:alert(1)", "label": "Bad link"}},
          {"op": "upsert_edge", "edge": {"id": "e1", "from": "left", "to": "late", "label": "then"}},
          {"op": "upsert_edge", "edge": {"id": "e2", "from": "first", "to": "late", "arrow": "both"}})


def test_outline_lists_elements_in_reading_order_with_connections(app, editor, canvas, owner):
    build_outline_canvas(app, canvas, owner)
    response = editor.get(f"/canvas/{canvas['slug']}/outline")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    order = [html.index(f'id="outline-{node_id}"') for node_id in ("left", "first", "late", "link")]
    assert order == sorted(order)
    assert "Note: Kick-off (group 1)" in html and "Locked" in html
    outline = html.split('<ol class="canvas-outline">', 1)[1].split("</ol>", 1)[0]
    assert "Last step" in outline and "Note: Left (group 1)" in outline and "<b>" not in outline
    assert 'href="#outline-late"' in html and "(then)" in html and "Linked with" in html
    assert "javascript:" not in html
    assert b"Text outline" in editor.get(f"/canvas/{canvas['slug']}").data


def test_outline_markdown_download(app, editor, canvas, owner):
    build_outline_canvas(app, canvas, owner)
    response = editor.get(f"/canvas/{canvas['slug']}/outline?format=md")
    assert response.status_code == 200 and response.mimetype == "text/markdown"
    assert 'filename="board.md"' in response.headers["Content-Disposition"]
    text = response.get_data(as_text=True)
    assert text.startswith("# Board\n\nAbout the board\n")
    assert "- **Note: Kick-off (group 1)**" in text and "  - → Note: Last step (then)" in text
    assert "  - ↔ Note: Last step" in text and "javascript:" not in text


def test_outline_respects_canvas_and_page_access(app, canvas, owner, make_user, login, client, db):
    secret = run(app, lambda: categories.create("Secret"))
    page = run(app, lambda: pages.create("Classified plan", "hidden words", category_id=secret["id"],
                                         author_id=None))
    apply(app, canvas, owner, {"op": "upsert_node", "node": {"id": "w", "type": "wiki_page", "page_id": page["id"]}})
    stranger = make_user("stranger1", role="editor")
    login(client, stranger)
    assert client.get(f"/canvas/{canvas['slug']}/outline").status_code == 404
    assert client.get(f"/canvas/{canvas['slug']}/outline?format=md").status_code == 404
    for kind in ("read", "write"):
        db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, 1)",
                   (stranger["id"], kind))
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'canvas.view')", (stranger["id"],))
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (canvas["id"], stranger["id"]))
    html = client.get(f"/canvas/{canvas['slug']}/outline").get_data(as_text=True)
    assert "Classified" not in html and "hidden words" not in html
    assert "The linked page was deleted or you cannot see it." in html
    assert "Classified" not in client.get(f"/canvas/{canvas['slug']}/outline?format=md").get_data(as_text=True)
    client.get("/logout")
    login(client, owner)
    html = client.get(f"/canvas/{canvas['slug']}/outline").get_data(as_text=True)
    assert "Classified plan" in html and f'/page/{page["slug"]}' in html


def test_outline_redirects_anonymous_visitors_of_private_canvases(client, canvas):
    response = client.get(f"/canvas/{canvas['slug']}/outline")
    assert response.status_code in (302, 404)
    if response.status_code == 302:
        assert "/login" in response.headers["Location"]


def test_empty_outline(editor, canvas):
    assert b"This canvas is empty." in editor.get(f"/canvas/{canvas['slug']}/outline").data


# ── Editor page ───────────────────────────────────────────────────────────────


def test_editor_page_offers_the_new_tools(app, editor, canvas, make_user, login, client, db):
    html = editor.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    for marker in ('data-action="align-left"', 'data-action="distribute-h" data-min="3"',
                   'data-action="group" data-min="2"', 'data-action="snap" aria-pressed="false"',
                   'data-action="export-png"', 'data-minimap', 'name="route"', 'value="hexagon"',
                   "canvas-export.js"):
        assert marker in html, marker
    viewer = make_user("viewer1", role="editor")
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'view')",
               (canvas["id"], viewer["id"]))
    client.get("/logout")
    login(client, viewer)
    html = client.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    assert 'data-action="export-svg"' in html and 'data-action="minimap"' in html
    assert 'data-action="align-left"' not in html and 'data-action="snap"' not in html
