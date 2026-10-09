"""Mascot: who sees it, the sunglasses easter egg, the viewer's switch and the administrator's actions."""

import json

from .pages_support import set_feature


def _prefs(db, user):
    raw = db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],))
    return json.loads(raw or "{}")


LOGO = 'banana_yellow.png" alt="" width="24" height="24">'


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


def test_signed_in_people_see_the_mascot_visitors_do_not(app, client, make_user, login):
    assert "data-mascot" not in client.get("/login").get_data(as_text=True)
    login(client, make_user("mascot_viewer"))
    html = _home(client)
    assert "data-mascot" in html and "mascot-config" in html
    assert "mascot-sprite--shades" not in html
    # The mascot takes the logo's place.
    assert LOGO not in html
    assert html.index("data-mascot") < html.index('class="topbar__brand"')


def test_switched_off_feature_hides_everything(app, client, make_user, login):
    set_feature(app, "mascot", False)
    login(client, make_user("mascot_off"))
    html = _home(client)
    assert "data-mascot" not in html and LOGO in html
    assert client.post("/mascot/shades").status_code == 404
    assert client.post("/settings/mascot", data={"action": "hide"}).status_code == 404
    assert 'id="mascot-heading"' not in client.get("/settings/display").get_data(as_text=True)


def test_eleventh_click_puts_sunglasses_on_until_taken_off(app, client, make_user, login, db):
    user = make_user("mascot_cool")
    login(client, user)
    response = client.post("/mascot/shades", headers={"Accept": "application/json"})
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert _prefs(db, user)["mascot_shades"] == 1
    assert "mascot-sprite--shades" in _home(client)
    # Signing out and back in keeps them: they are saved on the account.
    client.post("/logout")
    login(client, user)
    assert "mascot-sprite--shades" in _home(client)
    display = client.get("/settings/display").get_data(as_text=True)
    assert "shades_off" in display
    client.post("/settings/mascot", data={"action": "shades_off"})
    assert _prefs(db, user)["mascot_shades"] == 0
    assert "mascot-sprite--shades" not in _home(client)


def test_own_switch_hides_and_shows(app, client, make_user, login, db):
    user = make_user("mascot_shy")
    login(client, user)
    assert client.post("/settings/mascot", data={"action": "hide"}).status_code == 302
    assert _prefs(db, user)["mascot_enabled"] == 0
    html = _home(client)
    assert "data-mascot" not in html and LOGO in html
    # A hidden mascot cannot be clicked, so it cannot earn sunglasses either.
    assert client.post("/mascot/shades").status_code == 409
    client.post("/settings/mascot", data={"action": "show"})
    assert "data-mascot" in _home(client)
    assert client.post("/settings/mascot", data={"action": "shades_on"}).status_code == 400


def test_mascot_needs_an_account(app, client):
    response = client.post("/mascot/shades")
    assert response.status_code in (302, 401)


def test_display_preferences_do_not_touch_the_mascot(app, client, make_user, login, db):
    user = make_user("mascot_prefs")
    login(client, user)
    client.post("/mascot/shades")
    client.post("/settings/mascot", data={"action": "hide"})
    # The 1.4 JSON endpoint cannot set the mascot keys...
    client.post("/api/accessibility", json={"mascot_shades": 0, "mascot_enabled": 1, "theme_mode": "light"})
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "light" and prefs["mascot_shades"] == 1 and prefs["mascot_enabled"] == 0
    # ...saving the display form keeps them...
    client.post("/settings/display", data={"theme_mode": "dark"})
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "dark" and prefs["mascot_shades"] == 1 and prefs["mascot_enabled"] == 0
    # ...and so does resetting the display settings.
    client.post("/settings/display/reset")
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "default" and prefs["mascot_shades"] == 1 and prefs["mascot_enabled"] == 0


def test_only_administrators_change_everyone(app, client, make_user, login, db):
    editor = make_user("mascot_editor", role="editor")
    login(client, editor)
    assert client.post("/admin/appearance/mascot", data={"action": "hide"}).status_code == 403
    assert _prefs(db, editor).get("mascot_enabled", 1) == 1


def test_administrator_actions_change_every_account(app, admin_client, make_user, db, admin):
    alice, bob = make_user("mascot_alice"), make_user("mascot_bob")
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", ('{"theme_mode": "light"}', bob["id"]))
    assert "mascot-admin-heading" in admin_client.get("/admin/appearance").get_data(as_text=True)

    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    for user in (alice, bob, admin):
        assert _prefs(db, user)["mascot_shades"] == 1
    assert _prefs(db, bob)["theme_mode"] == "light"

    admin_client.post("/admin/appearance/mascot", data={"action": "hide"})
    assert all(_prefs(db, user)["mascot_enabled"] == 0 for user in (alice, bob, admin))
    assert "data-mascot" not in _home(admin_client)

    admin_client.post("/admin/appearance/mascot", data={"action": "show"})
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_off"})
    assert all(_prefs(db, user)["mascot_enabled"] == 1 and _prefs(db, user)["mascot_shades"] == 0
               for user in (alice, bob, admin))
    assert admin_client.post("/admin/appearance/mascot", data={"action": "explode"}).status_code == 400


def test_sprite_variants():
    from bananawiki.wiki.features.mascot import sprite

    normal, shades, off = (str(sprite.svg(v)) for v in sprite.VARIANTS)
    assert "mascot__shades" in normal and "mascot-sprite--shades" not in normal
    assert "mascot-sprite--shades" in shades
    assert "currentColor" in off and "mascot__eyes" not in off
