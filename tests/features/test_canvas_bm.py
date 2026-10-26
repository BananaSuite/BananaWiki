"""Canvases with BM mode's fewer tools: what the page leaves out, for a whole wiki in BM mode and for one account."""

from __future__ import annotations

import re

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import service

from .test_bm_mode import _save

SIMPLE_RULE = '[data-bm-hide~="canvas.simple_tools"]{display:none!important}'
SNAP_HELP = "G turns snapping to the grid on or off"
GROUP_HELP = "Ctrl+G groups the selection"
UNLOCK_HELP = "L unlocks the selection when all of it is locked"


@pytest.fixture
def easy(app_factory):
    return app_factory(environ={"BW_EASY_WIKI": "1"})


def _canvas_page(app, make_user, login, name, **fields):
    """The canvas page of a new canvas, seen by the editor who made it."""
    person = make_user(name, role="editor", **fields)
    with app.test_request_context(), connection_scope():
        canvas = service.create("Class map", "", person["id"])
    client = app.test_client()
    login(client, person)
    response = client.get(f"/canvas/{canvas['slug']}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _simplified(page):
    """The simplifications the page names on its root element (``<html data-bm>``), which the editor reads."""
    root = re.search(r"<html[^>]*>", page).group(0)
    found = re.search(r'\sdata-bm="([^"]*)"', root)
    return found.group(1).split() if found else []


@pytest.fixture(params=["wiki", "account"])
def bm_page(request, app, easy, make_user, login):
    """The canvas page for an editor in BM mode: the whole wiki's, or their account's only."""
    if request.param == "wiki":
        return _canvas_page(easy, make_user, login, "bmwiki")
    return _canvas_page(app, make_user, login, "bmaccount", bm_mode=1)


def test_fewer_tools_leave_out_the_minimap_the_arrange_menu_and_their_keys(bm_page):
    assert SIMPLE_RULE in bm_page
    assert "canvas.simple_tools" in _simplified(bm_page)
    assert re.search(r'id="canvas-app"[^>]*\sdata-bm=', bm_page) is None, "one marker, on the page's root"
    # Left out rather than hidden: the minimap would show itself, the empty group would keep its divider.
    assert "data-minimap" not in bm_page and "data-arrange-menu" not in bm_page
    # The keyboard help, read out through the stage's description, lists only keys that still work.
    assert 'aria-describedby="canvas-keys"' in bm_page and 'id="canvas-keys"' in bm_page
    assert SNAP_HELP not in bm_page and GROUP_HELP not in bm_page
    assert UNLOCK_HELP in bm_page and "guide lines appear" in bm_page


def test_fewer_tools_keep_saving_as_an_image(bm_page):
    # One button for the simplest format: a PNG, without the menu that also offers SVG.
    assert re.search(r'<button type="button" class="btn btn--small " data-action="export-png"[^>]*>', bm_page)
    assert 'data-action="export-svg"' not in bm_page and "canvas-menu__panel" not in bm_page
    # The canvas file export, for importing elsewhere, stays hidden.
    assert re.search(r'href="/canvas/[^"]+/export" data-bm-hide="canvas.simple_tools"', bm_page)


def test_fewer_tools_keep_the_text_outline(bm_page):
    # The outline is the canvas for screen readers: its link stays in the page header.
    assert re.search(r'<a class="btn btn--small" href="/canvas/[^"]+/outline">', bm_page)


def test_the_full_canvas_keeps_every_tool(app, make_user, login):
    page = _canvas_page(app, make_user, login, "fullcanvas")
    assert SIMPLE_RULE not in page and 'data-bm="' not in page
    assert "data-minimap" in page and "data-arrange-menu" in page
    assert SNAP_HELP in page and GROUP_HELP in page and UNLOCK_HELP not in page
    assert 'data-action="export-svg"' in page and 'class="canvas-menu__item" data-action="export-png"' in page


def test_switching_fewer_tools_off_gives_the_whole_canvas_back(easy, admin, make_user, login):
    chief = easy.test_client()
    login(chief, admin)
    assert _save(chief, mode=True, features=("canvas",), off=("canvas.simple_tools",)).status_code == 302
    page = _canvas_page(easy, make_user, login, "bmoff")
    assert SIMPLE_RULE not in page and "canvas.simple_tools" not in _simplified(page)
    assert "data-minimap" in page and "data-arrange-menu" in page and SNAP_HELP in page
