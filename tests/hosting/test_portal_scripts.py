"""The portal's own scripts for the easter eggs and the "Go to" menu, run under node on rendered pages
(tests/features/mini_dom.js stands in for the browser)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.core import eggs

ROOT = Path(__file__).parents[2]
MINI_DOM = ROOT / "tests/features/mini_dom.js"
RUNTIME = ROOT / "bananawiki/core/static/eggs/banana-eggs.js"
PAGE_SCRIPT = ROOT / "bananawiki/hosting/static/js/easter-egg.js"
HOSTING = ROOT / "bananawiki/hosting/static/js/hosting.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const document = new dom.Document(input.html);
const stored = new Map(Object.entries(input.stored || {}));
const localStorage = { getItem: (k) => (stored.has(k) ? stored.get(k) : null), setItem: (k, v) => stored.set(k, String(v)) };
const BW = { onReady: (fn) => fn() };
const window = dom.makeWindow(document, { localStorage, BW, setTimeout, clearTimeout, document,
                                          location: { href: "https://portal.test/", origin: "https://portal.test" },
                                          matchMedia: () => ({ matches: false }) });
for (const script of input.scripts) {
  try {
    vm.runInNewContext(fs.readFileSync(script, "utf8"), { window, document, URL, BW }, { filename: script });
  } catch (error) {
    // hosting.js wires every portal behaviour; only the ones this page has matter here.
    if (!input.lenient) throw error;
  }
}
const out = {};
const goto = document.querySelector("details[data-goto-menu]");
if (goto) {
  const toggle = goto.querySelector("summary");
  const links = goto.querySelectorAll("a");
  const key = (target, k) => target.dispatchEvent(new dom.Event("keydown", { bubbles: true, props: { key: k, target } }));
  out.goto = [[toggle.getAttribute("aria-expanded"), toggle.getAttribute("aria-controls")]];
  goto.open = true; goto.dispatchEvent(new dom.Event("toggle"));
  out.goto.push([toggle.getAttribute("aria-expanded"), goto.open]);
  goto.open = false; goto.dispatchEvent(new dom.Event("toggle"));
  toggle.focus();
  key(toggle, "ArrowDown");
  out.goto.push([toggle.getAttribute("aria-expanded"), goto.open, document.activeElement === links[0]]);
  key(links[0], "ArrowUp");
  out.goto.push([document.activeElement === links[links.length - 1]]);
  key(document.activeElement, "Escape");
  out.goto.push([toggle.getAttribute("aria-expanded"), goto.open, document.activeElement === toggle]);
}
const page = document.querySelector("[data-easter-egg-page]");
if (page) {
  out.count = page.querySelector("[data-easter-egg-count]").textContent;
  out.eggs = page.querySelectorAll("[data-egg-id]").map((li) => li.textContent.replace(/\s+/g, " ").trim());
}
process.stdout.write(JSON.stringify(out));
"""


def _run(tmp_path, html, scripts, *, stored=None, lenient=False):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "input.json"
    data.write_text(json.dumps({"html": html, "scripts": [str(s) for s in scripts], "stored": stored or {},
                                "lenient": lenient}), encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(MINI_DOM), str(data)], capture_output=True, text=True,
                          timeout=60, check=True)
    return json.loads(done.stdout)


def test_visitors_see_the_eggs_found_in_their_browser(tmp_path, web, query):
    config = {"version": 1, "eggs": [{**eggs.default_egg(), "id": "one", "keys": ["x", "y"]},
                                     {**eggs.default_egg(), "id": "two", "keys": ["x", "z"],
                                      "sprite": {"type": "emoji", "value": "🍕"}}]}
    query("UPDATE hosting_settings SET easter_egg_configs = ? WHERE id = 1", (eggs.dumps(config),))
    html = web.get("/easter-egg").get_data(as_text=True)
    finds = json.dumps({"two": "2026-05-01T10:00:00.000Z", "elsewhere": "2026-05-01T10:00:00.000Z"})
    result = _run(tmp_path, html, [RUNTIME, PAGE_SCRIPT], stored={"banana-eggs.found": finds})
    assert result["count"] == "1 of 2 found"
    assert result["eggs"][0] == "? Not found yet"
    assert result["eggs"][1].startswith("🍕 Found in this browser: two")


def test_the_go_to_menu_tells_its_state_and_answers_the_keyboard(tmp_path, web, query):
    query("UPDATE hosting_settings SET goto_chat_url = 'https://chat.example.org', "
          "goto_git_url = 'https://git.example.org' WHERE id = 1")
    html = web.get("/login").get_data(as_text=True)
    result = _run(tmp_path, html, [HOSTING], lenient=True)
    assert result["goto"] == [
        ["false", "goto-menu-list"],  # closed, and naming the list it opens
        ["true", True],  # opened by the browser (the <summary> click)
        ["true", True, True],  # ArrowDown on the toggle opens it at the first link
        [True],  # ArrowUp from the first link wraps to the last
        ["false", False, True],  # Escape closes it and returns to the toggle
    ]
