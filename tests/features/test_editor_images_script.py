"""The image dialog's source handling in editor.js, run under node.

Only the part that finds pictures in the Markdown and writes them back is run
(from the "Image options" comment to the dialog's wiring): it is what decides
which bytes of a page change. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

EDITOR = Path(__file__).parents[2] / "bananawiki" / "wiki" / "features" / "pages" / "static" / "editor.js"
SRC = "/static/uploads/5de18075a7caa71f1c553d38c4b58f2e.jpg"

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("  // Image options:"), end = source.indexOf("  var imageDialog = ");
if (start < 0 || end < start) throw new Error("image section not found");
const entities = { "&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#39;": "'" };
const decoder = () => {
  const node = { value: "" };
  const decode = (v) => v.replace(/&(quot|amp|lt|gt|#39);/g, (m) => entities[m]);
  Object.defineProperty(node, "innerHTML", { set(v) { node.value = decode(v); } });
  return node;
};
const context = vm.createContext({
  URL,
  location: { href: "http://wiki.test/create", origin: "http://wiki.test" },
  document: { createElement: decoder },
});
vm.runInContext(source.slice(start, end) + "\nthis.api = { imageTokens, imageOptions, imageMarkup };",
                context);
const api = context.api;
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
for (const [name, c] of Object.entries(cases)) {
  const tokens = api.imageTokens(c.text);
  if (c.options === undefined) {
    out[name] = tokens.map((t) => ({ src: t.src, alt: t.alt, raw: c.text.slice(t.start, t.end) }));
  } else {
    const token = tokens[c.index || 0];
    const options = Object.assign(api.imageOptions(token), c.options);
    out[name] = { read: api.imageOptions(token),
                  markup: api.imageMarkup(token, options, c.src || token.src) };
  }
}
process.stdout.write(JSON.stringify(out));
"""


def run(tmp_path, cases):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(EDITOR), str(data)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_pictures_in_code_are_not_counted(tmp_path):
    text = (f"Intro `![code]({SRC})` and ``![two](x.png)``\n\n![first]({SRC})\n\n"
            f"```\n![fenced]({SRC})\n```\n\n~~~~\n<img src=\"{SRC}\">\n~~~~\n\n"
            f'<img src="{SRC}" alt="second" class="media-left">\nText')
    found = run(tmp_path, {"all": {"text": text}})["all"]
    assert [(t["alt"], t["src"]) for t in found] == [("first", SRC), ("second", SRC)]
    assert found[0]["raw"] == f"![first]({SRC})" and found[1]["raw"].startswith("<img ")


def test_markdown_picture_with_options_becomes_an_img_tag(tmp_path):
    result = run(tmp_path, {"one": {"text": f"![Luca]({SRC})\nText", "options": {
        "alt": 'Rabbit "with" <saw> & co', "width": "240", "align": "right", "padding": "8"}}})["one"]
    assert result["read"] == {"alt": "Luca", "width": "", "unit": "px", "align": "none", "padding": ""}
    assert result["markup"] == (f'<img src="{SRC}" alt="Rabbit &quot;with&quot; &lt;saw&gt; &amp; co" '
                                'width="240" class="media-right" style="padding:8px">')


def test_img_tag_without_options_goes_back_to_markdown(tmp_path):
    text = f'<img src="{SRC}" alt="A [b] &quot;c&quot;" width="50%" class="media-full" style="padding:4px">'
    cleared = {"width": "", "align": "none", "padding": ""}
    result = run(tmp_path, {"one": {"text": text, "options": cleared}})["one"]
    assert result["read"] == {"alt": 'A [b] "c"', "width": "50", "unit": "%", "align": "full", "padding": "4"}
    assert result["markup"] == f'![A b "c"]({SRC})'


def test_unknown_attributes_and_styles_are_kept_old_sizes_replaced(tmp_path):
    text = (f"<img src='{SRC}' alt=x loading=\"lazy\" height=\"90\" class=\"media-responsive media-center\" "
            'style="--media-max-width:300px;--media-padding:6px;margin-top:4px" />')
    result = run(tmp_path, {"one": {"text": text, "options": {"width": "120", "align": "left"}}})["one"]
    assert result["read"] == {"alt": "x", "width": "300", "unit": "px", "align": "center", "padding": "6"}
    assert result["markup"] == (f'<img src="{SRC}" alt="x" width="120" class="media-responsive media-left" '
                                'style="padding:6px;margin-top:4px" loading="lazy" height="90">')


def test_second_copy_and_new_source_after_a_crop(tmp_path):
    text = f"![a]({SRC})\n\n![b]({SRC})"
    cropped = "/static/uploads/0123456789abcdef0123456789abcdef.png"
    result = run(tmp_path, {"one": {"text": text, "index": 1, "src": cropped, "options": {}}})["one"]
    assert result["markup"] == f"![b]({cropped})"
