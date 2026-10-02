"""Both applications serve and version one design system without changing URLs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Blueprint

from bananawiki import __version__
from bananawiki.core import assets
from tests.hosting.hosting_support import build_portal

STYLESHEET = "css/bananawiki.css"


@pytest.fixture(params=["wiki", "hosting"])
def application(request, tmp_path):
    if request.param == "wiki":
        return request.getfixturevalue("app")
    return build_portal(tmp_path)


def _url(application, filename=STYLESHEET, **kwargs):
    with application.test_request_context():
        return assets.asset_url(filename, **kwargs)


def test_design_system_uses_the_canonical_source(application):
    source = (assets.SHARED_STATIC_ROOT / STYLESHEET).read_bytes()
    url = _url(application)
    assert urlsplit(url).path == "/static/css/bananawiki.css"
    assert parse_qs(urlsplit(url).query)["v"] == [hashlib.sha256(source).hexdigest()[:10]]
    response = application.test_client().get(url)
    assert response.status_code == 200
    assert response.mimetype == "text/css"
    assert response.data == source
    assert response.headers["Content-Security-Policy"]
    assert url.encode() in application.test_client().get("/login").data


def test_shared_assets_keep_static_cache_and_conditional_requests(application):
    application.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    client = application.test_client()
    url = _url(application)
    response = client.get(url)
    assert response.cache_control.public
    assert response.cache_control.max_age == 3600
    assert response.headers["ETag"]
    conditional = client.get(url, headers={"If-None-Match": response.headers["ETag"]})
    assert conditional.status_code == 304
    assert not conditional.data
    head = client.head(url)
    assert head.status_code == 200 and not head.data
    assert head.content_length == len(response.data)


def test_shared_source_edits_change_both_the_response_and_version(application, tmp_path, monkeypatch):
    root = tmp_path / "shared"
    source = root / STYLESHEET
    source.parent.mkdir(parents=True)
    source.write_bytes(b"body { color: red; }")
    monkeypatch.setattr(assets, "SHARED_STATIC_ROOT", root)
    before = _url(application)
    assert application.test_client().get(before).data == source.read_bytes()
    source.write_bytes(b"body { color: blue; }")
    after = _url(application)
    assert after != before
    assert application.test_client().get(after).data == source.read_bytes()


def test_application_assets_still_use_their_own_static_folder(application):
    source = Path(application.static_folder) / "js/bananawiki.js"
    url = _url(application, "js/bananawiki.js")
    assert parse_qs(urlsplit(url).query)["v"] == [hashlib.sha256(source.read_bytes()).hexdigest()[:10]]
    assert application.test_client().get(url).data == source.read_bytes()


def test_missing_files_and_traversal_are_not_shared(application):
    client = application.test_client()
    for path in ("/static/css/missing.css", "/static/../core/assets.py", "/static/css/../../assets.py"):
        assert client.get(path).status_code == 404
    assert parse_qs(urlsplit(_url(application, "missing.css")).query)["v"] == [__version__]


def test_blueprint_assets_keep_their_own_source_even_with_a_shared_filename(app, tmp_path):
    root = tmp_path / "plugin-static"
    source = root / STYLESHEET
    source.parent.mkdir(parents=True)
    source.write_bytes(b".plugin { display: block; }")
    app.register_blueprint(Blueprint("asset_plugin", __name__, static_folder=str(root),
                                     static_url_path="/static/asset-plugin"))
    url = _url(app, blueprint="asset_plugin")
    assert urlsplit(url).path == "/static/asset-plugin/css/bananawiki.css"
    assert parse_qs(urlsplit(url).query)["v"] == [hashlib.sha256(source.read_bytes()).hexdigest()[:10]]
    assert app.test_client().get(url).data == source.read_bytes()
