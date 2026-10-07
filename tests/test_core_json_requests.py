"""Malformed JSON is rejected before it reaches storage or recursive consumers."""

from __future__ import annotations

import json

import pytest
from flask import jsonify
from flask import request as flask_request

from bananawiki.core.json import MAX_JSON_DEPTH
from bananawiki.hosting import auth as hosting_auth
from bananawiki.wiki import auth
from tests.hosting.hosting_support import build_portal


@pytest.fixture(params=["wiki", "hosting"])
def json_app(request, tmp_path):
    app = request.getfixturevalue("app") if request.param == "wiki" else build_portal(tmp_path)

    @app.post("/_json_probe")
    @auth.public
    @hosting_auth.public
    def probe():
        body = flask_request.get_json(silent=True)
        return (jsonify(body), 200) if isinstance(body, dict) else (jsonify({"error": "invalid JSON"}), 400)

    return app


@pytest.mark.parametrize("body", [
    b'{"text":"\\ud800"}',
    b'{"text":"\\udfff"}',
    b'{"\\ud800":"key"}',
    b'{"nested":[{"text":"\\udfff"}]}',
    b'{"number":NaN}',
    b'{"number":Infinity}',
    b'{"number":-Infinity}',
    b'{"number":1e9999}',
    b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
], ids=["high-surrogate", "low-surrogate", "object-key", "nested-surrogate", "nan", "infinity",
        "negative-infinity", "overflow", "deep-nesting"])
def test_malformed_json_is_a_bad_request(json_app, body):
    response = json_app.test_client().post("/_json_probe", data=body, content_type="application/json")
    assert response.status_code == 400


def test_valid_unicode_and_nested_documents_are_preserved(json_app):
    body = {"text": "Italiano: caffè · 日本語 · 🍌", "nested": [{"values": [None, True, 2, 1.5]}]}
    response = json_app.test_client().post("/_json_probe", data=json.dumps(body), content_type="application/json")
    assert response.status_code == 200
    assert response.json == body


@pytest.mark.parametrize("depth, status", [(MAX_JSON_DEPTH - 1, 200), (MAX_JSON_DEPTH, 400)])
def test_json_nesting_boundary(json_app, depth, status):
    body = b'{"nested":' + b'[' * depth + b'0' + b']' * depth + b'}'
    response = json_app.test_client().post("/_json_probe", data=body, content_type="application/json")
    assert response.status_code == status


def test_excessive_nesting_before_csrf_is_a_bad_request(csrf_client):
    body = b'{"csrf_token":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    response = csrf_client.post("/login", data=body, content_type="application/json")
    assert response.status_code == 400
