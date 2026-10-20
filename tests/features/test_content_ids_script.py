"""The interface's scripts run on pages whose content is named after their elements.

Under node, on the HTML the server renders (tests/features/mini_dom.js stands in
for the browser: lookups by id find the first element, as a browser's do, and
window answers to the page's ids). Skipped where node is not installed; the
markup and the script sources are checked everywhere by test_content_ids.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_content_ids import _trapped_board, _trapped_page
from .test_kanban import as_client, columns, new_ticket

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

WIKI = Path(__file__).parents[2] / "bananawiki" / "wiki"
MINI_DOM = Path(__file__).with_name("mini_dom.js")

PRELUDE = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const dom = require(process.argv[2]);
const scenario = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve)); };
const document = new dom.Document(scenario.html);
const element = (selector) => {
  const node = document.querySelector(selector);
  if (!node) throw new Error("no element " + selector);
  return node;
};
function load(context, file) {
  try { vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file }); return null; }
  catch (error) { return String((error && error.stack) || error); }
}
async function play(steps, seen) {
  for (const step of steps) {
    if (step.click) element(step.click).dispatchEvent(new dom.Event("click", { bubbles: true }));
    else if (step.submit) element(step.submit).dispatchEvent(new dom.Event("submit", { bubbles: true }));
    else if (step.set) element(step.set).value = step.value;
    else if (step.look) seen[step.name] = element(step.look)[step.prop];
    else if (step.call) await step.call();
    await flush();
  }
}
function finish(promise) {
  promise.then((result) => process.stdout.write(JSON.stringify(Object.assign({ errors: dom.errors }, result))),
               (error) => process.stdout.write(JSON.stringify({ crashed: "harness: " + String((error && error.stack) || error) })));
}
"""

# kanban-ticket.js with a stand-in for kanban-board.js (window.BWKanban) answering from the server's JSON.
KANBAN = PRELUDE + r"""
const data = JSON.parse(element("script#kanban-data").textContent);
const calls = [], toasts = [], failures = [], seen = {};
const BW = { t: (key) => "t:" + key, toast: (message, kind) => toasts.push([message, kind]), onReady: (fn) => fn() };
const copy = (value) => (value === undefined ? null : JSON.parse(JSON.stringify(value)));  // as sent over the network
const K = {
  state: data.state, assignable: data.assignable || [], canWrite: !!data.can_write,
  el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  },
  api(path, method, body) {
    calls.push({ path: path, method: method || "GET", body: copy(body) });
    const key = (method || "GET") + " " + path;
    return Promise.resolve(copy(scenario.responses[key] || {}));
  },
  fail(error) { failures.push(String((error && error.message) || error)); },
  refresh() {}, upsertTicket() {}, removeTickets() {}, announce() {},
  cardFrom(detail) { return detail; },
  column(id) { return K.state.columns.filter((col) => col.id === id)[0] || null; },
  moveTicket() { return Promise.resolve(); },
  ticket(id) { return K.state.tickets[String(id)] || null; },
};
const window = dom.makeWindow(document, { BW: BW, BWKanban: K, confirm: () => true });
const context = vm.createContext({
  window: window, document: document, BW: BW, CSS: { escape: dom.cssEscape },
  location: { hash: "", pathname: "/kanban/1", search: "" }, history: { replaceState() {} },
  CustomEvent: dom.Event, setTimeout: setTimeout, clearTimeout: clearTimeout, console: console,
});
finish((async () => {
  const crashed = load(context, scenario.script);
  const opens = typeof K.openTicket === "function";
  if (!crashed && opens) {
    await play(scenario.steps.map((step) => (step.open ? { call: () => K.openTicket(step.open) } : step)), seen);
  }
  return { crashed: crashed, opens: opens, calls: calls, toasts: toasts, failures: failures, seen: seen,
           open: element("dialog#kanban-ticket-dialog").open };
})());
"""

# kanban-filter.js with a stand-in for kanban-board.js whose refresh runs the filter's hooks.
KANBAN_FILTER = PRELUDE + r"""
const data = JSON.parse(element("script#kanban-data").textContent);
const seen = {};
const BW = { t: (key) => "t:" + key, onReady: (fn) => fn() };
const K = {
  state: data.state, assignable: data.assignable || [], userId: data.user_id, cardHooks: [], onRefresh: [],
  el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  },
  ticket(id) { return K.state.tickets[String(id)] || null; },
  refresh() { K.onRefresh.forEach((fn) => fn()); },
  announce() {},
  isoDate(date) { return date.toISOString().slice(0, 10); },
  dueStateIn() { return ""; },
};
const window = dom.makeWindow(document, { BW: BW, BWKanban: K });
const context = vm.createContext({
  window: window, document: document, BW: BW, URLSearchParams: URLSearchParams,
  location: { hash: "", pathname: "/kanban/1", search: "" },
  history: { state: null, replaceState(state, title, url) { seen.address = url; } },
  setTimeout: setTimeout, clearTimeout: clearTimeout, console: console,
});
finish((async () => {
  const crashed = load(context, scenario.script);
  if (!crashed) { K.refresh(); await play(scenario.steps, seen); }
  const shown = typeof K.isShown !== "function" ? null : Object.keys(K.state.tickets)
    .filter((id) => K.isShown(K.state.tickets[id])).map(Number).sort((a, b) => a - b);
  return { crashed: crashed, seen: seen, shown: shown };
})());
"""

# bananawiki.js on a page view: the shared dialog opener, and the core object's name on window.
CORE = PRELUDE + r"""
document.readyState = "loading";  // BW.onReady waits: only what runs at load and on clicks is exercised
const seen = {};
const window = dom.makeWindow(document, { addEventListener() {}, confirm: () => true });
const context = vm.createContext({
  window: window, document: document, CSS: { escape: dom.cssEscape }, setTimeout: setTimeout,
  clearTimeout: clearTimeout, console: console,
});
finish((async () => {
  const crashed = load(context, scenario.script);
  await play(scenario.steps, seen);
  const BW = window.BW;
  return { crashed: crashed, seen: seen, ownBW: Object.prototype.hasOwnProperty.call(window, "BW"),
           bwIsElement: !!(BW && BW.nodeType), bwHasT: !!(BW && typeof BW.t === "function") };
})());
"""

# navigation.js on a page view, loaded twice (sidebar and category page): it must set itself up once.
NAVIGATION = PRELUDE + r"""
const BW = { t: (key) => key, fetchJSON: () => Promise.resolve({}), toast() {}, onReady: (fn) => fn(), markMoveEnds() {} };
const window = dom.makeWindow(document, { BW: BW });
const context = vm.createContext({
  window: window, document: document, BW: BW, setTimeout: setTimeout, clearTimeout: clearTimeout,
  getComputedStyle: () => ({ visibility: "visible" }), console: console,
});
finish((async () => {
  const input = element("form[data-sidebar-search] input[name='q']");
  const counts = [];
  let crashed = null;
  for (let run = 0; run < 2; run++) {
    crashed = crashed || load(context, scenario.script);
    counts.push([document.listenerCount("click"), input.listenerCount("input")]);
  }
  return { crashed: crashed, counts: counts, mark: BW.pagesNav === true };
})());
"""

# canvas-static.js on a history preview: the snapshot comes from the script element.
CANVAS_STATIC = PRELUDE + r"""
const loaded = [];
function Scene() {}
Scene.prototype.load = function (data, snapshot) { loaded.push(snapshot); };
Scene.prototype.fit = function () {};
Scene.prototype.trimHeight = function () {};
const BW = { onReady: (fn) => fn(), Canvas: { Scene: Scene } };
const window = dom.makeWindow(document, { BW: BW });
const context = vm.createContext({ window: window, document: document, BW: BW, CSS: { escape: dom.cssEscape } });
finish((async () => ({ crashed: load(context, scenario.script), loaded: loaded }))());
"""


def _run(tmp_path, harness, script, html, **scenario):
    (tmp_path / "harness.js").write_text(harness, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps({"script": str(WIKI / script), "html": html, **scenario}),
                                            encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(MINI_DOM), str(tmp_path / "scenario.json")],
                          capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(done.stdout)
    assert result.get("crashed") is None, result.get("crashed")
    return result


def _sent(result, method, path):
    return [call["body"] for call in result["calls"] if call["method"] == method and call["path"] == path]


# Kanban

TICKET_SCRIPT = "features/kanban/static/kanban-ticket.js"


def _ticket_responses(client, ticket_id):
    ticket = client.get(f"/api/kanban/tickets/{ticket_id}").get_json()
    comments = client.get(f"/api/kanban/tickets/{ticket_id}/comments").get_json()
    return ticket, {f"GET /tickets/{ticket_id}": ticket, f"GET /tickets/{ticket_id}/comments": comments,
                    f"PUT /tickets/{ticket_id}": {"ok": True, **ticket}}


def test_a_viewers_comment_does_not_change_what_a_writer_saves(app, admin_client, db, make_user, login, tmp_path):
    board, ticket, writer_user = _trapped_board(app, admin_client, db, make_user, login, description="Plans")
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    detail, responses = _ticket_responses(admin_client, ticket["id"])
    assigned = sorted(person["id"] for person in detail["assignees"])
    assert len(assigned) == 2 and writer_user["id"] in assigned
    tid = ticket["id"]
    result = _run(tmp_path, KANBAN, TICKET_SCRIPT, html, responses=responses, steps=[
        {"open": tid},
        {"click": "dialog#kanban-ticket-dialog a[data-tab='comments']"},  # draws the viewer's comment
        {"look": "dialog#kanban-ticket-dialog ol#kanban-ticket-comments", "prop": "textContent", "name": "comments"},
        {"set": "select#kanban-ticket-priority", "value": "critical"},
        {"click": "button#kanban-ticket-save"},
        {"set": "textarea#kanban-comment-input", "value": "Agreed."},
        {"submit": "form#kanban-comment-form"},
    ])
    assert "Kanban ticket assignees" in result["seen"]["comments"]
    # Save sends the side panel's values: the assignees stay, the new priority goes.
    [body] = _sent(result, "PUT", f"/tickets/{tid}")
    assert sorted(body["assignees"]) == assigned and body["priority"] == "critical"
    # The comment box still posts what was typed in it.
    assert _sent(result, "POST", f"/tickets/{tid}/comments") == [{"content": "Agreed."}]
    assert result["failures"] == [] and ["t:saved", "success"] in result["toasts"] and result["errors"] == []


def test_a_ticket_description_does_not_take_the_ticket_fields(app, admin_client, db, make_user, login, tmp_path):
    board, ticket, writer_user = _trapped_board(app, admin_client, db, make_user, login, description="Plans")
    writer = as_client(app, login, writer_user)
    trap = "## Kanban ticket priority\n\n## Kanban ticket description editor\n\n## Kanban checklist input\n\nSpec."
    assert writer.put(f"/api/kanban/tickets/{ticket['id']}", json={"description": trap, "priority": "high"}
                      ).status_code == 200
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    _, responses = _ticket_responses(admin_client, ticket["id"])
    tid = ticket["id"]
    responses[f"POST /tickets/{tid}/checklist"] = {"ticket": {"id": tid},
                                                   "checklist": [{"id": 1, "text": "Write tests", "done": False}]}
    result = _run(tmp_path, KANBAN, TICKET_SCRIPT, html, responses=responses, steps=[
        {"open": tid},
        {"look": "select#kanban-ticket-priority", "prop": "value", "name": "shown_priority"},
        {"click": "button#kanban-ticket-description-edit"},
        {"look": "div#kanban-ticket-description-editor", "prop": "hidden", "name": "editor_hidden"},
        {"look": "textarea#kanban-ticket-description-input", "prop": "value", "name": "editing"},
        {"set": "select#kanban-ticket-priority", "value": "critical"},
        {"click": "button#kanban-ticket-save"},
        {"set": "input#kanban-checklist-input", "value": "Write tests"},
        {"submit": "form#kanban-checklist-form"},
    ])
    assert result["seen"] == {"shown_priority": "high", "editor_hidden": False, "editing": trap}
    [body] = _sent(result, "PUT", f"/tickets/{tid}")
    assert body["priority"] == "critical"
    assert _sent(result, "POST", f"/tickets/{tid}/checklist") == [{"text": "Write tests"}]
    assert result["failures"] == [] and result["errors"] == []


def test_a_board_description_does_not_stop_tickets_from_opening(app, admin_client, db, make_user, login, tmp_path):
    board, ticket, _ = _trapped_board(app, admin_client, db, make_user, login)  # "## Kanban ticket dialog" and more
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    _, responses = _ticket_responses(admin_client, ticket["id"])
    result = _run(tmp_path, KANBAN, TICKET_SCRIPT, html, responses=responses, steps=[{"open": ticket["id"]}])
    assert result["opens"] and result["open"] and result["failures"] == [] and result["errors"] == []


def test_a_board_description_does_not_stop_the_search(app, admin_client, db, make_user, login, tmp_path):
    board, ticket, _ = _trapped_board(app, admin_client, db, make_user, login)  # "## Kanban filter" and more
    other = new_ticket(admin_client, columns(admin_client, board)[0]["id"], "Other")
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    assert html.index('<h2 id="kanban-filter">') < html.index('<form class="kanban-filter')
    result = _run(tmp_path, KANBAN_FILTER, "features/kanban/static/kanban-filter.js", html, steps=[
        {"set": "form#kanban-filter input[name='q']", "value": "spec"},
        {"submit": "form#kanban-filter"},
        {"look": "form#kanban-filter p#kanban-filter-status", "prop": "textContent", "name": "status"},
        {"look": "form#kanban-filter button#kanban-filter-clear", "prop": "hidden", "name": "clear_hidden"},
    ])
    assert result["shown"] == [ticket["id"]] and other["id"] != ticket["id"]
    assert result["seen"] == {"status": "t:kanban.filter_result", "clear_hidden": False,
                              "address": "/kanban/1?q=spec"}
    assert result["errors"] == []


# Core: dialogs and the BW object


def test_page_dialogs_open_and_the_core_object_is_the_scripts(client, make_user, login, tmp_path):
    url = _trapped_page(client, make_user, login)  # headings "Dlg title", "Dlg slug", raw <h2 id="BW">
    login(client, make_user("page_admin", role="admin"))
    html = client.get(url).get_data(as_text=True)
    steps = []
    for name in ("dlg-title", "dlg-move", "dlg-slug"):
        steps += [{"click": f"button[data-dialog-open='{name}']"},
                  {"look": f"dialog#{name}", "prop": "open", "name": name}]
    result = _run(tmp_path, CORE, "static/js/bananawiki.js", html, steps=steps)
    assert result["seen"] == {"dlg-title": True, "dlg-move": True, "dlg-slug": True}
    assert result["ownBW"] and result["bwHasT"] and not result["bwIsElement"] and result["errors"] == []


def test_board_menu_dialogs_open_past_the_description(app, admin_client, db, make_user, login, tmp_path):
    board, _, _ = _trapped_board(app, admin_client, db, make_user, login)
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    steps = []
    for name in ("kanban-edit-dialog", "kanban-share-dialog", "kanban-delete-dialog"):
        steps += [{"click": f"button[data-dialog-open='{name}']"},
                  {"look": f"dialog#{name}", "prop": "open", "name": name}]
    result = _run(tmp_path, CORE, "static/js/bananawiki.js", html, steps=steps)
    assert result["seen"] == {"kanban-edit-dialog": True, "kanban-share-dialog": True, "kanban-delete-dialog": True}
    assert result["errors"] == []


# Sidebar navigation


def test_sidebar_navigation_sets_itself_up_once_whatever_the_page_names(client, make_user, login, tmp_path):
    url = _trapped_page(client, make_user, login)  # raw <h2 id="BWPagesNav">
    login(client, make_user("page_reader"))
    html = client.get(url).get_data(as_text=True)
    result = _run(tmp_path, NAVIGATION, "features/pages/static/navigation.js", html)
    first, second = result["counts"]
    assert first[0] >= 3 and first[1] == 1 and second == first and result["mark"]


# Canvas history preview


def test_canvas_history_preview_reads_its_own_snapshot(tmp_path):
    html = ('<main><h2 id="canvas-snapshot">{"data": {"nodes": [{"id": "heading"}]}}</h2>'
            '<div data-canvas-static data-source="canvas-snapshot"></div>'
            '<script type="application/json" id="canvas-snapshot">{"data": {"nodes": [{"id": "revision"}]}}</script>'
            "</main>")
    result = _run(tmp_path, CANVAS_STATIC, "features/canvas/static/canvas-static.js", html)
    assert [snapshot["data"]["nodes"][0]["id"] for snapshot in result["loaded"]] == ["revision"]
