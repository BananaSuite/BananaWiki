"""Canvases with BM mode's bigger controls: named tools in tinted groups, shapes and colours as pictures.

The page tests read what the server renders. The dialog tests run canvas-editor.js under node
(tests/features/mini_dom.js stands in for the browser, with a stand-in for the server's /data and
/ops and a scene that draws nothing); they are skipped where node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.wiki.features.canvas import model, service

from .test_bm_mode import _save
from .test_canvas_bm import _canvas_page, _simplified

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

STATIC = Path(service.__file__).parent / "static"
MINI_DOM = Path(__file__).with_name("mini_dom.js")
PALETTE = ("#e91e63", "#ff9800", "#ffeb3b", "#4caf50", "#2196f3", "#9c27b0", "#9e9e9e")
GROUPS = ("add", "change", "arrange", "history", "view")


@pytest.fixture
def easy(app_factory):
    return app_factory(environ={"BW_EASY_WIKI": "1"})


@pytest.fixture(params=["wiki", "account"])
def bm_page(request, app, easy, make_user, login):
    """The canvas page for an editor in BM mode: the whole wiki's, or their account's only."""
    if request.param == "wiki":
        return _canvas_page(easy, make_user, login, "bigwiki")
    return _canvas_page(app, make_user, login, "bigaccount", bm_mode=1)


@pytest.fixture
def full_page(app, make_user, login):
    return _canvas_page(app, make_user, login, "bigfull")


def _radios(page, name):
    return re.findall(rf'<input class="cv-choice__input" type="radio" name="{name}" value="([^"]*)">', page)


def test_bigger_controls_mark_the_page_and_tag_every_tool_group(bm_page):
    assert "canvas.big_controls" in _simplified(bm_page)
    # The arrange menu is left out by the fewer tools; every other group says what its tools do.
    assert re.findall(r'class="canvas-toolbar__group"[^>]*\sdata-tools="([^"]+)"', bm_page) == [
        "add", "change", "history", "view"]


def test_bigger_controls_offer_shapes_and_colours_as_pictures(bm_page):
    assert 'id="cn-shape"' not in bm_page and 'id="cn-color"' not in bm_page
    assert _radios(bm_page, "shape") == list(model.SHAPES)
    assert bm_page.count('<svg class="cv-choice__shape"') == len(model.SHAPES)
    # No colour, the palette, and a hidden choice that keeps a colour from outside it.
    assert _radios(bm_page, "color") == ["", *PALETTE, ""]
    assert re.search(r'<label class="cv-choice" data-choice-other hidden>', bm_page)
    for value in PALETTE:
        assert f'style="--swatch: {value}"' in bm_page
    # The shape pictures go with text nodes only, like the list they replace.
    assert re.search(r'<fieldset class="cv-choices" data-field="shape">\s*<legend>', bm_page)


def test_the_full_canvas_keeps_the_lists(full_page):
    assert "canvas.big_controls" not in _simplified(full_page)
    assert '<select id="cn-shape" name="shape">' in full_page and '<select id="cn-color" name="color">' in full_page
    assert 'class="cv-choices"' not in full_page and _radios(full_page, "color") == []
    # The groups' kinds are only names: without the bigger controls nothing styles them.
    assert re.findall(r'\sdata-tools="([^"]+)"', full_page) == list(GROUPS)


def test_only_the_editor_makes_room_for_the_bigger_toolbar():
    # canvas.css also styles canvases embedded in pages and history previews, which have no toolbar.
    css = (STATIC / "canvas.css").read_text(encoding="utf-8")
    rules = re.findall(r'html\[data-bm~="canvas\.big_controls"\][^{]*\{[^}]*\}', css)
    frames = [rule.split("{")[0].strip() for rule in rules if ".canvas-frame" in rule.split("{")[0]]
    assert frames and all(selector.endswith(".canvas-app > .canvas-frame") for selector in frames)


def test_switching_bigger_controls_off_brings_the_lists_back(easy, admin, make_user, login):
    chief = easy.test_client()
    login(chief, admin)
    assert _save(chief, mode=True, features=("canvas",), off=("canvas.big_controls",)).status_code == 302
    page = _canvas_page(easy, make_user, login, "bigoff")
    assert "canvas.big_controls" not in _simplified(page) and "canvas.simple_tools" in _simplified(page)
    assert '<select id="cn-color" name="color">' in page and _radios(page, "color") == []


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

const dialog = document.querySelector("dialog#canvas-node-dialog");
const form = dialog.querySelector("form[data-node-form]");
const radios = (name) => form.querySelectorAll('input[type="radio"][name="' + name + '"]');

// What the dialog shows: the picked picture, or the list's value in the full editor.
function look() {
  const choice = (name) => radios(name).length ?
    { pictures: radios(name).filter((radio) => radio.checked).map((radio) => radio.value) } :
    { list: form.querySelector('select[name="' + name + '"]').value };
  const other = form.querySelector("[data-choice-other]");
  return {
    open: dialog.open, shape: choice("shape"), color: choice("color"),
    shape_hidden: form.querySelector('[data-field="shape"]').hidden,
    other: other && { hidden: other.hidden, value: other.querySelector("input").value,
                      swatch: other.querySelector(".cv-swatch").getAttribute("style") },
  };
}

(async () => {
  await flush();
  const editor = BW.canvasEditor;
  const looks = [];
  for (const step of scenario.steps) {
    if (step.run) editor.run(step.run);
    else if (step.edit) editor.openNodeDialog(step.edit);
    else if (step.pick) {
      // As a click on a picture does: the one radio button of its group that is checked.
      const target = radios(step.pick).filter((radio) => radio.value === step.value && !radio.closest("[data-choice-other]"))[0];
      if (!target) throw new Error("no picture for " + step.pick + " " + step.value);
      radios(step.pick).forEach((radio) => { radio.checked = radio === target; });
    }
    else if (step.set) form.querySelector(step.set).value = step.value;
    else if (step.submit) form.dispatchEvent(new dom.Event("submit", { bubbles: true }));
    else if (step.look) looks.push(look());
    else throw new Error("unknown step " + JSON.stringify(step));
    await flush();
  }
  return { looks: looks, nodes: copy(editor.scene.nodes), errors: dom.errors };
})().then((result) => process.stdout.write(JSON.stringify(result)),
          (error) => process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })));
"""

NODES = [
    {"id": "n1", "type": "text", "x": 0, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "one", "shape": "diamond", "color": "#4caf50"},
    {"id": "n2", "type": "text", "x": 480, "y": 0, "width": 240, "height": 120, "layer": 1, "text_size": 13,
     "content": "two", "color": "#123456"},
    {"id": "n3", "type": "image", "x": 0, "y": 240, "width": 240, "height": 180, "layer": 1, "text_size": 13,
     "url": "https://example.org/map.png"},
]


def play(tmp_path, page, steps):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps({
        "html": page, "static": str(STATIC), "steps": steps, "doc": {"nodes": NODES, "edges": []}}), encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result.get("crashed") is None, result.get("crashed")
    assert result["errors"] == []
    return result


def _node(result, node_id):
    return next(node for node in result["nodes"] if node["id"] == node_id)


@needs_node
def test_the_pictures_show_and_save_a_node_s_shape_and_colour(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [
        {"edit": "n1"}, {"look": True},
        {"pick": "shape", "value": "hexagon"}, {"pick": "color", "value": "#2196f3"}, {"submit": True}, {"look": True},
        {"edit": "n3"}, {"look": True},
    ])
    opened, saved, image = result["looks"]
    assert opened["shape"] == {"pictures": ["diamond"]} and opened["color"] == {"pictures": ["#4caf50"]}
    assert opened["other"]["hidden"] is True and opened["shape_hidden"] is False
    assert saved["open"] is False
    assert _node(result, "n1")["shape"] == "hexagon" and _node(result, "n1")["color"] == "#2196f3"
    assert image["shape_hidden"] is True and image["color"] == {"pictures": [""]}


@needs_node
def test_a_colour_outside_the_palette_is_shown_and_kept(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [
        {"edit": "n2"}, {"look": True}, {"submit": True},
        # Opened again, the extra choice still holds the colour: it must not be taken for the colour itself.
        {"edit": "n2"}, {"look": True}, {"submit": True},
        {"edit": "n1"}, {"look": True},
    ])
    own, again, palette = result["looks"]
    for look in (own, again):
        assert look["color"] == {"pictures": ["#123456"]}
        assert look["other"] == {"hidden": False, "value": "#123456", "swatch": "--swatch: #123456"}
    assert _node(result, "n2")["color"] == "#123456", "saving without a change keeps the colour"
    # The next node's colour is in the palette: the extra choice goes away again.
    assert palette["color"] == {"pictures": ["#4caf50"]}
    assert palette["other"] == {"hidden": True, "value": "", "swatch": ""}


@needs_node
def test_no_colour_and_the_plain_rectangle_save_nothing(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [
        {"edit": "n1"}, {"pick": "shape", "value": "rectangle"}, {"pick": "color", "value": ""}, {"submit": True},
    ])
    node = _node(result, "n1")
    assert "shape" not in node and "color" not in node


@needs_node
def test_the_shape_tool_starts_on_the_rounded_picture(tmp_path, bm_page):
    result = play(tmp_path, bm_page, [{"run": "add-shape"}, {"look": True}, {"submit": True}])
    assert result["looks"][0]["shape"] == {"pictures": ["rounded"]}
    assert result["looks"][0]["color"] == {"pictures": [""]}
    added = [node for node in result["nodes"] if node["id"] not in ("n1", "n2", "n3")]
    assert len(added) == 1 and added[0]["shape"] == "rounded" and "color" not in added[0]


@needs_node
def test_the_full_editor_still_uses_the_lists(tmp_path, full_page):
    result = play(tmp_path, full_page, [
        {"edit": "n1"}, {"look": True},
        {"set": "select[name='color']", "value": "#2196f3"}, {"set": "select[name='shape']", "value": "pill"},
        {"submit": True},
    ])
    assert result["looks"][0]["shape"] == {"list": "diamond"} and result["looks"][0]["color"] == {"list": "#4caf50"}
    assert _node(result, "n1")["shape"] == "pill" and _node(result, "n1")["color"] == "#2196f3"
