"""The read-aloud player (static/tts.js) under node, on the panel the wiki renders.

The panel comes from the real template (rendered by the wiki, parsed here)
and sits under a small page in a stand-in for the DOM. The script's own
functions are reached by cutting its final ``init()`` call off. Checked:

* the words it highlights come in reading order (title first, inline code
  and image descriptions included, code blocks and the contents list left
  out), each weighted by its length and the pause after it, and a long page
  stops where the audio does;
* paragraph navigation, clicking a word to read from there, J/K/L while the
  player has focus, and the phone's lock-screen controls;
* the position each page's audio stopped at, offered again with "Start over";
* the small player at the bottom of the screen;
* states: removal stops the audio, a full queue is waited out, the queue
  position, no worker running, a failure that can be retried, the rate limit.

Skipped where node is not installed. Layout, scrolling and real audio are
checked in a browser (see the read-aloud notes in the changelog).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.tts.routes import render_panel

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")
SCRIPT = Path(__file__).parents[2] / "bananawiki" / "wiki" / "features" / "tts" / "static" / "tts.js"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

# The page around the panel: [tag, attributes, children...]
PAGE = [
    "div", {"class": "wiki-page__main"},
    ["header", {"class": "page-header"}, ["div", {"class": "wiki-page__title"}, ["h1", {}, "Guide"]]],
    ["article", {"id": "page-content", "class": "prose"},
     ["h2", {}, ["a", {"aria-hidden": "true"}, "#"], "Steps"],
     ["p", {}, "Run ", ["code", {}, "make"], " now."],
     ["ul", {}, ["li", {}, "Parent", ["ul", {}, ["li", {}, "Child"]]]],
     ["pre", {}, ["code", {}, "not read"]],
     ["nav", {"class": "toc"}, "Contents"],
     ["p", {}, ["img", {"alt": "A banana", "src": "x.png"}], ["img", {"alt": "", "src": "deco.png"}]],
     ["table", {}, ["tr", {}, ["td", {}, "Cell"]]],
     ["div", {"class": "bw-embed"}, ["p", {}, "Board Sprint Todo"]],  # an embedded board: the voice skips it
     ["p", {}, ["a", {"href": "/elsewhere"}, "Linked"], " End"]],
]
# Each word's weight: its length, a space, and a pause after a sentence (4) or a block (4).
WORDS = ["Guide", "Steps", "Run", "make", "now.", "Parent", "Child", "[img:A banana]", "Cell", "Linked", "End"]
WEIGHTS = [9, 9, 4, 5, 9, 10, 9, 12, 8, 7, 7]
STARTS = [sum(WEIGHTS[:index]) for index in range(len(WEIGHTS))]
TOTAL = sum(WEIGHTS)

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

/* A small DOM: elements, text, fragments, events that bubble, and selectors made of tags, #ids,
   .classes and [attributes], with descendant combinators and comma lists. */
class Node_ { constructor(type) { this.nodeType = type; this.parentNode = null; this.childNodes = []; } }
class Text_ extends Node_ {
  constructor(value) { super(3); this.nodeValue = value; }
  get textContent() { return this.nodeValue; }
}
function simpleMatch(el, sel) {
  const m = /^([a-z0-9]*)(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i.exec(sel);
  if (!m || el.nodeType !== 1) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && el.getAttribute("id") !== m[2].slice(1)) return false;
  const classes = (el.getAttribute("class") || "").split(/\s+/);
  for (const c of m[3].split(".").filter(Boolean)) if (!classes.includes(c)) return false;
  for (const a of (m[4].match(/\[[^\]]+\]/g) || [])) {
    const [, name, value] = /^\[([\w-]+)(?:='([^']*)')?\]$/.exec(a);
    if (!el.hasAttribute(name)) return false;
    if (value !== undefined && el.getAttribute(name) !== value) return false;
  }
  return true;
}
function complexMatch(el, sel) {
  const parts = sel.trim().split(/\s+/);
  if (!simpleMatch(el, parts.pop())) return false;
  let node = el.parentNode;
  while (parts.length && node && node.nodeType === 1) {
    if (simpleMatch(node, parts[parts.length - 1])) parts.pop();
    node = node.parentNode;
  }
  return !parts.length;
}
let activeElement = null;
class Element_ extends Node_ {
  constructor(tag, attrs) {
    super(1); this.tagName = tag.toUpperCase(); this.attrs = Object.assign({}, attrs || {});
    const self = this;
    this.classList = {
      add(c) { const l = (self.attrs["class"] || "").split(/\s+/).filter(Boolean); if (!l.includes(c)) l.push(c); self.attrs["class"] = l.join(" "); },
      remove(c) { self.attrs["class"] = (self.attrs["class"] || "").split(/\s+/).filter((x) => x && x !== c).join(" "); },
      contains(c) { return (self.attrs["class"] || "").split(/\s+/).includes(c); },
    };
    this.listeners = {};
    this.offsetHeight = 40;
    if (this.tagName === "INPUT") this.checked = this.hasAttribute("checked");
    if (this.tagName === "SELECT") { this.value = "1"; this.options = []; }
  }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(v) { if (v) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
  removeAttribute(n) { delete this.attrs[n]; }
  appendChild(child) {
    const add = child.isFragment ? child.childNodes.splice(0) : [child];
    add.forEach((c) => { c.parentNode = this; this.childNodes.push(c); });
    return child;
  }
  replaceChild(child, old) {
    const add = child.isFragment ? child.childNodes.splice(0) : [child];
    add.forEach((c) => { c.parentNode = this; });
    this.childNodes.splice(this.childNodes.indexOf(old), 1, ...add);
    old.parentNode = null;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.childNodes = []; this.appendChild(new Text_(v)); }
  matches(sel) { return sel.split(",").some((s) => complexMatch(this, s)); }
  closest(sel) { let e = this; while (e && e.nodeType === 1) { if (e.matches(sel)) return e; e = e.parentNode; } return null; }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const found = [];
    const walk = (n) => n.childNodes.forEach((c) => { if (c.nodeType === 1) { if (c.matches(sel)) found.push(c); walk(c); } });
    walk(this);
    return found;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra) {
    const event = Object.assign({ type, target: this, button: 0, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } }, extra || {});
    for (let node = this; node; node = node.parentNode) (node.listeners && node.listeners[type] || []).forEach((fn) => fn(event));
    return event;
  }
  focus() { activeElement = this; }
  getBoundingClientRect() { return { top: 100, bottom: 120 }; }
  scrollIntoView() { this.scrolledIntoView = true; }
}
function media(el) {
  Object.assign(el, { paused: true, ended: false, duration: NaN, playbackRate: 1, defaultPlaybackRate: 1, loads: 0,
    _time: 0,
    play() { this.paused = false; this.dispatch("play"); return Promise.resolve(); },
    pause() { if (!this.paused) { this.paused = true; this.dispatch("pause"); } },
    load() { this.loads += 1; } });
  Object.defineProperty(el, "currentTime", { get() { return this._time; }, set(v) { this._time = v; } });
}
function build(spec) {
  if (typeof spec === "string") return new Text_(spec);
  const [tag, attrs, ...children] = spec;
  const el = new Element_(tag, attrs);
  if (el.tagName === "AUDIO") media(el);
  children.forEach((c) => el.appendChild(build(c)));
  return el;
}
const body = new Element_("body");
const NodeFilter = { SHOW_ELEMENT: 1, SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 };
const document = {
  documentElement: new Element_("html"),
  body,
  get activeElement() { return activeElement; },
  querySelector: (sel) => body.querySelector(sel),
  querySelectorAll: (sel) => body.querySelectorAll(sel),
  createElement: (tag) => new Element_(tag),
  createTextNode: (v) => new Text_(v),
  createDocumentFragment: () => { const f = new Element_("fragment"); f.isFragment = true; return f; },
  createTreeWalker(root, show, filter) {
    const list = [];
    const visit = (n) => n.childNodes.forEach((c) => {
      if (c.nodeType === 3 && !(show & 4)) return;
      const verdict = filter.acceptNode(c);
      if (verdict === NodeFilter.FILTER_ACCEPT) list.push(c);
      if (verdict !== NodeFilter.FILTER_REJECT && c.nodeType === 1) visit(c);
    });
    visit(root);
    let i = -1;
    return { get currentNode() { return list[i]; }, nextNode() { i += 1; return i < list.length ? list[i] : null; } };
  },
  readyState: "complete",
  addEventListener() {},
};
const storage = {};
let selectionCollapsed = true;
let observer = null;
const mediaHandlers = {};
const windowObject = {
  innerHeight: 800,
  addEventListener() {},
  setTimeout(fn, ms) { return 1; },
  clearTimeout() {},
  requestIdleCallback(fn) { fn(); },
  confirm: () => true,
  getSelection: () => ({ isCollapsed: selectionCollapsed }),
  localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); } },
  IntersectionObserver: function (callback) { observer = callback; this.observe = function () {}; },
  MediaMetadata: function (data) { Object.assign(this, data); },
};
const navigator = { mediaSession: { metadata: null, setActionHandler(name, fn) { mediaHandlers[name] = fn; } } };
windowObject.BW = { t: (key, vars) => key + (vars ? JSON.stringify(vars) : ""), tn: (key, n) => key + "#" + n,
                    fetchJSON: null };
const context = vm.createContext({ document, window: windowObject, navigator, NodeFilter, JSON, Math, Date, Promise,
                                   isFinite, parseFloat, encodeURIComponent, BW: windowObject.BW });
let source = fs.readFileSync(process.argv[2], "utf8");
const tail = source.indexOf("  if (document.readyState");
if (tail < 0) throw new Error("init call not found");
source = source.slice(0, tail) + "  globalThis.api = { collectWords: collectWords, Panel: Panel };\n}());\n";
vm.runInContext(source, context);
const api = context.api;
const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
body.appendChild(build(input.page));
body.appendChild(build(input.panel));
const root = body.querySelector("[data-tts-panel]");
const q = (sel) => root.querySelector(sel);
const audio = q("[data-tts-audio]");
const word = (text) => body.querySelectorAll("[data-tts-word]").find((w) => w.textContent === text);

const USABLE = { generation: { id: 1, status: "completed", completed_at: "x", language_label: "English", file_size: 9 },
                 usable: true, in_flight: false, can_generate: false, can_cancel: true, blocked_reason: null,
                 cut_at: 0 };
const NONE = { generation: null, usable: false, in_flight: false, can_generate: true, can_cancel: false,
               blocked_reason: null, blocked_message: "" };
function loaded(seconds) { audio.duration = seconds; audio.dispatch("loadedmetadata"); }
function state(changes) { return Object.assign({}, USABLE, changes); }
function src() { return audio.getAttribute("src"); }
function now() { return Math.round(audio.currentTime * 1000) / 1000; }

const scenarios = {
  words() {
    const words = api.collectWords([body.querySelector("h1"), body.querySelector("article")], {});
    const cut = api.collectWords([body.querySelector("h1"), body.querySelector("article")], { all: words }, 12);
    return { words: words.map((w) => w.el.tagName === "IMG" ? "[img:" + w.el.getAttribute("alt") + "]" : w.el.textContent),
             weights: words.map((w) => w.end - w.start), cut: cut.map((w) => w.el.textContent) };
  },
  paragraphs() {
    const panel = new api.Panel(root);
    panel.render(USABLE);
    loaded(input.total);  // one second per weight unit
    const at = (seconds) => { audio.currentTime = seconds; };
    const out = {};
    at(input.starts[3] + 0.5);  // in "make", 4.5 s into its paragraph
    panel.paragraph(1); out.next = now();
    at(input.starts[3] + 0.5);
    panel.paragraph(-1); out.back_to_start = now();
    at(input.starts[2] + 0.5);  // just started "Run make now."
    panel.paragraph(-1); out.previous = now();
    at(input.starts[input.starts.length - 1] + 1);
    panel.paragraph(1); out.after_last = now();
    out.buttons_enabled = root.querySelectorAll("[data-tts-next]").every((b) => !b.disabled);
    return out;
  },
  click() {
    const panel = new api.Panel(root);
    panel.render(USABLE);
    loaded(input.total);
    const out = {};
    out.seekable = body.querySelector("article").classList.contains("tts-seekable");
    word("make").dispatch("click");
    out.before_listening = { time: now(), playing: !audio.paused };
    panel.paragraph(1); audio.currentTime = 0;
    word("make").dispatch("click");
    out.after_next = { time: now(), playing: !audio.paused };
    audio.play(); audio.pause(); audio.currentTime = 0;
    word("make").dispatch("click");
    out.make = { time: now(), playing: !audio.paused };
    audio.pause(); audio.currentTime = 0;
    word("Linked").dispatch("click");
    out.link = now();
    selectionCollapsed = false;
    word("Cell").dispatch("click");
    out.selecting = now();
    selectionCollapsed = true;
    q("[data-tts-follow]").checked = false;
    q("[data-tts-follow]").dispatch("change");
    word("Cell").dispatch("click");
    out.highlight_off = now();
    return out;
  },
  keys() {
    const panel = new api.Panel(root);
    panel.render(USABLE);
    loaded(input.total);
    const out = {};
    const key = (k, target) => (target || audio).dispatch("keydown", { key: k });
    out.k_default = key("k").defaultPrevented; out.after_k = !audio.paused;
    audio.currentTime = input.starts[3] + 0.5;
    key("l"); out.after_l = now();
    key("J"); out.after_j = now();
    out.in_select = key("l", q("[data-tts-speed]")).defaultPrevented;
    out.ctrl = audio.dispatch("keydown", { key: "l", ctrlKey: true }).defaultPrevented;
    // The lock screen: next paragraph, ten seconds back.
    audio.currentTime = input.starts[3] + 0.5;
    mediaHandlers.nexttrack(); out.media_next = now();
    mediaHandlers.seekbackward({}); out.media_back = now();
    out.media_title = navigator.mediaSession.metadata && navigator.mediaSession.metadata.title;
    return out;
  },
  resume() {
    // An entry of another shape (an older format, a hand edit) next to a good one is dropped, not fatal.
    storage.bwTtsPositions = JSON.stringify({ "/page/guide/tts/audio?v=1-x": [30, Date.now()], "/x": { "x": null } });
    const panel = new api.Panel(root);
    panel.render(USABLE);
    const out = { note: !q("[data-tts-resume]").hidden, text: q("[data-tts-resume-text]").textContent };
    loaded(input.total);
    out.restored = now();
    out.highlight_before_play = body.querySelectorAll(".is-current").length;
    q("[data-tts-restart]").dispatch("click");
    out.restarted = now();
    out.forgotten = !(src() in JSON.parse(storage.bwTtsPositions));
    audio.play(); audio.currentTime = 50; audio.pause();
    out.saved = JSON.parse(storage.bwTtsPositions)[src()][0];
    audio.dispatch("ended");
    out.after_end = src() in JSON.parse(storage.bwTtsPositions);
    return out;
  },
  mini() {
    const panel = new api.Panel(root);
    panel.render(USABLE);
    loaded(input.total);
    const mini = q("[data-tts-mini]");
    const out = { before: mini.hidden };
    audio.play();
    out.panel_in_view = mini.hidden;
    observer([{ isIntersecting: false }]);
    out.scrolled_away = mini.hidden;
    out.label = q("[data-tts-toggle]").getAttribute("aria-label");
    q("[data-tts-toggle]").dispatch("click");
    out.paused = audio.paused;
    out.label_paused = q("[data-tts-toggle]").getAttribute("aria-label");
    audio.dispatch("ended");
    out.after_end = mini.hidden;
    return out;
  },
  states() {
    const panel = new api.Panel(root);
    const out = {};
    const status = () => q("[data-tts-status]").textContent;
    panel.render(USABLE);
    audio.play();
    out.remove_delay = panel.render(NONE);
    out.removed = { paused: audio.paused, src: src(), loads: audio.loads, hidden: q("[data-tts-player]").hidden };
    const queued = { generation: { id: 2, status: "pending", language_label: "English" }, usable: false, in_flight: true,
                     can_generate: false, can_cancel: false, queue_ahead: 0, worker_running: true };
    out.pending = [panel.render(queued) > 0, status()];
    panel.render(Object.assign({}, queued, { queue_ahead: 2 })); out.ahead = status();
    panel.render(Object.assign({}, queued, { worker_running: false })); out.no_worker = status();
    out.full = panel.render(Object.assign({}, NONE, { can_generate: false, blocked_reason: "queue_full",
                                                     blocked_message: "Busy" })) > 0;
    out.full_button_hidden = q("[data-tts-generate]").hidden;
    const failed = Object.assign({}, NONE, { generation: { id: 3, status: "failed" } });
    panel.render(failed); out.retry = [q("[data-tts-generate]").textContent, status()];
    panel.render(Object.assign({}, failed, { can_generate: false })); out.failed_reader = status();
    panel.render(NONE); out.generate_label = q("[data-tts-generate]").textContent;
    panel.render(state({ cut_at: 12 }));
    out.cut_note = !q("[data-tts-cut]").hidden;
    return out;
  },
  async refused() {
    windowObject.BW.fetchJSON = () => Promise.reject(Object.assign(new Error("x"), { status: 429,
      data: { error: "Too many requests" } }));
    const panel = new api.Panel(root);
    panel.render(NONE);
    panel.generate();
    await new Promise((resolve) => setTimeout(resolve, 10));
    return { status: q("[data-tts-status]").textContent };
  },
  async generated() {
    windowObject.BW.fetchJSON = () => Promise.resolve({ ok: true });
    const panel = new api.Panel(root);
    panel.render(NONE);
    panel.generate();
    await new Promise((resolve) => setTimeout(resolve, 10));
    return { focus: activeElement && activeElement.hasAttribute("data-tts-status"),
             button_hidden: q("[data-tts-generate]").hidden };
  },
};
Promise.resolve(scenarios[process.argv[4]]()).then((out) => process.stdout.write(JSON.stringify(out)));
"""


class _Spec(HTMLParser):
    """HTML to the harness's [tag, attributes, children...] form."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["div", {}]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = [tag, {name: value or "" for name, value in attrs}]
        self.stack[-1].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].append([tag, {name: value or "" for name, value in attrs}])

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].append(data)


@pytest.fixture
def panel(app, make_user):
    """The panel the wiki renders for a page without audio yet, as harness markup."""
    listener = make_user("listener")
    with app.test_request_context("/page/guide"), connection_scope():
        g.user = g.real_user = listener
        page = pages.create("Guide", "Some text to read aloud.", category_id=None, author_id=None)
        html = str(render_panel(page))
    parser = _Spec()
    parser.feed(html)
    return next(node for node in parser.root[2:] if isinstance(node, list) and node[0] == "section")


@pytest.fixture
def player(tmp_path, panel):
    def run(scenario):
        harness = tmp_path / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        data = tmp_path / "input.json"
        data.write_text(json.dumps({"page": PAGE, "panel": panel, "starts": STARTS, "total": TOTAL}), encoding="utf-8")
        done = subprocess.run([NODE, str(harness), str(SCRIPT), str(data), scenario], capture_output=True, text=True,
                              timeout=30)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)
    return run


def test_words_come_in_reading_order_with_their_pauses(player):
    result = player("words")
    # The title is read first; inline code, the parent of a nested list and image descriptions are
    # read too; code blocks, the contents list, embeds, hidden anchors and decorative images are not.
    assert result["words"] == WORDS
    assert result["weights"] == WEIGHTS
    assert result["cut"] == ["Guide", "Steps"], "a long page: the words after the audio's last are not highlighted"


def test_previous_and_next_paragraph(player):
    result = player("paragraphs")
    assert result["buttons_enabled"]
    assert result["next"] == STARTS[5], "from 'make' to the list item"
    assert result["back_to_start"] == STARTS[2], "first back to the start of the paragraph being read"
    assert result["previous"] == STARTS[1], "just after a paragraph starts: the one before"
    assert result["after_last"] == STARTS[-1] + 1, "after the last paragraph nothing happens"


def test_clicking_a_word_reads_from_there(player):
    result = player("click")
    assert result["seekable"]
    assert result["before_listening"] == {"time": 0, "playing": False}, "a tap never starts the audio by surprise"
    assert result["after_next"] == {"time": 0, "playing": False}, "moving between paragraphs is not listening yet"
    assert result["make"] == {"time": STARTS[3], "playing": True}
    assert result["link"] == 0, "a link keeps working as a link"
    assert result["selecting"] == 0, "selecting text is not a request to read"
    assert result["highlight_off"] == 0, "only while highlighting is on"


def test_keys_and_lock_screen_controls(player):
    result = player("keys")
    assert result["k_default"] and result["after_k"]
    assert result["after_l"] == STARTS[5]
    assert result["after_j"] == STARTS[2], "at the start of a paragraph, J goes to the one before"
    assert result["in_select"] is False and result["ctrl"] is False
    assert result["media_next"] == STARTS[5] and result["media_back"] == STARTS[5] - 10
    assert result["media_title"] == "Guide"


def test_each_page_continues_where_the_reader_stopped(player):
    result = player("resume")
    assert result["note"] and result["text"] == 'tts.resume{"time":"0:30"}'
    assert result["restored"] == 30
    assert result["highlight_before_play"] == 0, "nothing moves on the page before the reader presses play"
    assert result["restarted"] == 0 and result["forgotten"]
    assert result["saved"] == 50
    assert result["after_end"] is False, "a page listened to the end starts from the beginning next time"


def test_the_small_player_appears_while_playing_out_of_view(player):
    result = player("mini")
    assert result["before"] and result["panel_in_view"], "hidden before playing and while the panel is in view"
    assert result["scrolled_away"] is False
    assert result["label"] == "Pause" and result["paused"] and result["label_paused"] == "Play"
    assert result["after_end"]


def test_states(player):
    result = player("states")
    assert result["remove_delay"] == 0
    assert result["removed"] == {"paused": True, "src": None, "loads": 1, "hidden": True}
    assert result["pending"] == [True, "tts.pending"]
    assert result["ahead"] == "tts.queued_ahead#2"
    assert result["no_worker"] == "tts.no_worker"
    assert result["full"] and result["full_button_hidden"]
    assert result["retry"] == ["Try again", "tts.failed_retry"]
    assert result["failed_reader"] == "tts.failed"
    assert result["generate_label"] == "Generate audio"
    assert result["cut_note"]


def test_a_rate_limited_request_says_so(player):
    assert player("refused")["status"] == "Too many requests"


def test_after_asking_for_audio_the_keyboard_stays_with_the_news(player):
    assert player("generated") == {"focus": True, "button_hidden": True}
