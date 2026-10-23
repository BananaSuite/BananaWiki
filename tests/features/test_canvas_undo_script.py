"""Canvas editor with two people on one canvas: undo, redo and dialogs, run under node.

canvas-editor.js runs twice on the page the server renders (tests/features/mini_dom.js
stands in for the browser), as sessions "a" and "b". A small stand-in for the
server's /data, /sync and /ops keeps the document and the operation log between
them; the scene is a stand-in too (no drawing), with the real canvas-core.js
helpers. Timers never fire: each step saves or syncs when it says so. Skipped
where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import service

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

STATIC = Path(service.__file__).parent / "static"
MINI_DOM = Path(__file__).with_name("mini_dom.js")

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const scenario = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const copy = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));  // as sent over the network
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve)); };

// The server: the document, the operation log, and the lock rule (a locked node only changes by being unlocked).
const server = { nodes: {}, edges: {}, events: [], version: 1, sent: [] };
(scenario.doc.nodes || []).forEach((node) => { server.nodes[node.id] = copy(node); });
(scenario.doc.edges || []).forEach((edge) => { server.edges[edge.id] = copy(edge); });
const head = () => server.events.length;
const unlocks = (old, node) => node && !node.locked && JSON.stringify(Object.assign({}, old, { locked: undefined })) === JSON.stringify(Object.assign({}, node, { locked: undefined }));
function applyOps(ops, session) {
  const applied = [], rejected = [];
  ops.forEach((op) => {
    const id = op.node ? op.node.id : op.id;
    const old = server.nodes[id];
    if ((op.op === "upsert_node" || op.op === "delete_node") && old && old.locked && !unlocks(old, op.node)) {
      rejected.push({ op: op.op, id: id, reason: "locked" });
      return;
    }
    if (op.op === "upsert_node") server.nodes[id] = op.node;
    else if (op.op === "delete_node") {
      if (!old) return;
      delete server.nodes[id];
      Object.keys(server.edges).forEach((key) => {
        if (server.edges[key].from === id || server.edges[key].to === id) delete server.edges[key];
      });
    } else if (op.op === "upsert_edge") {
      if (!server.nodes[op.edge.from] || !server.nodes[op.edge.to]) return;
      server.edges[op.edge.id] = op.edge;
    } else if (op.op === "delete_edge") {
      if (!server.edges[op.id]) return;
      delete server.edges[op.id];
    }
    applied.push(op);
    server.events.push({ seq: head() + 1, op_type: op.op, payload: op, session: session });
  });
  if (applied.length) server.version += 1;
  return { version: server.version, seq: head(), applied: applied, rejected: rejected };
}
function document_() {
  return { nodes: Object.values(server.nodes), edges: Object.values(server.edges) };
}

function open(name) {
  const document = new dom.Document(scenario.html);
  const app = document.querySelector("div#canvas-app");
  const urls = app.dataset;
  const toasts = [];
  const BW = {
    t: (key) => "t:" + key,
    toast: (message, kind) => toasts.push([message, kind]),
    onReady: (fn) => fn(),
    fetchJSON(url, options) {
      options = options || {};
      const session = (options.headers || {})["X-Canvas-Session"];
      let answer;
      if (url === urls.dataUrl) answer = { version: server.version, seq: head(), data: document_() };
      else if (url.indexOf(urls.syncUrl + "?since=") === 0) {
        const since = Number(url.slice(url.indexOf("=") + 1));
        answer = { version: server.version, seq: head(), reset: false,
                   events: server.events.filter((e) => e.seq > since && e.session !== session) };
      } else if (url === urls.opsUrl && options.method === "POST") {
        const ops = copy(options.body.ops);
        server.sent.push({ who: name, ops: ops });
        answer = applyOps(ops, session);
      } else {
        return Promise.reject(new Error("unexpected request " + url));
      }
      return Promise.resolve(copy(answer));
    },
  };
  const window = dom.makeWindow(document, {
    BW: BW, crypto: require("crypto").webcrypto,
    setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: () => 0, addEventListener: () => {},
  });
  const context = vm.createContext({ window: window, document: document, BW: BW, console: console });
  for (const file of ["canvas-core.js", "canvas-editor.js"]) {
    if (file === "canvas-editor.js") {
      // The scene without drawing: the document, the selection, and the real lookups and loading.
      const Real = BW.Canvas.Scene;
      const Scene = function (stage, options) {
        Object.assign(this, { stage: stage, options: options, nodes: [], edges: [], pages: {}, cache: {}, selected: {},
                              selectedEdge: null, connectFrom: null, viewport: { x: 0, y: 0, zoom: 1 }, elements: {},
                              pointers: {}, world: document.createElement("div") });
      };
      ["load", "absorb", "node", "edge"].forEach((key) => { Scene.prototype[key] = Real.prototype[key]; });
      ["render", "renderEdges", "renderPreview", "moveNode", "fit", "applyViewport", "zoomBy"].forEach((key) => {
        Scene.prototype[key] = function () {};
      });
      Scene.prototype.center = () => ({ x: 0, y: 0 });
      BW.Canvas.Scene = Scene;
    }
    vm.runInContext(fs.readFileSync(scenario.static + "/" + file, "utf8"), context, { filename: file });
  }
  return { name: name, document: document, editor: BW.canvasEditor, toasts: toasts };
}

function look(person) {
  const editor = person.editor, form = person.document;
  return {
    nodes: copy(editor.scene.nodes), edges: copy(editor.scene.edges), toasts: person.toasts,
    undo: editor.undoStack.length, redo: editor.redoStack.length,
    undo_disabled: form.querySelector("[data-action='undo']").disabled,
    node_dialog: form.querySelector("dialog#canvas-node-dialog").open,
    edge_dialog: form.querySelector("dialog#canvas-edge-dialog").open,
  };
}

(async () => {
  const people = { a: open("a"), b: open("b") };
  await flush();
  for (const step of scenario.steps) {
    const person = people[step.who], editor = person.editor;
    if (step.add) editor.addNode(step.add, step.at || null);
    else if (step.move) { editor.select([step.move], false); editor.moveSelection(step.by[0], step.by[1]); }
    else if (step.remove) { editor.select([step.remove], false); editor.deleteSelection(); }
    else if (step.remove_edge) { editor.selectEdge(step.remove_edge); editor.deleteSelection(); }
    else if (step.lock) { editor.select([step.lock], false); editor.toggleLock(); }
    else if (step.connect) { editor.scene.connectFrom = step.connect[0]; editor.finishConnect(step.connect[1]); }
    else if (step.undo) editor.undo();
    else if (step.redo) editor.redo();
    else if (step.save) await editor.save();
    else if (step.sync) editor.sync();
    else if (step.edit) editor.openNodeDialog(step.edit);
    else if (step.edit_edge) editor.openEdgeDialog(step.edit_edge);
    else if (step.set) person.document.querySelector(step.set).value = step.value;
    else if (step.submit) person.document.querySelector(step.submit).dispatchEvent(new dom.Event("submit", { bubbles: true }));
    else throw new Error("unknown step " + JSON.stringify(step));
    await flush();
  }
  return { a: look(people.a), b: look(people.b), server: document_(), sent: server.sent, errors: dom.errors };
})().then((result) => process.stdout.write(JSON.stringify(result)),
          (error) => process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })));
"""


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


@pytest.fixture
def owner(make_user):
    return make_user("undo_owner", role="editor")


@pytest.fixture
def page(app, client, login, owner):
    canvas = run(app, lambda: service.create("Class board", "", owner["id"]))
    login(client, owner)
    html = client.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    assert 'data-can-edit="true"' in html
    return html


def play(tmp_path, page, steps, nodes=(), edges=()):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps({
        "html": page, "static": str(STATIC), "steps": steps,
        "doc": {"nodes": list(nodes), "edges": list(edges)}}), encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result.get("crashed") is None, result.get("crashed")
    assert result["errors"] == []
    return result


def note(node_id, x=0, y=0, **fields):
    return {"id": node_id, "type": "text", "x": x, "y": y, "width": 240, "height": 120, "layer": 1,
            "text_size": 13, "content": node_id, **fields}


def ids(items):
    return sorted(item["id"] for item in items)


def sent(result, who):
    return [op for batch in result["sent"] if batch["who"] == who for op in batch["ops"]]


def by_id(items, item_id):
    return next(item for item in items if item["id"] == item_id)


def test_undo_takes_back_only_your_own_change(tmp_path, page):
    result = play(tmp_path, page, [
        {"who": "b", "add": note("mine"), "at": {"x": 0, "y": 0}}, {"who": "b", "save": True},
        {"who": "a", "sync": True},
        {"who": "a", "add": note("theirs"), "at": {"x": 400, "y": 0}}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
        {"who": "a", "sync": True},
    ])
    # B's undo removes B's note, and only that: A's note, added after it, stays everywhere.
    assert [op for op in sent(result, "b") if op["op"] == "delete_node"] == [{"op": "delete_node", "id": "mine"}]
    assert ids(result["server"]["nodes"]) == ids(result["a"]["nodes"]) == ids(result["b"]["nodes"]) == ["theirs"]
    assert result["b"]["redo"] == 1 and result["b"]["undo"] == 0 and result["b"]["undo_disabled"] is True


def test_undo_and_redo_keep_someone_elses_edits(tmp_path, page):
    start = [note("n1", x=0), note("n2", x=480)]
    result = play(tmp_path, page, [
        {"who": "b", "move": "n1", "by": [24, 0]}, {"who": "b", "save": True},
        {"who": "a", "sync": True},
        {"who": "a", "move": "n2", "by": [0, 48]}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
        {"who": "b", "redo": True}, {"who": "b", "save": True},
    ], nodes=start)
    # Each undo and redo sends B's node only; A's move of n2 is kept.
    assert [(op["op"], op["node"]["id"], op["node"]["x"]) for op in sent(result, "b")] == [
        ("upsert_node", "n1", 24), ("upsert_node", "n1", 0), ("upsert_node", "n1", 24)]
    server = result["server"]["nodes"]
    assert (by_id(server, "n1")["x"], by_id(server, "n2")["y"]) == (24, 48)
    assert by_id(result["b"]["nodes"], "n2")["y"] == 48


def test_undoing_an_unsaved_change_lands_on_the_saved_version(tmp_path, page):
    result = play(tmp_path, page, [
        {"who": "b", "move": "n1", "by": [24, 0]},  # not saved yet
        {"who": "a", "move": "n1", "by": [0, 96]}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
    ], nodes=[note("n1")])
    # B's own move goes; what A saved meanwhile stays, so there is nothing to send.
    assert sent(result, "b") == []
    assert (by_id(result["b"]["nodes"], "n1")["x"], by_id(result["b"]["nodes"], "n1")["y"]) == (0, 96)


def test_undo_never_brings_back_or_changes_what_someone_else_deleted_or_locked(tmp_path, page):
    start = [note("gone"), note("locked", x=300), note("free", x=600)]
    result = play(tmp_path, page, [
        {"who": "b", "move": "gone", "by": [24, 0]}, {"who": "b", "save": True},
        {"who": "b", "move": "locked", "by": [24, 0]}, {"who": "b", "save": True},
        {"who": "b", "move": "free", "by": [24, 0]}, {"who": "b", "save": True},
        {"who": "a", "sync": True},
        {"who": "a", "remove": "gone"}, {"who": "a", "save": True},
        {"who": "a", "lock": "locked"},
        {"who": "b", "sync": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
    ], nodes=start)
    # The first undo moves "free" back; the steps on the deleted and the locked node do nothing and are skipped.
    assert [(op["op"], op["node"]["id"]) for op in sent(result, "b")[3:]] == [("upsert_node", "free")]
    server = result["server"]["nodes"]
    assert ids(server) == ["free", "locked"] and by_id(server, "locked")["locked"] is True
    assert by_id(server, "free")["x"] == 600 and by_id(server, "locked")["x"] == 324
    assert result["b"]["undo"] == 0 and result["b"]["toasts"] == []


def test_undoing_your_new_node_takes_the_connections_to_it_and_redo_brings_them_back(tmp_path, page):
    steps = [
        {"who": "b", "add": note("mine"), "at": {"x": 120, "y": 60}}, {"who": "b", "save": True},
        {"who": "a", "sync": True},
        {"who": "a", "connect": ["theirs", "mine"]}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "undo": True}, {"who": "b", "save": True},
    ]
    undone = play(tmp_path, page, steps, nodes=[note("theirs", x=400)])
    # The connection A drew to B's node goes with it (the server removes it with the node).
    assert sent(undone, "b")[-1:] == [{"op": "delete_node", "id": "mine"}]
    assert ids(undone["server"]["nodes"]) == ["theirs"] and undone["server"]["edges"] == []
    assert undone["b"]["edges"] == [] and undone["b"]["redo"] == 1
    redone = play(tmp_path, page, steps + [{"who": "b", "redo": True}, {"who": "b", "save": True}],
                  nodes=[note("theirs", x=400)])
    assert [op["op"] for op in sent(redone, "b")[-2:]] == ["upsert_node", "upsert_edge"]
    assert ids(redone["server"]["nodes"]) == ["mine", "theirs"]
    assert [(edge["from"], edge["to"]) for edge in redone["server"]["edges"]] == [("theirs", "mine")]


def test_saving_the_dialog_of_a_node_someone_deleted_puts_it_back(tmp_path, page):
    result = play(tmp_path, page, [
        {"who": "b", "edit": "n1"},
        {"who": "a", "remove": "n1"}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "set": "dialog#canvas-node-dialog textarea[name='content']", "value": "My new text"},
        {"who": "b", "submit": "dialog#canvas-node-dialog form[data-node-form]"},
        {"who": "b", "save": True},
        {"who": "a", "sync": True},
    ], nodes=[note("n1", x=120, y=240, content="Old text", color="#4caf50")])
    [back] = result["server"]["nodes"]
    assert back["id"] != "n1" and back["content"] == "My new text" and back["color"] == "#4caf50"
    assert (back["x"], back["y"], back["width"], back["height"]) == (120, 240, 240, 120)
    assert ids(result["a"]["nodes"]) == [back["id"]]
    assert result["b"]["node_dialog"] is False and result["b"]["toasts"] == [["t:canvas.status.node_restored", "info"]]


def test_saving_the_dialog_of_a_connection_someone_deleted(tmp_path, page):
    start = [note("n1"), note("n2", x=400), note("n3", x=800)]
    edges = [{"id": "e1", "from": "n1", "to": "n2", "label": "old", "text_size": 14, "color": "#336699"},
             {"id": "e2", "from": "n2", "to": "n3", "label": "old", "text_size": 14}]
    result = play(tmp_path, page, [
        {"who": "b", "edit_edge": "e1"},
        {"who": "a", "remove_edge": "e1"}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "set": "input#ce-label", "value": "new"},
        {"who": "b", "submit": "form[data-edge-form]"},
        {"who": "b", "save": True},
        # The other one goes with its element: nothing to put it back between.
        {"who": "b", "edit_edge": "e2"},
        {"who": "a", "remove": "n3"}, {"who": "a", "save": True},
        {"who": "b", "sync": True},
        {"who": "b", "set": "input#ce-label", "value": "lost"},
        {"who": "b", "submit": "form[data-edge-form]"},
    ], nodes=start, edges=edges)
    [back] = result["server"]["edges"]
    assert back["id"] not in ("e1", "e2") and (back["from"], back["to"], back["label"]) == ("n1", "n2", "new")
    assert back["color"] == "#336699"
    assert result["b"]["edge_dialog"] is False
    assert result["b"]["toasts"] == [["t:canvas.status.edge_restored", "info"], ["t:canvas.status.edge_lost", "error"]]
