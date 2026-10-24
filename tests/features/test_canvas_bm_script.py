"""The canvas editor with BM mode's fewer tools, run under node: keys that go with hidden controls.

canvas-editor.js runs on the page the server renders (tests/features/mini_dom.js
stands in for the browser), with a stand-in for the server's /data and /ops and for
localStorage, and a scene that draws nothing. Skipped where node is not installed.
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
const copy = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve)); };

const document = new dom.Document(scenario.html);
const app = document.querySelector("div#canvas-app");
const urls = app.dataset;
const store = Object.assign({}, scenario.storage);
const localStorage = {
  getItem: (key) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
};
let version = 1, seq = 0;
const BW = {
  t: (key) => "t:" + key,
  toast: () => {},
  onReady: (fn) => fn(),
  fetchJSON(url, options) {
    options = options || {};
    if (url === urls.dataUrl) return Promise.resolve(copy({ version: version, seq: seq, data: scenario.doc }));
    if (url === urls.opsUrl && options.method === "POST") {
      const ops = copy(options.body.ops);
      version += 1; seq += ops.length;
      return Promise.resolve({ version: version, seq: seq, applied: ops, rejected: [] });
    }
    if (url.indexOf(urls.syncUrl + "?since=") === 0) return Promise.resolve({ version: version, seq: seq, reset: false, events: [] });
    return Promise.reject(new Error("unexpected request " + url));
  },
};
const window = dom.makeWindow(document, {
  BW: BW, crypto: require("crypto").webcrypto,
  setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: () => 0, addEventListener: () => {},
});
const context = vm.createContext({ window: window, document: document, BW: BW, console: console, localStorage: localStorage });
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

(async () => {
  await flush();
  const editor = BW.canvasEditor;
  const start = { snap: editor.snap, minimap: editor.minimap.visible };
  const handled = [];
  for (const press of scenario.keys) {
    editor.select(scenario.select, false);
    handled.push(editor.handleKey({ key: press.key, ctrlKey: !!press.ctrl, shiftKey: !!press.shift, metaKey: false,
                                    altKey: false, target: editor.stage }));
    await flush();
  }
  return {
    start: start, handled: handled, snap: editor.snap, storage: store, nodes: copy(editor.scene.nodes),
    selected: Object.keys(editor.scene.selected).sort(), has_minimap: !!document.querySelector("[data-minimap]"),
    errors: dom.errors,
  };
})().then((result) => process.stdout.write(JSON.stringify(result)),
          (error) => process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })));
"""

NODES = [
    {"id": "n1", "type": "text", "x": 0, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "one"},
    {"id": "n2", "type": "text", "x": 480, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "two"},
    {"id": "n3", "type": "text", "x": 0, "y": 240, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "three"},
]


def _page(app, client, login, make_user, name, **fields):
    person = make_user(name, role="editor", **fields)
    with app.test_request_context(), connection_scope():
        canvas = service.create("Class map", "", person["id"])
    login(client, person)
    html = client.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    assert 'data-can-edit="true"' in html
    return html


@pytest.fixture
def full_page(app, client, login, make_user):
    return _page(app, client, login, make_user, "keysfull")


@pytest.fixture
def simple_page(app, client, login, make_user):
    return _page(app, client, login, make_user, "keyssimple", bm_mode=1)


def play(tmp_path, page, keys, storage=None, select=("n1", "n2"), nodes=NODES):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps({
        "html": page, "static": str(STATIC), "keys": list(keys), "storage": storage or {},
        "select": list(select), "doc": {"nodes": list(nodes), "edges": []}}), encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result.get("crashed") is None, result.get("crashed")
    assert result["errors"] == []
    return result


def _node(result, node_id):
    return next(node for node in result["nodes"] if node["id"] == node_id)


def _with(**fields):
    """The three notes, n1 and n2 carrying *fields* (``locked``, ``group``)."""
    return [{**node, **fields} if node["id"] in ("n1", "n2") else node for node in NODES]


def test_the_full_editor_answers_the_grid_lock_and_group_keys(tmp_path, full_page):
    result = play(tmp_path, full_page, [{"key": "g"}, {"key": "l"}])
    assert result["handled"] == [True, True]
    assert result["start"]["snap"] is True and result["snap"] is False and result["storage"]["canvas-snap"] == "0"
    assert _node(result, "n1").get("locked") and _node(result, "n2").get("locked")
    assert result["has_minimap"] is True
    grouped = play(tmp_path, full_page, [{"key": "g", "ctrl": True}])
    assert grouped["handled"] == [True] and _node(grouped, "n1").get("group")
    assert _node(grouped, "n1")["group"] == _node(grouped, "n2")["group"]
    ungrouped = play(tmp_path, full_page, [{"key": "G", "ctrl": True, "shift": True}], nodes=_with(group="g1"))
    assert ungrouped["handled"] == [True] and not _node(ungrouped, "n1").get("group")


def test_with_fewer_tools_the_grid_lock_and_group_keys_do_nothing(tmp_path, simple_page):
    result = play(tmp_path, simple_page, [{"key": "g"}, {"key": "G", "shift": True}, {"key": "l"},
                                          {"key": "g", "ctrl": True}])
    assert result["handled"] == [False, False, False, False]
    assert result["snap"] is True and "canvas-snap" not in result["storage"]
    for node_id in ("n1", "n2"):
        assert not _node(result, node_id).get("locked") and not _node(result, node_id).get("group")
    # A selection that is only partly locked would be locked by L: that stays out of reach too.
    mixed = play(tmp_path, simple_page, [{"key": "l"}],
                 nodes=[{**node, "locked": True} if node["id"] == "n1" else node for node in NODES])
    assert mixed["handled"] == [False]
    assert _node(mixed, "n1").get("locked") and not _node(mixed, "n2").get("locked")


def test_with_fewer_tools_what_is_locked_or_grouped_elsewhere_can_still_be_freed(tmp_path, simple_page):
    # Template frames, a teacher's locks or a duplicated locked element must not get stuck.
    unlocked = play(tmp_path, simple_page, [{"key": "l"}], nodes=_with(locked=True))
    assert unlocked["handled"] == [True]
    assert not _node(unlocked, "n1").get("locked") and not _node(unlocked, "n2").get("locked")
    ungrouped = play(tmp_path, simple_page, [{"key": "G", "ctrl": True, "shift": True}], nodes=_with(group="g1"))
    assert ungrouped["handled"] == [True]
    assert not _node(ungrouped, "n1").get("group") and not _node(ungrouped, "n2").get("group")


def test_with_fewer_tools_snap_ignores_what_this_browser_remembers(tmp_path, full_page, simple_page):
    # Someone switched the grid off on this browser in the full editor; the hidden toggle cannot switch it back.
    assert play(tmp_path, full_page, [], storage={"canvas-snap": "0"})["start"]["snap"] is False
    simple = play(tmp_path, simple_page, [], storage={"canvas-snap": "0", "canvas-minimap": "1"})
    assert simple["start"] == {"snap": True, "minimap": False}
    assert simple["has_minimap"] is False and simple["storage"]["canvas-snap"] == "0", "the remembered choice is kept"


def test_with_fewer_tools_the_other_keys_still_work(tmp_path, simple_page):
    # Select all has no hidden control behind it (and #34 made its delete undoable): it stays.
    result = play(tmp_path, simple_page, [{"key": "a", "ctrl": True}], select=("n1",))
    assert result["handled"] == [True] and result["selected"] == ["n1", "n2", "n3"]
