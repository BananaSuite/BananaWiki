"""The upload helpers in bananawiki.js (BW.upload, BW.uploadProblem, BW.formatBytes), run under node.

A fake XMLHttpRequest plays the server and the network: it decides when the
body counts as sent, what answers and whether the connection drops. Skipped
where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

CORE = Path(__file__).parents[2] / "bananawiki" / "wiki" / "static" / "js" / "bananawiki.js"

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const script = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const noop = () => {};
const element = () => ({ getAttribute: () => "en", style: { setProperty: noop }, classList: { toggle: noop } });
const document = {
  readyState: "loading", documentElement: element(), addEventListener: noop,
  querySelector: () => null, querySelectorAll: () => [],
};
const requests = [];
class FakeXHR {
  constructor() { this.listeners = {}; this.upload = { listeners: {}, addEventListener: (n, f) => { this.upload.listeners[n] = f; } }; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(name, value) { (this.headers = this.headers || {})[name] = value; }
  addEventListener(name, f) { this.listeners[name] = f; }
  getResponseHeader() { return this.type || ""; }
  abort() { this.aborted = true; this.listeners.abort && this.listeners.abort(); }
  send() {
    const step = script.shift() || { status: 200, json: {} };
    requests.push({ url: this.url, upload: this.headers["X-BW-Upload"], csrf: this.headers["X-CSRF-Token"] });
    setTimeout(() => {
      if (this.aborted) return;
      if (this.upload.listeners.progress) this.upload.listeners.progress({ loaded: 5, total: 10, lengthComputable: true });
      if (step.drop === "before_sent") { this.listeners.error(); return; }
      if (this.upload.listeners.load) this.upload.listeners.load();
      if (step.drop === "after_sent") { this.listeners.error(); return; }
      this.status = step.status;
      this.type = step.json !== undefined ? "application/json" : "text/html";
      this.responseText = step.json !== undefined ? JSON.stringify(step.json) : (step.html || "");
      this.listeners.load();
    }, 1);
  }
}
const timers = { setTimeout: (f) => setTimeout(f, 0) };
const window = { addEventListener: noop, matchMedia: null, setTimeout: timers.setTimeout };
const context = vm.createContext({
  window, document, XMLHttpRequest: FakeXHR, setTimeout: timers.setTimeout, Promise, JSON, Date, Math, Number,
  parseInt, Object, Array, String, Error, Headers: class {}, fetch: noop, DOMParser: undefined, CSS: {},
});
vm.runInContext(source, context);
const BW = context.window.BW;
const out = { problems: {}, sizes: {} };
const rules = { maxBytes: 1000, allow: ["pdf", "png"], deny: ["exe"] };
for (const [name, size] of [["a.pdf", 10], ["a.PNG", 10], ["big.pdf", 5000], ["virus.exe", 10], ["x.txt", 10],
                            ["empty.pdf", 0], ["noext", 10]]) {
  out.problems[name] = BW.uploadProblem({ name, size }, rules);
}
for (const n of [0, 900, 1536, 5 * 1024 * 1024, 3.25 * 1024 * 1024 * 1024]) out.sizes[n] = BW.formatBytes(n);
const progress = [];
const retries = [];
// {"cancel_on_retry": true} first: the person presses Cancel while the upload waits to try again.
const cancelOnRetry = !!(script[0] && script[0].cancel_on_retry);
if (cancelOnRetry) script.shift();
const signal = { aborted: false, listeners: [], addEventListener(name, f) { this.listeners.push(f); } };
const cancel = () => { signal.aborted = true; signal.listeners.forEach((f) => f()); };
BW.upload("/up", {}, { onProgress: (l, t) => progress.push([l, t]), signal,
                       onRetry: (n) => { retries.push(n); if (cancelOnRetry) cancel(); } })
  .then((data) => ({ ok: true, data }), (error) => ({ ok: false, status: error.status, message: error.message,
                                                   aborted: !!error.aborted, network: !!error.network }))
  .then((result) => {
    out.result = result;
    out.requests = requests;
    out.retries = retries;
    out.progress = progress.length > 0;
    out.active = BW.uploadsActive();
    process.stdout.write(JSON.stringify(out));
  });
"""


def run(tmp_path: Path, steps: list[dict]) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "steps.json"
    data.write_text(json.dumps(steps), encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(CORE), str(data)], capture_output=True, text=True, timeout=60,
                          check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_files_the_server_would_refuse_are_named_before_sending(tmp_path):
    result = run(tmp_path, [])
    problems = result["problems"]
    assert problems["a.pdf"] == "" and problems["a.PNG"] == ""
    assert problems["big.pdf"] == "upload.too_large"  # the key: no catalogue in this harness
    assert problems["virus.exe"] == problems["x.txt"] == problems["noext"] == "upload.type"
    assert problems["empty.pdf"] == "upload.empty"
    assert result["sizes"] == {"0": "0 B", "900": "900 B", "1536": "1.5 KB", "5242880": "5 MB",
                               "3489660928": "3.3 GB"}


def test_an_upload_reports_progress_and_marks_itself(tmp_path):
    result = run(tmp_path, [{"status": 201, "json": {"id": 7}}])
    assert result["result"] == {"ok": True, "data": {"id": 7}}
    assert result["progress"] and not result["active"]
    assert result["requests"][0]["upload"] == "1"


def test_a_connection_lost_before_the_body_was_sent_is_tried_again(tmp_path):
    result = run(tmp_path, [{"drop": "before_sent"}, {"drop": "before_sent"}, {"status": 201, "json": {"id": 1}}])
    assert result["result"]["ok"] and result["retries"] == [1, 2] and len(result["requests"]) == 3


def test_a_connection_lost_after_the_body_was_sent_is_not_sent_twice(tmp_path):
    # The server may have stored the file: sending it again could make a copy.
    result = run(tmp_path, [{"drop": "after_sent"}, {"status": 201, "json": {}}])
    assert result["result"]["network"] and len(result["requests"]) == 1 and result["retries"] == []


def test_a_busy_server_gets_the_file_again_but_a_refusal_does_not(tmp_path):
    busy = run(tmp_path, [{"status": 503, "json": {"error": "busy"}}, {"status": 201, "json": {"id": 2}}])
    assert busy["result"]["ok"] and busy["retries"] == [1]
    refused = run(tmp_path, [{"status": 413, "json": {"error": "Too large."}}])
    assert refused["result"] == {"ok": False, "status": 413, "message": "Too large.", "aborted": False,
                                 "network": False}
    gave_up = run(tmp_path, [{"drop": "before_sent"}] * 3)
    assert gave_up["result"]["network"] and len(gave_up["requests"]) == 3 and not gave_up["active"]


def test_cancelling_while_an_upload_waits_to_try_again_ends_it(tmp_path):
    # Without this the promise never settled: the page kept asking before leaving and its form stayed locked.
    result = run(tmp_path, [{"cancel_on_retry": True}, {"drop": "before_sent"}, {"status": 201, "json": {}}])
    assert result["result"]["aborted"] and result["retries"] == [1]
    assert len(result["requests"]) == 1 and not result["active"]
