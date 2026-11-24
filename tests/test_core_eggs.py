"""bananawiki.core.eggs, the BananaSuite easter egg reference, against the suite's shared vectors.

``tests/fixtures/eggs-vectors.json`` is a byte-identical copy of the vectors BananaChat and BananaGit test
their own cleaners with: every copy must give the same answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bananawiki.core import eggs

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "eggs-vectors.json").read_text(encoding="utf-8"))


def test_the_vectors_are_the_format_this_module_reads():
    assert VECTORS["spec"] == "banana-eggs/v1" and eggs.VERSION == 1


@pytest.mark.parametrize("case", VECTORS["clean"], ids=lambda case: case["name"])
def test_clean(case):
    assert eggs.clean(case["input"]) == case["clean"]


@pytest.mark.parametrize("case", VECTORS["clean"], ids=lambda case: case["name"])
def test_strict_errors(case):
    try:
        cleaned = eggs.clean(case["input"], strict=True)
    except eggs.EggError as error:
        assert [list(item) for item in error.errors] == case["strict_errors"]
    else:
        assert case["strict_errors"] == [] and cleaned == case["clean"]


@pytest.mark.parametrize("case", VECTORS["legacy"], ids=lambda case: case["name"])
def test_legacy(case):
    assert eggs.from_legacy(case["value"], case["columns"]) == case["result"]


def test_the_default_is_the_konami_code_and_cleans_to_itself():
    config = eggs.default_config()
    assert config["eggs"][0]["keys"] == eggs.KONAMI
    assert eggs.clean(eggs.dumps(config), strict=True) == config


def test_the_client_config_leaves_out_disabled_eggs_and_badges():
    config = eggs.clean({"version": 1, "eggs": [{"id": "a", "keys": ["x", "y"], "badge": "4"},
                                                {"id": "b", "keys": ["x", "z"], "enabled": False}]})
    client = eggs.client_config(config, found_url="/f/{id}", page_url="/p", signed_in=True,
                                csrf_header="X-CSRF-Token", csrf_token="t")
    assert [egg["id"] for egg in client["eggs"]] == ["a"] and "badge" not in client["eggs"][0]
    assert client["csrf"] == {"header": "X-CSRF-Token", "token": "t"}


def test_script_json_cannot_close_the_script_element():
    text = eggs.script_json({"message": "</script><!-- &  "})
    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"message": "</script><!-- &  "}
