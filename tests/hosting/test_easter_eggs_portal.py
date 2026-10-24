"""The portal's easter eggs (Settings → Easter eggs): each one can be switched off."""

from __future__ import annotations

import re

import pytest

from bananawiki.core import mascot


def _button(page: str) -> str:
    match = re.search(r'<button type="button" class="mascot"[^>]*>', page)
    assert match, "the mascot is in the top bar"
    return match.group(0)


def _switch(web, login, admin, **eggs):
    login(web, admin)
    form = {"action": "save_easter_eggs", **{f"easter_{egg}": "1" for egg, on in eggs.items() if on}}
    assert web.post("/admin/settings", data=form).status_code == 302


@pytest.fixture
def admin(make_account):
    return make_account("chief", admin=True)


def test_every_easter_egg_is_on_by_default(web, query):
    row = query("SELECT easter_shades, easter_konami, easter_peek FROM hosting_settings", one=True)
    assert row == {"easter_shades": 1, "easter_konami": 1, "easter_peek": 1}
    button = _button(web.get("/login").get_data(as_text=True))
    assert f'data-mascot-clicks="{mascot.CLICKS_FOR_SHADES}"' in button and "data-mascot-konami" not in button
    assert 'class="mascot-peek"' in web.get("/no-such-page").get_data(as_text=True)


def test_the_sunglasses_can_be_switched_off(web, login, admin):
    _switch(web, login, admin, shades=False, konami=True, peek=True)
    button = _button(web.get("/admin/settings").get_data(as_text=True))
    assert 'data-mascot-clicks="0"' in button, "no number of clicks puts them on"
    assert "data-mascot-konami" not in button


def test_the_banana_shower_can_be_switched_off(web, login, admin):
    _switch(web, login, admin, shades=True, konami=False, peek=True)
    button = _button(web.get("/admin/settings").get_data(as_text=True))
    assert 'data-mascot-konami="off"' in button
    assert f'data-mascot-clicks="{mascot.CLICKS_FOR_SHADES}"' in button


def test_the_peek_into_page_not_found_can_be_switched_off(web, login, admin):
    _switch(web, login, admin, shades=True, konami=True, peek=False)
    response = web.get("/no-such-page")
    assert response.status_code == 404 and 'class="mascot-peek"' not in response.get_data(as_text=True)


def test_only_administrators_change_them(web, make_account, login, query):
    login(web, make_account("member"))
    assert web.post("/admin/settings", data={"action": "save_easter_eggs"}).status_code == 403
    assert query("SELECT easter_konami FROM hosting_settings", one=True)["easter_konami"] == 1


def test_the_settings_page_lists_them_in_every_language(web, login, admin):
    login(web, admin)
    for language, label in (("en", "Easter eggs"), ("it", "Easter egg"), ("de", "Easter Eggs")):
        web.post("/language", data={"language": language})
        page = web.get("/admin/settings").get_data(as_text=True)
        assert f"<summary>{label}</summary>" in page
        for egg in ("shades", "konami", "peek"):
            assert f'name="easter_{egg}" value="1" checked' in page
