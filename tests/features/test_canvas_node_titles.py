"""The title bar of canvas elements: what someone wrote in the Title field, a wiki page's title or a code
block's first line, never the address of an image, video or link that has no title.

canvas-core.js draws each element with its real fillNode under node (tests/features/mini_dom.js stands
in for the browser); skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
const document = new dom.Document("<!doctype html><html><body></body></html>");
document.createElementNS = (ns, tag) => document.createElement(tag);
const BW = { t: (key) => "t:" + key };
const window = dom.makeWindow(document, { BW: BW });
const context = vm.createContext({ window: window, document: document, BW: BW, console: console, URL: URL });
vm.runInContext(fs.readFileSync(scenario.static + "/canvas-core.js", "utf8"), context, { filename: "canvas-core.js" });
// The scene as the editor has it, without a stage: only the drawing of one element is used.
const scene = Object.create(BW.Canvas.Scene.prototype);
Object.assign(scene, { options: { editable: true }, pages: scenario.pages, elements: {} });
const drawn = scenario.nodes.map((node) => {
  const element = document.createElement("div");
  element.style = { removeProperty() {}, setProperty() {} };
  scene.fillNode(element, node, null);
  const bar = element.querySelector(".cv-node__title");
  return { id: node.id, title: bar ? bar.textContent : null, name: element.getAttribute("aria-label") };
});
process.stdout.write(JSON.stringify({ drawn: drawn, errors: dom.errors }));
"""

PICTURE = "https://upload.wikimedia.org/wikipedia/commons/italia.png"
NODES = [
    # What the element dialog saves for an image, a video and a link left without a title: the label is
    # the start of the address.
    {"id": "image", "type": "image", "x": 0, "y": 0, "url": PICTURE, "label": PICTURE},
    {"id": "video", "type": "video", "x": 0, "y": 0, "url": "https://example.org/clip.mp4", "label": "https://example.org/clip.mp4"},
    {"id": "link", "type": "external_link", "x": 0, "y": 0, "url": "https://example.org/", "label": "https://example.org/"},
    {"id": "named", "type": "image", "x": 0, "y": 0, "url": PICTURE, "label": "Italia", "display_text": "Italia"},
    {"id": "page", "type": "wiki_page", "x": 0, "y": 0, "page_id": 7, "label": "Regioni"},
    {"id": "code", "type": "code", "x": 0, "y": 0, "content": "print('ciao')\nprint('mondo')"},
    {"id": "note", "type": "text", "x": 0, "y": 0, "content": "Una nota"},
    {"id": "titled", "type": "text", "x": 0, "y": 0, "content": "Una nota", "display_text": "Titolo"},
]
PAGES = {"7": {"id": 7, "title": "Regioni", "slug": "regioni"}}


def _draw(tmp_path):
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps({"static": str(STATIC), "nodes": NODES, "pages": PAGES}),
                                            encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == []
    return {item["id"]: item for item in result["drawn"]}


def test_images_videos_and_links_show_a_title_only_when_one_is_written(tmp_path):
    drawn = _draw(tmp_path)
    assert drawn["image"]["title"] is None and drawn["video"]["title"] is None and drawn["link"]["title"] is None
    assert drawn["named"]["title"] == "Italia"
    # The address still names the element to screen readers.
    assert drawn["image"]["name"] == "t:node.image: " + PICTURE


def test_pages_code_and_notes_keep_their_titles(tmp_path):
    drawn = _draw(tmp_path)
    assert drawn["page"]["title"] == "Regioni" and drawn["code"]["title"] == "print('ciao')"
    assert drawn["note"]["title"] is None and drawn["titled"]["title"] == "Titolo"
