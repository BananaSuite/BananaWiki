"""Mascot: who sees it, the sunglasses easter egg, taking them off and the administrator's actions."""

import json
import re
from pathlib import Path

import pytest

from .pages_support import set_feature


def _prefs(db, user):
    raw = db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],))
    return json.loads(raw or "{}")


LOGO = '<img class="topbar__logo" src="/static/favicons/banana_yellow.png?v='


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


def _section(page, heading_id):
    """The mascot's ``<section>`` of *page*, from its heading to its end."""
    section = page[page.index(f'id="{heading_id}"'):]
    return section[:section.index("</section>")]


def _actions(section):
    return re.findall(r'name="action" value="([^"]+)"', section)


def test_signed_in_people_see_the_mascot_visitors_do_not(app, client, make_user, login):
    assert "data-mascot" not in client.get("/login").get_data(as_text=True)
    login(client, make_user("mascot_viewer"))
    html = _home(client)
    assert "data-mascot" in html and "data-mascot-url" in html
    assert "mascot-sprite--shades" not in html
    # The mascot takes the logo's place.
    assert LOGO not in html
    assert html.index("data-mascot") < html.index('class="topbar__brand"')


def test_switched_off_feature_hides_everything(app, client, make_user, login, db):
    # The plugin switch is the only way to remove the mascot; the logo comes back for everyone.
    user = make_user("mascot_off", role="admin", accessibility='{"mascot_shades": 1}')
    set_feature(app, "mascot", False)
    login(client, user)
    html = _home(client)
    assert "data-mascot" not in html and LOGO in html
    assert client.post("/mascot/shades").status_code == 404
    assert client.post("/settings/mascot", data={"action": "shades_off"}).status_code == 404
    assert client.post("/admin/appearance/mascot", data={"action": "shades_off"}).status_code == 404
    assert 'id="mascot-heading"' not in client.get("/settings/display").get_data(as_text=True)
    assert "mascot-admin-heading" not in client.get("/admin/appearance").get_data(as_text=True)
    # Switched on again, it is back with the sunglasses the account had.
    set_feature(app, "mascot", True)
    html = _home(client)
    assert "data-mascot" in html and "mascot-sprite--shades" in html and LOGO not in html
    assert _prefs(db, user)["mascot_shades"] == 1


@pytest.mark.parametrize("saved", ['{"mascot_enabled": 0}', '{"mascot_enabled": "0", "theme_mode": "dark"}'])
def test_a_mascot_hidden_in_1_7_0_is_shown_again(app, client, make_user, login, db, saved):
    # 1.7.0 let people hide it (mascot_enabled 0); that choice is ignored.
    user = make_user("mascot_was_hidden", accessibility=saved)
    login(client, user)
    html = _home(client)
    assert "data-mascot" in html and LOGO not in html
    # It can earn its sunglasses like anyone else's.
    response = client.post("/mascot/shades", headers={"Accept": "application/json"})
    assert response.status_code == 200 and _prefs(db, user)["mascot_shades"] == 1
    assert "mascot-sprite--shades" in _home(client)


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


def test_customise_cannot_hide_it_and_offers_only_taking_the_sunglasses_off(app, client, make_user, login, db):
    user = make_user("mascot_shy")
    login(client, user)
    # Without sunglasses there is nothing to change: no section at all, which keeps the easter egg a surprise.
    display = client.get("/settings/display").get_data(as_text=True)
    assert 'id="mascot-heading"' not in display and "/settings/mascot" not in display
    # There is no switch to hide or show it: those actions answer 400 and write nothing.
    for action in ("hide", "show", "shades_on"):
        assert client.post("/settings/mascot", data={"action": action}).status_code == 400
    assert "mascot_enabled" not in _prefs(db, user)
    assert "data-mascot" in _home(client)
    # With the sunglasses on, the section offers just taking them off.
    client.post("/mascot/shades")
    display = client.get("/settings/display").get_data(as_text=True)
    section = _section(display, "mascot-heading")
    assert _actions(section) == ["shades_off"] and ">Take the sunglasses off<" in section
    assert "Hide the mascot" not in display and "Show the mascot" not in display


def test_mascot_needs_an_account(app, client):
    response = client.post("/mascot/shades")
    assert response.status_code in (302, 401)
    assert client.delete("/mascot/shades").status_code in (302, 401)


def test_as_many_clicks_again_take_the_sunglasses_off(app, client, make_user, login, db):
    user = make_user("mascot_uncool", accessibility='{"theme_mode": "dark"}')
    login(client, user)
    client.post("/mascot/shades", headers={"Accept": "application/json"})
    response = client.delete("/mascot/shades", headers={"Accept": "application/json"})
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert _prefs(db, user) == {"theme_mode": "dark", "mascot_shades": 0}
    assert "mascot-sprite--shades" not in _home(client)


def _peek(html):
    card = html[html.index('class="card card--narrow error-page"'):]
    card = card[:card.index("</div>")]
    match = re.search(r'<span class="mascot-peek" aria-hidden="true"><svg class="([^"]+)"', card)
    return match.group(1) if match else None


def test_the_mascot_peeks_into_the_page_not_found_card(app, client, make_user, login, db):
    assert _peek(client.get("/no-such-address").get_data(as_text=True)) is None
    user = make_user("mascot_lost")
    login(client, user)
    page = client.get("/no-such-address")
    assert page.status_code == 404 and _peek(page.get_data(as_text=True)) == "mascot-sprite"
    client.post("/mascot/shades")
    assert _peek(client.get("/no-such-address").get_data(as_text=True)) == "mascot-sprite mascot-sprite--shades"
    # Only on "not found": other errors keep their plain card.
    assert client.get("/admin/appearance").status_code == 403
    assert _peek(client.get("/admin/appearance").get_data(as_text=True)) is None
    # Nor for accounts the wiki does not let in, nor with the feature off.
    db.execute("UPDATE users SET approval_status = 'pending' WHERE id = ?", (user["id"],))
    assert _peek(client.get("/no-such-address").get_data(as_text=True)) is None
    db.execute("UPDATE users SET approval_status = 'approved' WHERE id = ?", (user["id"],))
    set_feature(app, "mascot", False)
    assert _peek(client.get("/no-such-address").get_data(as_text=True)) is None


def test_display_preferences_do_not_touch_the_mascot(app, client, make_user, login, db):
    user = make_user("mascot_prefs")
    login(client, user)
    client.post("/mascot/shades")
    # The 1.4 JSON endpoint cannot set the sunglasses, nor bring back the old switch...
    client.post("/api/accessibility", json={"mascot_shades": 0, "mascot_enabled": 0, "theme_mode": "light"})
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "light" and prefs["mascot_shades"] == 1 and "mascot_enabled" not in prefs
    assert "mascot_enabled" not in client.get("/api/accessibility").get_json()
    # ...saving the display form keeps them, and drops the switch 1.7.0 saved...
    db.execute("UPDATE users SET accessibility = json_set(accessibility, '$.mascot_enabled', 0) WHERE id = ?",
               (user["id"],))
    client.post("/settings/display", data={"theme_mode": "dark"})
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "dark" and prefs["mascot_shades"] == 1 and "mascot_enabled" not in prefs
    assert "data-mascot" in _home(client)
    # ...and resetting the display settings keeps them too.
    client.post("/settings/display/reset")
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "default" and prefs["mascot_shades"] == 1 and "mascot_enabled" not in prefs


def test_only_administrators_change_everyone(app, client, make_user, login, db):
    editor = make_user("mascot_editor", role="editor")
    login(client, editor)
    assert client.post("/admin/appearance/mascot", data={"action": "shades_on"}).status_code == 403
    assert "mascot_shades" not in _prefs(db, editor)


def test_administrator_actions_change_every_account(app, admin_client, make_user, db, admin):
    alice, bob = make_user("mascot_alice"), make_user("mascot_bob")
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", ('{"theme_mode": "light"}', bob["id"]))
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    # Only the sunglasses: everyone signed in sees the mascot while the plugin is on.
    section = _section(page, "mascot-admin-heading")
    assert _actions(section) == ["shades_on", "shades_off"]
    assert "Show to everyone" not in section and "Hide for everyone" not in section
    assert "under Appearance" in section and "switch it back on" not in section

    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    for user in (alice, bob, admin):
        assert _prefs(db, user)["mascot_shades"] == 1
    assert _prefs(db, bob)["theme_mode"] == "light"
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_off"})
    assert all(_prefs(db, user)["mascot_shades"] == 0 for user in (alice, bob, admin))

    # Administrators cannot show or hide it for everyone: 400, nothing written and nothing audited.
    for action in ("show", "hide", "explode"):
        assert admin_client.post("/admin/appearance/mascot", data={"action": action}).status_code == 400
    assert all("mascot_enabled" not in _prefs(db, user) for user in (alice, bob, admin))
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action IN (?, ?)",
                     ("mascot.everyone.show", "mascot.everyone.hide")) == 0
    assert "data-mascot" in _home(admin_client)


# Wording, plurals, audit log, layout, motion and CSRF

PACKAGE = Path(__file__).resolve().parents[2] / "bananawiki"
def _catalogue(lang):
    return json.loads((PACKAGE / "wiki" / "features" / "mascot" / "translations" / f"{lang}.json").read_text(encoding="utf-8"))


def test_bulk_actions_are_audited_in_the_readers_language(app, admin_client, make_user, db):
    make_user("mascot_audited")
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    row = db.one("SELECT action, details FROM audit_log WHERE action LIKE 'mascot.%'")
    accounts = db.scalar("SELECT COUNT(*) FROM users")
    assert row["action"] == "mascot.everyone.shades_on" and json.loads(row["details"]) == {"count": accounts}
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "Sunglasses put on the mascot for every account" in page
    assert '<strong title="count">Count</strong>' in page
    # Audit entries from 1.7.0 can still name the show and hide actions.
    for lang in ("en", "it", "de"):
        strings = _catalogue(lang)
        assert all(f"audit.action.mascot.everyone.{name}" in strings
                   for name in ("show", "hide", "shades_on", "shades_off")), lang


def test_mascot_requests_need_the_csrf_token(app, client, make_user, login, db):
    user = make_user("mascot_csrf")
    login(client, user)
    app.config["CSRF_DISABLED"] = False
    assert client.post("/mascot/shades", headers={"Accept": "application/json"}).status_code == 400
    assert client.delete("/mascot/shades", headers={"Accept": "application/json"}).status_code == 400
    assert client.post("/settings/mascot", data={"action": "shades_off"}).status_code == 400
    assert "mascot_shades" not in _prefs(db, user)


def test_taking_the_sunglasses_off_needs_an_account(client):
    response = client.post("/settings/mascot", data={"action": "shades_off"})
    assert response.status_code == 302 and "/login" in response.headers["Location"]
