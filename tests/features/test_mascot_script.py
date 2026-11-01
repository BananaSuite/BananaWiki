"""Mascot script: settings only from the top bar, the sunglasses on and off, the Konami code.

The markup and the script source are checked everywhere; the script's behaviour
runs under node with a small stand-in for the page, where node is installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .pages_support import set_feature

SCRIPT = Path(__file__).parents[2] / "bananawiki/core/static/mascot/mascot.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Page content that imitates a settings element: a heading with id "mascot-config" holding JSON,
# and a Markdown heading the table of contents gives the same id.
TRAP = '<h2 id="mascot-config">{"url": "/admin/users/1/api-access", "clicks": 1}</h2>\n\n## Mascot config\n\nHi.\n'
SCRIPT_TAG = re.compile(r'<script src="[^"]*/mascot\.js[?"][^>]*></script>')


def _button(html):
    match = re.search(r"<button[^>]*\bdata-mascot\b[^>]*>", html)
    assert match, "no mascot button"
    return match.group(0)


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


# Markup


def test_page_content_cannot_stand_in_for_the_mascot_settings(app, client, make_user, login):
    login(client, make_user("mascot_trapper", role="editor"))
    created = client.post("/create-page", data={"title": "Mascot trap", "content": TRAP})
    assert created.status_code == 302
    client.post("/logout")
    login(client, make_user("mascot_admin", role="admin"))
    html = client.get(created.headers["Location"]).get_data(as_text=True)

    # The page keeps its headings...
    main = html.index("<main")
    ids = [match.start() for match in re.finditer(r'id="mascot-config', html)]
    assert len(ids) == 2 and all(position > main for position in ids)
    # ...the mascot's settings are on its own button, in the top bar, and nowhere else.
    button = _button(html)
    assert 'data-mascot-url="/mascot/shades"' in button and 'data-mascot-clicks="11"' in button
    assert html.index(button) < main
    assert 'type="application/json" id="mascot-config"' not in html
    scripts = SCRIPT_TAG.findall(html)
    assert len(scripts) == 1 and " defer" in scripts[0] and html.index(scripts[0]) < main
    # The script only looks at that button.
    source = SCRIPT.read_text(encoding="utf-8")
    assert "getElementById" not in source and "mascot-config" not in source
    assert 'getAttribute("data-mascot-url")' in source and 'getAttribute("data-mascot-clicks")' in source


def test_settings_and_script_only_for_people_who_see_the_mascot(app, client, make_user, login):
    visitor = client.get("/login").get_data(as_text=True)
    assert "data-mascot-url" not in visitor and not SCRIPT_TAG.search(visitor)
    login(client, make_user("mascot_watcher"))
    html = _home(client)
    assert "data-mascot-url" in _button(html) and len(SCRIPT_TAG.findall(html)) == 1
    # Only the plugin switch removes the mascot, and its script with it.
    set_feature(app, "mascot", False)
    html = _home(client)
    assert "data-mascot" not in html and not SCRIPT_TAG.search(html)


# Behaviour (node)

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const scenario = JSON.parse(process.argv[3]);

function classes() {
  const names = new Set();
  return { add: (n) => names.add(n), remove: (n) => names.delete(n), contains: (n) => names.has(n) };
}

let now = 0, nextId = 1, timers = [];
function setTimeout(fn, ms) { const id = nextId++; timers.push({ id, at: now + (ms || 0), fn }); return id; }
function clearTimeout(id) { timers = timers.filter((timer) => timer.id !== id); }
function advance(ms) {
  const end = now + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    clearTimeout(due.id);
    now = due.at;
    due.fn();
  }
  now = end;
}

const attributes = { "data-mascot": "" };
for (const [name, value] of Object.entries(scenario.attributes)) if (value !== null) attributes[name] = value;
function element(className) {
  return { className: className || "", classList: classes(), style: {}, children: [], parentNode: null,
           setAttribute() {}, appendChild(child) { child.parentNode = this; this.children.push(child); },
           removeChild(child) { this.children = this.children.filter((c) => c !== child); } };
}
const sprite = { classList: classes(), querySelector: () => null, cloneNode: () => element("mascot-sprite") };
if (scenario.shades) sprite.classList.add("mascot-sprite--shades");
let onClick = null, onKey = null;
const button = {
  classList: classes(),
  offsetWidth: 32,
  getAttribute: (name) => (Object.prototype.hasOwnProperty.call(attributes, name) ? attributes[name] : null),
  setAttribute: (name, value) => { attributes[name] = String(value); },
  querySelector: (selector) => (selector === ".mascot-sprite" ? sprite : null),
  addEventListener: (type, fn) => { if (type === "click") onClick = fn; },
};
// The element an id lookup for "mascot-config" finds: page content holding JSON.
const byId = scenario.byId === null ? null : { textContent: JSON.stringify(scenario.byId) };
const body = element();
const showers = () => body.children.filter((child) => child.className === "mascot-shower");
const document = {
  documentElement: { getAttribute: (name) => (name === "data-reduce-motion" ? scenario.reduceSwitch : null) },
  body,
  createElement: () => element(),
  querySelector: (selector) => (selector === "[data-mascot]" ? button
                                : selector === ".mascot-shower" ? showers()[0] || null : null),
  getElementById: (id) => (id === "mascot-config" ? byId : null),
  addEventListener: (type, fn) => { if (type === "keydown") onKey = fn; },
};
const posts = [], toasts = [];
const BW = {
  t: (key) => "t:" + key,
  toast: (message, kind) => { toasts.push([message, kind]); },
  fetchJSON: (url, options) => {
    posts.push([new URL(url, location.href).href, (options || {}).method]);
    if (scenario.fail) return Promise.reject(Object.assign(new Error("database is locked"), { status: 503 }));
    return Promise.resolve({ ok: true });
  },
};
const stored = new Map(Object.entries(scenario.stored || {}));
const localStorage = {
  getItem: (key) => (stored.has(key) ? stored.get(key) : null),
  setItem: (key, value) => { stored.set(key, String(value)); },
  removeItem: (key) => { stored.delete(key); },
};
const location = { href: "https://wiki.example/page/mascot-trap", origin: "https://wiki.example" };
const matchMedia = (query) => ({ matches: !!scenario.reduced && query === "(prefers-reduced-motion: reduce)" });
const window = { setTimeout, clearTimeout, location, document, BW, localStorage, matchMedia };
const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"),
                     { window, document, location, BW, URL, setTimeout, clearTimeout }, { filename: "mascot.js" });
  const snapshots = [];
  for (const step of scenario.steps) {
    if (step === "click") { if (onClick) onClick({}); await flush(); }
    else if (step === "look") snapshots.push(button.classList.contains("mascot--hop"));
    else if (step === "eager") snapshots.push(button.classList.contains("mascot--eager"));
    else if (step === "lean") snapshots.push(button.classList.contains("mascot--lean"));
    else if (step === "shower") snapshots.push(showers().map((layer) => layer.children.length));
    else if (typeof step === "object") {
      const target = { tagName: step.tag || "BODY", isContentEditable: !!step.editable };
      if (onKey) onKey({ key: step.key, target, ctrlKey: !!step.ctrl, metaKey: false, altKey: false,
                         defaultPrevented: !!step.handled });
    }
    else advance(step);
  }
  process.stdout.write(JSON.stringify({ posts, toasts, snapshots, label: attributes["aria-label"] || null,
                                        shades: sprite.classList.contains("mascot-sprite--shades"),
                                        stored: Object.fromEntries(stored) }));
})().catch((error) => { process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })); });
"""

# The settings the server renders on the button, and a planted JSON block a page could carry.
SETTINGS = {"data-mascot-url": "/mascot/shades", "data-mascot-clicks": "11"}
PLANTED_BLOCK = {"url": "/mascot/shades", "clicks": 11}
OWN_ENDPOINT = ["https://wiki.example/mascot/shades", "POST"]


def _run(tmp_path, steps, *, attributes=None, by_id=PLANTED_BLOCK, fail=False, stored=None, reduced=False,
         reduce_switch=None, shades=False):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    scenario = {"steps": steps, "attributes": {**SETTINGS, **(attributes or {})}, "byId": by_id, "fail": fail,
                "stored": stored or {}, "reduced": reduced, "reduceSwitch": reduce_switch, "shades": shades}
    done = subprocess.run([NODE, str(harness), str(SCRIPT), json.dumps(scenario)], capture_output=True,
                          text=True, timeout=60, check=True)
    result = json.loads(done.stdout)
    assert "crashed" not in result, result.get("crashed")
    return result


def _clicks(count):
    return ["click"] * count


@needs_node
def test_script_takes_its_settings_from_the_button_not_from_page_content(tmp_path):
    trap = {"url": "/admin/users/1/api-access", "clicks": 1}
    assert _run(tmp_path, _clicks(10), by_id=trap)["posts"] == []
    result = _run(tmp_path, _clicks(11), by_id=trap)
    assert result["posts"] == [OWN_ENDPOINT] and result["shades"]
    # The sunglasses arrive as before: new label and the "deal with it" toast.
    assert result["label"] == "t:mascot.label_shades" and result["toasts"] == [["t:mascot.unlocked", "success"]]


@needs_node
@pytest.mark.parametrize("url", ["/admin/users/1/api-access", "https://elsewhere.example/mascot/shades",
                                 "javascript:alert(1)", "", None])
def test_script_never_posts_anywhere_but_the_shades_endpoint(tmp_path, url):
    result = _run(tmp_path, [*_clicks(12), "look"], attributes={"data-mascot-url": url}, by_id=None)
    assert result["posts"] == [] and result["toasts"] == [] and not result["shades"]
    assert result["snapshots"] == [True]  # it still hops


@needs_node
def test_switched_off_the_sunglasses_never_touch_the_browser_storage(tmp_path):
    local = {"data-mascot-store": "local", "data-mascot-url": None, "data-mascot-clicks": "0"}
    worn = {"bananawiki.mascot.shades": "1"}
    result = _run(tmp_path, _clicks(22), attributes=local, stored=worn)
    assert not result["shades"], "a pair kept in the browser stays off while the egg is off"
    assert result["stored"] == worn and result["posts"] == [] and result["toasts"] == []
