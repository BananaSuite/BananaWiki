"""banana-eggs.js, the BananaSuite easter egg runtime (core/static/eggs/), run under node on rendered markup.

tests/features/mini_dom.js stands in for the page; the harness adds what the runtime needs besides
(inline styles, the default value of a field, timers, storage, fetch and navigation).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.core import eggs

SCRIPT = Path(__file__).parents[2] / "bananawiki/core/static/eggs/banana-eggs.js"
MINI_DOM = Path(__file__).with_name("mini_dom.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const scenario = JSON.parse(process.argv[4]);

// What mini_dom leaves out and the runtime uses.
Object.defineProperty(dom.Element.prototype, "style", {
  get() { return this._style || (this._style = {}); }, configurable: true });
Object.defineProperty(dom.Element.prototype, "defaultValue", {
  get() { return this.localName === "textarea" ? this.textContent : (this.getAttribute("value") || ""); },
  configurable: true });
Object.defineProperty(dom.Element.prototype, "defaultChecked", {
  get() { return this.hasAttribute("checked"); }, configurable: true });
Object.defineProperty(dom.Element.prototype, "defaultSelected", {
  get() { return this.hasAttribute("selected"); }, configurable: true });
Object.defineProperty(dom.Element.prototype, "isContentEditable", {
  get() { return this.getAttribute("contenteditable") === "true"; }, configurable: true });

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

const document = new dom.Document(scenario.html);
const fetches = [];
const stored = new Map(Object.entries(scenario.stored || {}));
const localStorage = {
  getItem: (key) => (stored.has(key) ? stored.get(key) : null),
  setItem: (key, value) => { stored.set(key, String(value)); },
  removeItem: (key) => { stored.delete(key); },
};
const location = { href: "https://wiki.example/page/home", origin: "https://wiki.example" };
const matchMedia = (query) => ({ matches: !!scenario.reduced && query === "(prefers-reduced-motion: reduce)" });
function fetch(url, options) {
  fetches.push({ url, method: options.method, headers: options.headers, body: options.body });
  return Promise.resolve({ ok: true });
}
const window = dom.makeWindow(document, { setTimeout, clearTimeout, location, localStorage, matchMedia, fetch, document });
const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[3], "utf8"), { window, document, URL, setTimeout, clearTimeout },
                     { filename: "banana-eggs.js" });
  const snapshots = [];
  const items = () => document.querySelectorAll(".banana-eggs-item");
  for (const step of scenario.steps) {
    if (typeof step === "number") advance(step);
    else if (step.key !== undefined) {
      const target = step.target ? document.querySelector(step.target) : document.body;
      document.dispatchEvent(new dom.Event("keydown", { props: {
        key: step.key, target, repeat: !!step.repeat, shiftKey: !!step.shift, ctrlKey: !!step.ctrl,
        metaKey: false, altKey: false, isComposing: false } }));
    } else if (step.set) document.querySelector(step.set).value = step.value;
    else if (step.preview) snapshots.push(window.BananaEggs.preview(step.preview));
    else if (step.look) {
      const toast = document.querySelector(".banana-eggs-toast");
      const link = toast && toast.querySelector("a");
      snapshots.push({ items: items().length,
                       still: items().filter((n) => n.classList.contains("banana-eggs-item--still")).length,
                       toast: toast ? toast.textContent : null, link: link ? (link.getAttribute("href") || link.href) : null });
    }
    await flush();
  }
  process.stdout.write(JSON.stringify({ fetches, snapshots, href: location.href, stored: Object.fromEntries(stored),
                                        api: typeof window.BananaEggs, errors: dom.errors }));
})().catch((error) => { process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })); });
"""

KONAMI = [{"key": key} for key in eggs.KONAMI]
FOUND_URL = "/easter-eggs/{id}/found"


def _egg(egg_id="party", keys=("p", "a", "r", "t", "y"), **values):
    return eggs.clean({"version": 1, "eggs": [{"id": egg_id, "keys": list(keys), "count": 5, "delay": 50,
                                                "speed_min": 1, "speed_max": 1.5, **values}]})["eggs"][0]


def _page(*items, signed_in=True, reduce=False, where="head", found_url=FOUND_URL, body="", html_attrs=""):
    config = eggs.client_config({"eggs": list(items) or eggs.default_config()["eggs"]},
                                found_url=found_url if signed_in else None, page_url="/easter-egg",
                                signed_in=signed_in, csrf_header="X-CSRF-Token" if signed_in else None,
                                csrf_token="tok", reduce_motion=reduce,
                                strings={"found": "Found it!", "open_page": "Your eggs"})
    block = f'<script type="application/json" id="banana-eggs">{eggs.script_json(config)}</script>'
    head, content = (block, "") if where == "head" else ("", block)
    return (f"<!doctype html><html{html_attrs}><head><title>t</title>{head}</head>"
            f"<body><main>{content}<p>Text</p>{body}</main></body></html>")


def _run(tmp_path, html, steps, **extra):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    scenario = {"html": html, "steps": steps, **extra}
    done = subprocess.run([NODE, str(harness), str(MINI_DOM), str(SCRIPT), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(done.stdout)
    assert "crashed" not in result, result.get("crashed")
    assert result["errors"] == []
    return result


def _keys(text):
    return [{"key": key} for key in text]


def test_the_default_egg_showers_and_records_the_find(tmp_path):
    result = _run(tmp_path, _page(), [*KONAMI, 2000, {"look": True}, 6000, {"look": True}])
    assert result["snapshots"][0]["items"] == 10 and result["snapshots"][0]["toast"] is None
    assert result["snapshots"][1]["items"] == 0, "every item goes once it has fallen"
    assert result["fetches"] == [{"url": "/easter-eggs/konami/found", "method": "POST", "body": "{}",
                                  "headers": {"Accept": "application/json", "Content-Type": "application/json",
                                              "X-CSRF-Token": "tok"}}]
    assert result["href"] == "https://wiki.example/page/home", "the default egg does nothing afterwards"


def test_keys_typed_in_a_field_never_count(tmp_path):
    steps = [{**step, "target": "input"} for step in KONAMI]
    result = _run(tmp_path, _page(body='<input value="">'), [*steps, 500, {"look": True}])
    assert result["snapshots"] == [{"items": 0, "still": 0, "toast": None, "link": None}] and result["fetches"] == []


def test_shift_alone_is_ignored_and_capitals_count_as_small_letters(tmp_path):
    steps = [{"key": "Shift"}, {"key": "P", "shift": True}, {"key": "Shift"}, {"key": "A", "shift": True},
             *_keys("rty"), 100, {"look": True}]
    result = _run(tmp_path, _page(_egg()), steps)
    assert result["snapshots"][0]["items"] > 0 and len(result["fetches"]) == 1


def test_held_keys_and_shortcuts_do_not_count(tmp_path):
    repeated = [{"key": "p"}, {"key": "a", "repeat": True}, *_keys("rty"), 100, {"look": True}]
    assert _run(tmp_path, _page(_egg()), repeated)["fetches"] == []
    shortcut = [*_keys("pa"), {"key": "r", "ctrl": True}, *_keys("ty"), 100, {"look": True}]
    assert _run(tmp_path, _page(_egg()), shortcut)["fetches"] == []


@pytest.mark.parametrize("how", ["config", "page", "system"])
def test_with_less_motion_one_still_item_appears(tmp_path, how):
    html = _page(_egg(), reduce=how == "config",
                 html_attrs=' data-reduce-motion="true"' if how == "page" else "")
    result = _run(tmp_path, html, [*_keys("party"), 100, {"look": True}], reduced=how == "system")
    assert result["snapshots"][0]["items"] == 1 and result["snapshots"][0]["still"] == 1
    assert len(result["fetches"]) == 1


def test_the_config_is_only_read_from_the_head(tmp_path):
    result = _run(tmp_path, _page(_egg(), where="body"), [*_keys("party"), 500, {"look": True}])
    assert result["snapshots"][0]["items"] == 0 and result["fetches"] == []
    assert result["api"] == "object", "the editor's preview still works"


def test_the_easter_egg_page_opens_at_the_egg(tmp_path):
    result = _run(tmp_path, _page(_egg(unlock={"action": "page", "delay": 100})), [*_keys("party"), 3000])
    assert result["href"] == "/easter-egg#egg-party"


def test_unsaved_edits_turn_the_page_into_a_message_with_a_link(tmp_path):
    html = _page(_egg(unlock={"action": "page", "delay": 100}), body='<input class="title" value="Draft">')
    result = _run(tmp_path, html, [{"set": "input", "value": "Draft, edited"}, *_keys("party"), 1000,
                                   {"look": True}])
    assert result["href"] == "https://wiki.example/page/home"
    assert result["snapshots"][0]["toast"] == "Found it! Your eggs" and result["snapshots"][0]["link"] == "/easter-egg"


def test_a_message_egg_shows_its_own_message(tmp_path):
    html = _page(_egg(unlock={"action": "toast", "delay": 100}, message="Party time"))
    result = _run(tmp_path, html, [*_keys("party"), 1000, {"look": True}])
    assert result["snapshots"][0]["toast"] == "Party time" and result["href"] == "https://wiki.example/page/home"


def test_signed_out_finds_stay_in_the_browser(tmp_path):
    result = _run(tmp_path, _page(_egg(), signed_in=False), [*_keys("party"), 100, *_keys("party"), 4000])
    assert result["fetches"] == []
    assert list(json.loads(result["stored"]["banana-eggs.found"])) == ["party"]


def test_a_found_address_on_another_site_is_never_called(tmp_path):
    html = _page(_egg(), found_url="https://elsewhere.example/steal/{id}")
    result = _run(tmp_path, html, [*_keys("party"), 100])
    assert result["fetches"] == [] and "party" in json.loads(result["stored"]["banana-eggs.found"])


def test_an_egg_that_is_not_recorded_is_never_sent(tmp_path):
    result = _run(tmp_path, _page(_egg(record=False)), [*_keys("party"), 100])
    assert result["fetches"] == [] and result["stored"] == {}


def test_the_preview_plays_without_recording_or_leaving_the_page(tmp_path):
    egg = _egg("other", keys=("q", "w"), unlock={"action": "page", "delay": 100}, message="Hi")
    result = _run(tmp_path, _page(_egg()), [{"preview": egg}, 1000, {"look": True}])
    assert result["snapshots"][0] is True
    assert result["snapshots"][1]["items"] > 0 and result["snapshots"][1]["toast"] == "Hi"
    assert result["fetches"] == [] and result["stored"] == {} and result["href"] == "https://wiki.example/page/home"


# The editor (banana-eggs-admin.js)

EDITOR = Path(__file__).parents[2] / "bananawiki/core/static/eggs/banana-eggs-admin.js"
EDITOR_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const html = process.argv[4];
Object.defineProperty(dom.Element.prototype, "style", {
  get() { return this._style || (this._style = {}); }, configurable: true });
const document = new dom.Document(html);
const window = dom.makeWindow(document, { setTimeout, clearTimeout, setInterval, clearInterval, document });
vm.runInNewContext(fs.readFileSync(process.argv[3], "utf8"), { window, document }, { filename: "banana-eggs-admin.js" });
const input = document.querySelector("input[name=easter_egg_configs]");
const button = (text, index) => document.querySelectorAll("button").filter((b) => b.textContent === text)[index || 0];
const values = [JSON.parse(input.value)];
button("Add an easter egg").dispatchEvent(new dom.Event("click"));
values.push(JSON.parse(input.value));
button("Record keys", 1).dispatchEvent(new dom.Event("click"));
for (const key of ["Shift", "P", "i"]) document.dispatchEvent(new dom.Event("keydown", { props: { key, target: document.body } }));
button("Stop (15)").dispatchEvent(new dom.Event("click"));
values.push(JSON.parse(input.value));
process.stdout.write(JSON.stringify({ values, cards: document.querySelectorAll(".banana-eggs-editor__card").length,
                                      errors: dom.errors }));
"""


def test_the_editor_keeps_the_hidden_field_up_to_date(tmp_path, app, admin_client):
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    harness = tmp_path / "editor.js"
    harness.write_text(EDITOR_HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(MINI_DOM), str(EDITOR), page], capture_output=True, text=True,
                          timeout=60, check=True)
    result = json.loads(done.stdout)
    assert result["errors"] == [] and result["cards"] == 2
    first, added, recorded = result["values"]
    assert eggs.clean(first) == eggs.default_config()
    assert [egg["id"] for egg in added["eggs"]] == ["konami", "egg-2"] and added["eggs"][1]["keys"] == []
    assert added["eggs"][1]["unlock"]["action"] == "toast"
    assert recorded["eggs"][1]["keys"] == ["p", "i"]
    assert eggs.clean(recorded, strict=True)["eggs"][1]["keys"] == ["p", "i"]
