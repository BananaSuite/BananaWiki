"""The portal's easter eggs (Settings → Easter eggs): each one can be switched off; the key-sequence eggs
an administrator designs, finds and the easter egg page."""

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


def test_the_key_sequences_can_be_switched_off(web, login, admin):
    assert 'id="banana-eggs"' in web.get("/login").get_data(as_text=True)
    _switch(web, login, admin, shades=True, konami=False, peek=True)
    page = web.get("/admin/settings").get_data(as_text=True)
    button = _button(page)
    assert "data-mascot-konami" not in button and f'data-mascot-clicks="{mascot.CLICKS_FOR_SHADES}"' in button
    assert 'id="banana-eggs"' not in page
    # The editor still previews: the settings page loads the runtime itself.
    assert "/static/eggs/banana-eggs.js" in page and "/static/eggs/banana-eggs-admin.js" in page


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


# Key-sequence eggs (banana-eggs/v1)

import json  # noqa: E402

from bananawiki.core import eggs  # noqa: E402
from bananawiki.hosting.db import connection_scope  # noqa: E402

CONFIG = re.compile(r'<script type="application/json" id="banana-eggs">(.*?)</script>', re.S)


def _config(web, path="/login"):
    match = CONFIG.search(web.get(path).get_data(as_text=True))
    return json.loads(match.group(1)) if match else None


def _egg(egg_id, **values):
    return {**eggs.default_egg(), "id": egg_id, "keys": ["x", egg_id[0]], **values}


def _store(query, *items):
    query("UPDATE hosting_settings SET easter_egg_configs = ? WHERE id = 1",
          (eggs.dumps({"version": 1, "eggs": list(items)}),))


def _save_list(web, value, **switches):
    form = {"action": "save_easter_eggs", "easter_egg_configs": value,
            **{f"easter_{egg}": "1" for egg in ("shades", "konami", "peek") if switches.get(egg, True)}}
    return web.post("/admin/settings", data=form)


def test_visitors_get_the_default_egg_and_keep_their_finds_in_the_browser(web):
    config = _config(web)
    assert [egg["id"] for egg in config["eggs"]] == ["konami"]
    assert config["signedIn"] is False and config["foundUrl"] is None and config["pageUrl"] == "/easter-egg"


def test_signed_in_accounts_record_finds_once(web, make_account, login, query):
    member = make_account("finder")
    login(web, member)
    config = _config(web, "/account")
    assert config["signedIn"] is True and config["foundUrl"] == "/easter-eggs/{id}/found"
    assert config["csrf"]["header"] == "X-CSRF-Token"
    assert web.post("/easter-eggs/konami/found", json={}).get_json() == {"ok": True, "new": True}
    assert web.post("/easter-eggs/konami/found", json={}).get_json() == {"ok": True, "new": False}
    assert query("SELECT account_id, egg_id FROM account_egg_finds") == [{"account_id": member["id"], "egg_id": "konami"}]
    events = query("SELECT action, reason FROM hosting_events WHERE action = 'easter_egg.found'")
    assert events == [{"action": "easter_egg.found", "reason": "konami"}]


def test_finds_need_an_account_and_an_egg_that_can_be_found(web, make_account, login, query):
    assert web.post("/easter-eggs/konami/found", json={}).status_code in (302, 401)
    _store(query, _egg("on"), _egg("off", enabled=False), _egg("quiet", record=False))
    login(web, make_account("finder"))
    for egg_id in ("nope", "off", "quiet", "konami"):
        assert web.post(f"/easter-eggs/{egg_id}/found", json={}).status_code == 404
    assert web.post("/easter-eggs/on/found", json={}).status_code == 200
    query("UPDATE hosting_settings SET easter_konami = 0 WHERE id = 1")
    assert web.post("/easter-eggs/on/found", json={}).status_code == 404


def test_finds_are_rate_limited_per_account(web, make_account, login):
    login(web, make_account("finder"))
    assert [web.post("/easter-eggs/nope/found", json={}).status_code for _ in range(10)] == [404] * 10
    assert web.post("/easter-eggs/konami/found", json={}).status_code == 429


def test_finds_need_the_csrf_header(tmp_path):
    from .hosting_support import PASSWORD, build_portal, csrf_token

    portal = build_portal(tmp_path / "csrf", csrf=True)
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts

        accounts.create("finder", PASSWORD, is_admin=False, email="")
    client = portal.test_client()
    assert client.post("/login", data={"username": "finder", "password": PASSWORD,
                                       "csrf_token": csrf_token(client)}).status_code == 302
    refused = client.post("/easter-eggs/konami/found", json={})
    assert refused.status_code in (400, 302) and refused.get_json(silent=True) is None, "the portal's CSRF answer"
    config = _config(client, "/account")
    response = client.post("/easter-eggs/konami/found", json={},
                           headers={config["csrf"]["header"]: config["csrf"]["token"]})
    assert response.get_json() == {"ok": True, "new": True}


def test_the_page_lists_finds_and_tells_visitors_their_browser_keeps_theirs(web, make_account, login, query):
    _store(query, _egg("one"), _egg("two"))
    visitor = web.get("/easter-egg").get_data(as_text=True)
    assert "0 of 2 found" in visitor and "data-local-finds" in visitor and "/static/js/easter-egg.js" in visitor
    assert 'id="egg-one"' in visitor and 'data-count-template="{found} of {total} found"' in visitor
    login(web, make_account("finder"))
    web.post("/easter-eggs/two/found", json={})
    page = web.get("/easter-egg").get_data(as_text=True)
    assert "1 of 2 found" in page and "Found: two" in page and "data-local-finds" not in page
    query("UPDATE hosting_settings SET easter_konami = 0 WHERE id = 1")
    assert web.get("/easter-egg").status_code == 404


def test_the_editor_saves_a_clean_list(web, login, admin, query):
    login(web, admin)
    page = web.get("/admin/settings").get_data(as_text=True)
    assert 'data-banana-eggs-editor data-input="easter-egg-list"' in page
    options = json.loads(re.search(r"data-banana-eggs-editor-options>(.*?)</script>", page, re.S).group(1))
    assert options["badges"] == [] and options["reset"] == eggs.default_config()["eggs"]
    party = _egg("party", keys=["P", "a"])
    assert _save_list(web, json.dumps({"version": 1, "eggs": [party]})).status_code == 302
    stored = json.loads(query("SELECT easter_egg_configs FROM hosting_settings", one=True)["easter_egg_configs"])
    assert stored["eggs"][0]["id"] == "party" and stored["eggs"][0]["keys"] == ["p", "a"]
    assert [egg["id"] for egg in _config(web, "/admin/settings")["eggs"]] == ["party"]


def test_a_wrong_list_is_refused_naming_each_problem(web, login, admin, query):
    login(web, admin)
    wrong = {"version": 1, "eggs": [{**_egg("bad"), "keys": ["x"]}]}
    response = _save_list(web, json.dumps(wrong))
    assert "open=save_easter_eggs" in response.headers["Location"]
    page = web.get(response.headers["Location"]).get_data(as_text=True)
    assert "Easter egg 1 · Key sequence: needs 2 to 32 keys" in page
    assert query("SELECT easter_egg_configs FROM hosting_settings", one=True)["easter_egg_configs"] is None


def test_only_administrators_save_the_list(web, make_account, login, query):
    login(web, make_account("member"))
    assert _save_list(web, "[]").status_code == 403
    assert query("SELECT easter_egg_configs FROM hosting_settings", one=True)["easter_egg_configs"] is None


def test_saving_the_settings_page_keeps_the_default_list_unsaved(web, login, admin, query):
    login(web, admin)
    page = web.get("/admin/settings").get_data(as_text=True)
    current = re.search(r'id="easter-egg-list" name="easter_egg_configs" value="([^"]*)"', page).group(1)
    import html

    assert _save_list(web, html.unescape(current)).status_code == 302
    assert query("SELECT easter_egg_configs FROM hosting_settings", one=True)["easter_egg_configs"] is None
    events = query("SELECT reason FROM hosting_events WHERE action = 'settings.changed'")
    assert not any("easter_egg_configs" in event["reason"] for event in events)
