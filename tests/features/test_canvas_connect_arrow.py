"""BM mode's connect arrow and first note: the arrow under the selected element connects it or adds a note
below it, and new canvases start from one note.

The page tests read what the server renders. The arrow tests run canvas-editor.js under node
(tests/features/mini_dom.js stands in for the browser, with a stand-in for the server's /data and /ops
and a scene that draws no nodes but builds and places the real handles), pressing and dragging the
handles with pointer events; they are skipped where node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import service

from .test_canvas_bm import _canvas_page, _simplified

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

STATIC = Path(service.__file__).parent / "static"
MINI_DOM = Path(__file__).with_name("mini_dom.js")
ADD_BELOW_HELP = "Shift+Enter adds a matching note below the focused element"


@pytest.fixture
def easy(app_factory):
    return app_factory(environ={"BW_EASY_WIKI": "1"})


@pytest.fixture(params=["wiki", "account"])
def bm_page(request, app, easy, make_user, login):
    """The canvas page for an editor in BM mode: the whole wiki's, or their account's only."""
    if request.param == "wiki":
        return _canvas_page(easy, make_user, login, "arrowwiki")
    return _canvas_page(app, make_user, login, "arrowaccount", bm_mode=1)


@pytest.fixture
def full_page(app, make_user, login):
    return _canvas_page(app, make_user, login, "arrowfull")


def test_bm_mode_names_the_arrow_and_the_first_note(bm_page, full_page):
    assert {"canvas.connect_arrow", "canvas.first_note"} <= set(_simplified(bm_page))
    # The keyboard way to the arrow's click, in the help screen readers get through the stage's description.
    assert ADD_BELOW_HELP in bm_page
    assert ADD_BELOW_HELP not in full_page and "canvas.connect_arrow" not in _simplified(full_page)


def _list_page(app, person, login):
    client = app.test_client()
    login(client, person)
    response = client.get("/canvas")
    assert response.status_code == 200
    return client, response.get_data(as_text=True)


def test_new_canvases_start_from_the_first_note_in_bm_mode(easy, app, make_user, login):
    teacher = make_user("firstbm", role="admin")
    _, page = _list_page(easy, teacher, login)
    assert '<option value="first_note" selected>' in page
    # The empty canvas and the other templates stay to choose from.
    assert '<option value="">' in page and '<option value="mind_map">' in page
    _, full = _list_page(app, make_user("firstfull", role="admin"), login)
    assert '<option value="first_note">' in full and '<option value="">' in full
    assert not re.search(r'<option value="[^"]*" selected>', full), "outside BM mode a canvas starts empty"


def test_the_first_note_is_one_note_to_write_in(easy, make_user, login):
    teacher = make_user("firstnote", role="admin")
    client, _ = _list_page(easy, teacher, login)
    assert client.post("/canvas/create", data={"title": "Ideas", "template": "first_note"}).status_code == 302
    with easy.app_context(), connection_scope() as session:
        doc = json.loads(session.scalar("SELECT data FROM canvas__layouts WHERE slug = 'ideas'"))
    assert doc["edges"] == [] and len(doc["nodes"]) == 1
    note = doc["nodes"][0]
    assert note["content"] == "Write your idea here"
    # The size and text size the editor gives a new note: the arrow's notes below it look the same.
    assert (note["type"], note["width"], note["height"], note["text_size"]) == ("text", 240, 120, 13)
    assert note["shape"] == "rounded" and note["color"] == "#2196f3"
    assert note["x"] % 24 == 0 and note["y"] % 24 == 0, "on the grid, so the notes added below line up"


HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const scenario = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const copy = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve)); };

const document = new dom.Document(scenario.html);
// The real scene builds its SVG layers with createElementNS: plain elements stand in for them.
document.createElementNS = (ns, tag) => document.createElement(tag);
const app = document.querySelector("div#canvas-app");
const urls = app.dataset;
const stage = document.querySelector("[data-canvas-stage]");
stage.setPointerCapture = () => {};
// Where the stage is on the page. By default it is large and the view is shifted to match, so client
// coordinates are canvas coordinates.
const box = scenario.stage || [-10000, -10000, 20000, 20000];
stage.getBoundingClientRect = () => ({ left: box[0], top: box[1], width: box[2], height: box[3],
                                       right: box[0] + box[2], bottom: box[1] + box[3] });
const store = {};
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
const targets = [];
for (const file of ["canvas-core.js", "canvas-editor.js"]) {
  if (file === "canvas-editor.js") {
    // The scene without drawing the nodes: the document, the selection, the real lookups, loading, toWorld
    // and panning, and the real world with BM mode's handles, which render and moveNode place as they do.
    const Real = BW.Canvas.Scene;
    const Scene = function (stage, options) {
      Object.assign(this, { id: "cv1", stage: stage, options: options, nodes: [], edges: [], pages: {}, cache: {},
                            selected: {}, selectedEdge: null, connectFrom: null, dropTarget: null, handlesFor: null,
                            preview: null, viewport: Object.assign({}, scenario.viewport || { x: 10000, y: 10000, zoom: 1 }),
                            elements: {}, pointers: {} });
      Real.prototype.build.call(this);
      if (this.handles) this.handles.style = {};
    };
    ["load", "absorb", "node", "edge", "toWorld", "panBy", "placeHandles"].forEach((key) => {
      Scene.prototype[key] = Real.prototype[key];
    });
    ["renderEdges", "renderPreview", "fit", "applyViewport", "zoomBy"].forEach((key) => {
      Scene.prototype[key] = function () {};
    });
    Scene.prototype.render = function () { this.placeHandles(); };
    Scene.prototype.moveNode = function (node) { if (this.handlesFor === node.id) this.placeHandles(); };
    Scene.prototype.markDropTarget = function (id) { Real.prototype.markDropTarget.call(this, id); targets.push(this.dropTarget); };
    Scene.prototype.center = () => ({ x: 0, y: 0 });
    BW.Canvas.Scene = Scene;
  }
  vm.runInContext(fs.readFileSync(scenario.static + "/" + file, "utf8"), context, { filename: file });
}

const dialog = document.querySelector("dialog#canvas-node-dialog");
const form = dialog.querySelector("form[data-node-form]");

function pointer(type, target, point) {
  target.dispatchEvent(new dom.Event(type, { bubbles: true, props: { button: 0, pointerId: 7, clientX: point[0], clientY: point[1] } }));
}

(async () => {
  await flush();
  const editor = BW.canvasEditor;
  const start = editor.scene.nodes.map((node) => node.id);
  // Stand-ins for the node elements the scene would draw: focus, outlines and the knob's node need them.
  editor.scene.nodes.forEach((node) => {
    const element = document.createElement("div");
    element.className = "cv-node";
    element.setAttribute("data-id", node.id);
    stage.appendChild(element);
    editor.scene.elements[node.id] = element;
  });
  const handles = stage.querySelector(".cv-handles");
  // The handle of the element, shown once it is the one selected element, as a person would see it.
  const handleOn = (id, kind) => {
    editor.select([id], false);
    if (!handles || handles.hidden || handles.getAttribute("data-for") !== id) throw new Error("no handles on " + id);
    return handles.querySelector('[data-handle="' + kind + '"]');
  };
  const looks = [];
  for (const step of scenario.steps) {
    if (step.arrow || step.knob) {
      // Pressed on the handle, moved along the path on the stage (it holds the pointer), let go at its end.
      const path = step.path;
      pointer("pointerdown", handleOn(step.arrow || step.knob, step.arrow ? "arrow" : "resize"), path[0]);
      for (const point of path.slice(1)) pointer("pointermove", stage, point);
      pointer(step.cancel ? "pointercancel" : "pointerup", stage, path[path.length - 1]);
    } else if (step.select) editor.select(step.select, false);
    else if (step.key) {
      editor.handleKey({ key: step.key, shiftKey: !!step.shift, ctrlKey: false, metaKey: false, altKey: false,
                         target: editor.scene.elements[step.on] });
    } else if (step.set) form.querySelector(step.set).value = step.value;
    else if (step.submit) form.dispatchEvent(new dom.Event("submit", { bubbles: true }));
    else if (step.undo) editor.undo();
    else if (step.look) {
      looks.push({ open: dialog.open, title: dialog.querySelector("[data-dialog-title]").textContent,
                   editing: editor.dialogState && editor.dialogState.nodeId, preview: editor.scene.preview,
                   interaction: !!editor.interaction,
                   handles: handles ? { hidden: handles.hidden, for: handles.getAttribute("data-for") } : null,
                   cornerless: Object.keys(editor.scene.elements).filter((id) => editor.scene.elements[id].classList.contains("has-handles")) });
    } else throw new Error("unknown step " + JSON.stringify(step));
    await flush();
  }
  return { looks: looks, nodes: copy(editor.scene.nodes), edges: copy(editor.scene.edges), targets: targets,
           added: copy(editor.scene.nodes.filter((node) => start.indexOf(node.id) < 0)),
           viewport: copy(editor.scene.viewport), undo: editor.undoStack.length, errors: dom.errors };
})().then((result) => process.stdout.write(JSON.stringify(result)),
          (error) => process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })));
"""

GREEN, RED = "#4caf50", "#e91e63"
N1 = {"id": "n1", "type": "text", "x": 0, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 15,
      "content": "one", "shape": "rounded", "color": GREEN}
NODES = [
    N1,
    {"id": "n2", "type": "text", "x": 480, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "two"},
    {"id": "n3", "type": "image", "x": 0, "y": 600, "width": 320, "height": 240, "layer": 1, "text_size": 17,
     "url": "https://example.org/map.png", "color": RED},
    # A template column: a locked frame on the bottom layer, a background rather than an element.
    {"id": "frame", "type": "text", "x": -1000, "y": -1000, "width": 600, "height": 600, "layer": 0,
     "text_size": 13, "display_text": "What went well", "locked": True},
]
EDGES = [{"id": "e0", "from": "n2", "to": "n3", "label": "", "text_size": 14}]
CLICK = {"arrow": "n1", "path": [[120, 150]]}


def play(tmp_path, page, steps, nodes=NODES, stage=None, viewport=None):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    scenario = {"html": page, "static": str(STATIC), "steps": steps, "doc": {"nodes": list(nodes), "edges": EDGES},
                "stage": stage, "viewport": viewport}
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result.get("crashed") is None, result.get("crashed")
    assert result["errors"] == []
    return result


def _links(result):
    return [(edge["from"], edge["to"]) for edge in result["edges"]]


def _overlap(a, b):
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])


@needs_node
def test_the_handles_go_on_the_one_selected_unlocked_element(tmp_path, bm_page, full_page):
    result = play(tmp_path, bm_page, [
        {"select": ["n1"]}, {"look": True},
        {"select": ["n1", "n2"]}, {"look": True},
        {"select": ["frame"]}, {"look": True},
        {"select": ["n2"]}, {"key": "c", "on": "n2"}, {"look": True},
    ])
    one, two, locked, connecting = result["looks"]
    assert one["handles"] == {"hidden": False, "for": "n1"}
    # Only the element with the knob loses its small resize corner.
    assert one["cornerless"] == ["n1"]
    for look in (two, locked, connecting):
        assert look["handles"]["hidden"] is True and look["cornerless"] == []
    # Without BM mode the scene has no handles at all.
    assert play(tmp_path, full_page, [{"select": ["n1"]}, {"look": True}])["looks"][0]["handles"] is None


@needs_node
def test_a_click_on_the_arrow_adds_a_matching_note_below_and_opens_it(tmp_path, bm_page):
    made = play(tmp_path, bm_page, [CLICK, {"look": True}])
    (note,) = made["added"]
    assert {key: note.get(key) for key in ("type", "x", "y", "width", "height", "text_size", "shape", "color")} == {
        "type": "text", "x": 0, "y": 192, "width": 240, "height": 120, "text_size": 15, "shape": "rounded",
        "color": GREEN}
    assert not note.get("content") and ("n1", note["id"]) in _links(made)
    look = made["looks"][0]
    assert look["open"] and look["editing"] == note["id"] and look["title"] == "t:canvas.dialog.edit"
    # The new note is selected: the handles move to it, ready for the next one.
    assert look["handles"] == {"hidden": False, "for": note["id"]}
    # The note and its connection are one step to undo.
    assert made["undo"] == 1
    undone = play(tmp_path, bm_page, [CLICK, {"undo": True}])
    assert undone["added"] == [] and _links(undone) == [("n2", "n3")] and undone["undo"] == 0


@needs_node
def test_clicking_the_arrow_again_puts_the_next_note_beside_the_last(tmp_path, bm_page):
    steps = []
    for word in ("a", "b", "c"):
        steps += [CLICK, {"set": "textarea[name='content']", "value": word}, {"submit": True}]
    result = play(tmp_path, bm_page, steps)
    added = sorted(result["added"], key=lambda node: node["x"])
    assert [(node["x"], node["y"], node["content"]) for node in added] == [(0, 192, "a"), (264, 192, "b"), (528, 192, "c")]
    assert not any(_overlap(a, b) for a in added for b in result["nodes"] if a["id"] != b["id"])
    assert sorted(edge for edge in _links(result) if edge[0] == "n1") == sorted(("n1", node["id"]) for node in added)


@needs_node
def test_on_a_template_column_the_next_note_goes_down_the_column(tmp_path, bm_page):
    column = {"id": "column", "type": "text", "x": -48, "y": -48, "width": 336, "height": 1200, "layer": 0,
              "text_size": 13, "display_text": "Ideas", "locked": True}
    neighbour = {"id": "column2", "type": "text", "x": 312, "y": -48, "width": 336, "height": 1200, "layer": 0,
                 "text_size": 13, "display_text": "Questions", "locked": True}
    steps = []
    for word in ("a", "b"):
        steps += [CLICK, {"set": "textarea[name='content']", "value": word}, {"submit": True}]
    result = play(tmp_path, bm_page, steps, nodes=[column, neighbour, N1])
    assert [(node["x"], node["y"]) for node in result["added"]] == [(0, 192), (0, 336)]


@needs_node
def test_the_new_note_is_brought_into_view(tmp_path, bm_page):
    # An 800x250 stage showing the canvas 100px in from its left edge: the note below n1 (y 192 to 312)
    # is cut off at the bottom, so the view moves up until it shows with a margin, and only up.
    result = play(tmp_path, bm_page, [CLICK], stage=[0, 0, 800, 250], viewport={"x": 100, "y": 0, "zoom": 1})
    assert result["viewport"] == {"x": 100, "y": 250 - 24 - 312, "zoom": 1}


@needs_node
def test_the_knob_resizes_the_selected_element_onto_the_grid(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [{"knob": "n1", "path": [[240, 120], [270, 140], [290, 150]]}])
    n1 = next(node for node in result["nodes"] if node["id"] == "n1")
    assert (n1["x"], n1["y"], n1["width"], n1["height"]) == (0, 0, 288, 144)
    assert result["undo"] == 1 and result["added"] == []


@needs_node
def test_a_wobbly_click_still_counts_as_a_click(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [{"arrow": "n1", "path": [[120, 150], [123, 152], [125, 152]]}])
    assert len(result["added"]) == 1


@needs_node
def test_dragging_the_arrow_onto_an_element_connects_them_once(tmp_path, bm_page):
    drag = {"arrow": "n1", "path": [[120, 150], [300, 100], [600, 60]]}
    result = play(tmp_path, bm_page, [drag, {"look": True}, drag])
    assert _links(result).count(("n1", "n2")) == 1 and result["added"] == [] and result["undo"] == 1
    assert result["targets"][-1] is None and "n2" in result["targets"], "the target is outlined, then let go"
    look = result["looks"][0]
    assert look["open"] is False and look["preview"] is None and look["interaction"] is False


@needs_node
def test_the_element_on_top_is_the_one_connected(tmp_path, bm_page):
    above = {"id": "n4", "type": "text", "x": 600, "y": 60, "width": 240, "height": 120, "layer": 2, "text_size": 13,
             "content": "above"}
    result = play(tmp_path, bm_page, [{"arrow": "n1", "path": [[120, 150], [650, 100]]}], nodes=[*NODES, above])
    assert ("n1", "n4") in _links(result) and ("n1", "n2") not in _links(result)


@needs_node
def test_letting_the_arrow_go_elsewhere_does_nothing(tmp_path, bm_page):
    empty = {"arrow": "n1", "path": [[120, 150], [900, 900], [2000, 2000]]}
    frame = {"arrow": "n1", "path": [[120, 150], [-800, -800]]}
    cancelled = {"arrow": "n1", "path": [[120, 150], [600, 60]], "cancel": True}
    result = play(tmp_path, bm_page, [empty, frame, cancelled, {"look": True}])
    assert _links(result) == [("n2", "n3")] and result["added"] == [] and result["undo"] == 0
    look = result["looks"][0]
    assert look["preview"] is None and look["interaction"] is False and look["open"] is False


@needs_node
def test_letting_the_arrow_go_on_its_own_element_does_nothing(tmp_path, bm_page):
    # n1 sits on a bigger note (a map, say): over n1, the note underneath is not the target.
    under = {"id": "map", "type": "text", "x": -60, "y": -60, "width": 400, "height": 300, "layer": 0, "text_size": 13,
             "content": "map"}
    result = play(tmp_path, bm_page, [{"arrow": "n1", "path": [[120, 150], [120, 60]]}], nodes=[under, *NODES])
    assert _links(result) == [("n2", "n3")] and result["undo"] == 0 and "map" not in result["targets"]


@needs_node
def test_letting_the_arrow_go_outside_the_canvas_does_nothing(tmp_path, bm_page):
    # The stage shows the canvas from its origin, 800x300: n3 (y 600 to 840) is below its bottom edge.
    outside = {"arrow": "n1", "path": [[120, 150], [100, 700]]}
    inside = {"arrow": "n1", "path": [[120, 150], [600, 60]]}
    result = play(tmp_path, bm_page, [outside, inside], stage=[0, 0, 800, 300], viewport={"x": 0, "y": 0, "zoom": 1})
    assert ("n1", "n3") not in _links(result) and ("n1", "n2") in _links(result)


@needs_node
def test_any_other_element_gets_a_plain_note_in_its_colour(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [{"arrow": "n3", "path": [[160, 860]]}])
    (note,) = result["added"]
    assert (note["type"], note["width"], note["height"], note["text_size"], note.get("color")) == ("text", 240, 120, 13, RED)
    assert "shape" not in note and (note["x"], note["y"]) == (40, 912) and ("n3", note["id"]) in _links(result)


@needs_node
def test_shift_enter_is_the_arrow_s_click_from_the_keyboard(tmp_path, bm_page, full_page):
    result = play(tmp_path, bm_page, [{"key": "Enter", "shift": True, "on": "n1"}, {"look": True}])
    (note,) = result["added"]
    assert ("n1", note["id"]) in _links(result) and result["looks"][0]["editing"] == note["id"]
    # Without BM mode's arrow, Shift+Enter is Enter: it opens the element's dialog.
    full = play(tmp_path, full_page, [{"key": "Enter", "shift": True, "on": "n1"}, {"look": True}])
    assert full["added"] == [] and full["looks"][0]["editing"] == "n1"


@needs_node
def test_shift_enter_finishes_a_connection_and_leaves_locked_elements_alone(tmp_path, bm_page):
    # While connecting, Shift+Enter is Enter: it picks the target.
    connecting = play(tmp_path, bm_page, [{"select": ["n2"]}, {"key": "c", "on": "n2"},
                                          {"key": "Enter", "shift": True, "on": "n1"}, {"look": True}])
    assert ("n2", "n1") in _links(connecting) and connecting["added"] == [] and connecting["undo"] == 1
    assert connecting["looks"][0]["open"] is False
    # A locked element has no arrow, and no note comes from it by keyboard either.
    locked = play(tmp_path, bm_page, [{"key": "Enter", "shift": True, "on": "frame"}, {"look": True}])
    assert locked["added"] == [] and locked["looks"][0]["open"] is False
