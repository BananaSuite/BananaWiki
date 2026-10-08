# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Canvas: wiki-page nodes store only page ids and never reveal pages the reader cannot open."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import service
from bananawiki.wiki.features.pages import service as pages
from tests.features.pages_support import make_category, make_page, restrict


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def hidden(app):
    """A page in a category the restricted editor cannot read."""
    open_cat, hidden_cat = make_category(app, "Open"), make_category(app, "Hidden")
    page = make_page(app, "Salaries 2026", "confidential numbers", category_id=hidden_cat["id"])
    return {"open": open_cat, "page": page}


@pytest.fixture
def restricted(make_user, db, hidden):
    user = make_user("restricted_ed", role="editor")
    restrict(db, user, read=[hidden["open"]["id"]], write=[hidden["open"]["id"]])
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'all' WHERE id = 1")
    return user


def ops(client, layout, *items):
    response = client.post(f"/canvas/{layout['slug']}/ops", json={"ops": list(items)})
    assert response.status_code == 200, response.data[:300]
    return response.get_json()


def wiki(node_id, **fields):
    return {"id": node_id, "type": "wiki_page", "x": 0, "y": 0, **fields}


def stored(db, layout):
    return db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (layout["id"],))


def shown_nodes(client, layout):
    return {node["id"]: node for node in client.get(f"/canvas/{layout['slug']}/data").get_json()["data"]["nodes"]}


def test_restricted_and_missing_pages_look_the_same(app, client, login, db, hidden, restricted):
    layout = run(app, lambda: service.create("Probe", "", restricted["id"]))
    login(client, restricted)
    ops(client, layout, {"op": "upsert_node", "node": wiki("a", page_id=hidden["page"]["id"])},
        {"op": "upsert_node", "node": wiki("b", page_id=999_999)},
        {"op": "upsert_node", "node": wiki("c", page_slug=hidden["page"]["slug"])},
        {"op": "upsert_node", "node": wiki("d", page_slug="no-such-page")})
    nodes = shown_nodes(client, layout)
    assert {json.dumps({**node, "id": ""}, sort_keys=True) for node in nodes.values()} == {
        json.dumps({**nodes["a"], "id": ""}, sort_keys=True)}
    assert nodes["a"]["restricted"] is True and "page_id" not in nodes["a"] and "deleted" not in nodes["a"]
    outline = client.get(f"/canvas/{layout['slug']}/outline").get_data(as_text=True)
    assert outline.count("The linked page was deleted or you cannot see it.") == 4


def test_canvas_stores_no_title_or_slug_of_linked_pages(app, client, login, db, hidden, restricted):
    layout = run(app, lambda: service.create("Probe", "", restricted["id"]))
    login(client, restricted)
    ops(client, layout, {"op": "upsert_node", "node": wiki("a", page_id=hidden["page"]["id"])},
        {"op": "upsert_node", "node": wiki("b", page_slug=hidden["page"]["slug"])})
    data = stored(db, layout)
    assert "Salaries" not in data and "salaries-2026" not in data.replace('"page_slug":"salaries-2026"', "")
    nodes = {node["id"]: node for node in json.loads(data)["nodes"]}
    # The slug the editor typed stays, but is never turned into the id of a page they cannot read.
    assert nodes["a"]["page_id"] == hidden["page"]["id"] and "page_id" not in nodes["b"]
    assert not {"label", "deleted"} & (nodes["a"].keys() | nodes["b"].keys())


def test_personal_data_export_shows_canvas_pages_as_the_account_may_see_them(app, client, login, db, hidden,
                                                                               restricted):
    layout = run(app, lambda: service.create("Probe", "", restricted["id"]))
    # A canvas saved by 1.6.0 still holds the title and slug of the linked page.
    legacy = {"nodes": [wiki("a", page_id=hidden["page"]["id"], label="Salaries 2026", page_slug="salaries-2026"),
                        wiki("b", page_slug="salaries-2026", label="Salaries 2026", deleted=True)],
              "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (json.dumps(legacy), layout["id"]))
    login(client, restricted)
    response = client.get("/settings/export")
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    rows = json.loads(archive.read("data/canvas__layouts.json"))
    assert [row["id"] for row in rows] == [layout["id"]]
    assert "Salaries" not in rows[0]["data"] and "salaries" not in rows[0]["data"]
    assert all("page_id" not in node for node in json.loads(rows[0]["data"])["nodes"])


def test_personal_data_export_keeps_pages_the_account_can_read(app, client, login, db, make_user):
    owner = make_user("owner1", role="editor")
    page = make_page(app, "Team plan", "x")
    layout = run(app, lambda: service.create("Mine", "", owner["id"]))
    run(app, lambda: service.apply(layout, [{"op": "upsert_node", "node": wiki("a", page_id=page["id"])}],
                                   user_id=owner["id"], session_id=""))
    login(client, owner)
    archive = zipfile.ZipFile(io.BytesIO(client.get("/settings/export").get_data()))
    node = json.loads(json.loads(archive.read("data/canvas__layouts.json"))[0]["data"])["nodes"][0]
    assert node["page_id"] == page["id"] and node["label"] == "Team plan" and node["page_slug"] == page["slug"]


def test_editors_who_cannot_see_a_page_keep_its_link_when_saving(app, client, login, db, hidden, restricted,
                                                                 make_user):
    owner = make_user("boss", role="admin")
    layout = run(app, lambda: service.create("Shared", "", owner["id"]))
    run(app, lambda: service.apply(layout, [
        {"op": "upsert_node", "node": wiki("a", page_id=hidden["page"]["id"])},
        {"op": "upsert_node", "node": wiki("b", page_id=hidden["page"]["id"], locked=True)},
    ], user_id=owner["id"], session_id=""))
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'edit')",
               (layout["id"], restricted["id"]))
    login(client, restricted)
    body = client.get(f"/canvas/{layout['slug']}/data").get_json()
    nodes = {node["id"]: node for node in body["data"]["nodes"]}
    assert "page_id" not in nodes["a"] and "page_id" not in nodes["b"]
    # The editor moves one node, then saves the whole document as received.
    ops(client, layout, {"op": "upsert_node", "node": {**nodes["a"], "x": 50}},
        {"op": "upsert_node", "node": {**nodes["b"], "layer": 3}})
    saved = client.post(f"/canvas/{layout['slug']}/data", json={"data": body["data"]})
    assert saved.status_code == 200, saved.data
    links = {node["id"]: node.get("page_id") for node in json.loads(stored(db, layout))["nodes"]}
    assert links == {"a": hidden["page"]["id"], "b": hidden["page"]["id"]}


def test_guessing_the_page_of_a_locked_node_tells_nothing(app, client, login, db, hidden, restricted, make_user):
    owner = make_user("boss", role="admin")
    layout = run(app, lambda: service.create("Shared", "", owner["id"]))
    run(app, lambda: service.apply(layout, [
        {"op": "upsert_node", "node": wiki("b", page_id=hidden["page"]["id"], locked=True)}],
        user_id=owner["id"], session_id=""))
    db.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, ?, 'edit')",
               (layout["id"], restricted["id"]))
    login(client, restricted)
    body = client.get(f"/canvas/{layout['slug']}/data").get_json()
    node = body["data"]["nodes"][0]
    page_id = hidden["page"]["id"]
    by_ops, by_save = set(), set()
    for guess in (page_id + 1, page_id, page_id + 2, 999_999):
        answer = ops(client, layout, {"op": "upsert_node", "node": {**node, "page_id": guess}})
        by_ops.add((bool(answer.get("rejected")), len(answer["applied"])))
        saved = client.post(f"/canvas/{layout['slug']}/data",
                            json={"data": {**body["data"], "nodes": [{**node, "page_id": guess}]}})
        by_save.add(saved.status_code)
    assert by_ops == {(False, 1)} and by_save == {200}
    # Unlocking may name any page too: the node keeps its own.
    ops(client, layout, {"op": "upsert_node", "node": {**node, "page_id": 999_999, "locked": False}})
    [kept] = json.loads(stored(db, layout))["nodes"]
    assert kept["page_id"] == page_id and "locked" not in kept


def test_page_links_follow_renames_and_deletions(app, client, login, db, make_user):
    owner = make_user("owner1", role="editor")
    admin = make_user("admin1", role="admin")
    page = make_page(app, "Target page", "Some **content** here")
    layout = run(app, lambda: service.create("Board", "", owner["id"]))
    run(app, lambda: service.apply(layout, [{"op": "upsert_node", "node": wiki("w", page_id=page["id"])}],
                                   user_id=owner["id"], session_id=""))
    # A node saved by an older version carries only the slug and a title.
    data = json.loads(stored(db, layout))
    data["nodes"].append(wiki("old", page_slug=page["slug"], label="stale"))
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (json.dumps(data), layout["id"]))
    version = db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout["id"],))

    run(app, lambda: pages.change_slug(pages.get(page["id"]), "moved-page"))
    nodes = {node["id"]: node for node in json.loads(stored(db, layout))["nodes"]}
    assert nodes["old"]["page_id"] == page["id"] and not {"page_slug", "label"} & nodes["old"].keys()
    assert db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout["id"],)) == version + 1
    login(client, owner)
    events = client.get(f"/canvas/{layout['slug']}/sync?since=0").get_json()["events"]
    assert [event["payload"]["node"]["id"] for event in events if event["op_type"] == "upsert_node"][-1] == "old"
    shown = shown_nodes(client, layout)
    assert shown["w"]["page_slug"] == shown["old"]["page_slug"] == "moved-page"
    assert shown["w"]["label"] == "Target page"

    # Nodes that hold only the page id need no rewrite: the title is read when shown.
    run(app, lambda: pages.update(pages.get(page["id"]), author_id=None, title="Renamed target"))
    assert shown_nodes(client, layout)["w"]["label"] == "Renamed target"
    run(app, lambda: pages.delete(pages.get(page["id"]), actor_id=None))
    assert db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout["id"],)) == version + 1
    shown = shown_nodes(client, layout)
    assert shown["w"]["restricted"] is True and "page_id" not in shown["w"]
    client.get("/logout")
    login(client, admin)
    shown = shown_nodes(client, layout)
    assert shown["w"]["deleted"] is True and "restricted" not in shown["w"] and "page_id" not in shown["w"]
