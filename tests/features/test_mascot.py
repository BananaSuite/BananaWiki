"""Mascot: who sees it, the sunglasses easter egg, the viewer's switch and the administrator's actions."""

import json
from pathlib import Path

import pytest

from .pages_support import set_feature


def _prefs(db, user):
    raw = db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],))
    return json.loads(raw or "{}")


LOGO = '<img class="topbar__logo" src="/static/favicons/banana_yellow.png?v='


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


def test_signed_in_people_see_the_mascot_visitors_do_not(app, client, make_user, login):
    assert "data-mascot" not in client.get("/login").get_data(as_text=True)
    login(client, make_user("mascot_viewer"))
    html = _home(client)
    assert "data-mascot" in html and "data-mascot-url" in html
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


# ── Integration into 1.7 (wording, plurals, audit, layout, motion, CSRF) ─────

MASCOT = Path(__file__).resolve().parents[2] / "bananawiki" / "wiki" / "features" / "mascot"


def _catalogue(lang):
    return json.loads((MASCOT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))


def test_english_strings_use_british_spelling_like_the_customise_button():
    english = _catalogue("en")
    assert not [key for key, text in english.items() if "Customiz" in text]
    assert "Customise" in english["mascot.admin.hint"] and "Customise" in english["js.mascot.unlocked"]


@pytest.mark.parametrize("action", ["show", "hide", "shades_on", "shades_off"])
def test_bulk_messages_have_a_singular_in_both_languages(action):
    key = f"mascot.admin.done.{action}_one"
    assert key in _catalogue("en") and key in _catalogue("it")


def test_bulk_message_for_one_account_is_singular(app, admin_client, make_user, login, db):
    alice = make_user("mascot_single")
    admin_client.post("/admin/appearance/mascot", data={"action": "hide"})
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", ('{"mascot_enabled": 1}', alice["id"]))
    page = admin_client.post("/admin/appearance/mascot", data={"action": "hide"},
                             follow_redirects=True).get_data(as_text=True)
    assert "Mascot hidden on 1 account." in page


def test_bulk_actions_are_audited_in_the_readers_language(app, admin_client, make_user, db):
    make_user("mascot_audited")
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    row = db.one("SELECT action, details FROM audit_log WHERE action LIKE 'mascot.%'")
    accounts = db.scalar("SELECT COUNT(*) FROM users")
    assert row["action"] == "mascot.everyone.shades_on" and json.loads(row["details"]) == {"count": accounts}
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "Sunglasses put on the mascot for every account" in page
    assert '<strong title="count">Count</strong>' in page
    italian = _catalogue("it")
    assert all(f"audit.action.mascot.everyone.{name}" in italian for name in ("show", "hide", "shades_on", "shades_off"))


def test_administrator_hint_is_spaced_from_its_buttons(admin_client):
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    section = page[page.index('id="mascot-admin-heading"'):]
    assert '<p class="hint hint--block">' in section[:400]


def test_idle_animations_stop_when_the_system_asks_for_reduced_motion():
    # The shell shortens animations to .01ms for prefers-reduced-motion; an infinite bob and blink
    # would still run (one frame each), so the mascot stops them itself.
    css = (MASCOT / "static" / "mascot.css").read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".mascot__body" in block and ".mascot__eyes" in block and "animation: none" in block


def test_mascot_requests_need_the_csrf_token(app, client, make_user, login, db):
    user = make_user("mascot_csrf")
    login(client, user)
    app.config["CSRF_DISABLED"] = False
    assert client.post("/mascot/shades", headers={"Accept": "application/json"}).status_code == 400
    assert client.post("/settings/mascot", data={"action": "hide"}).status_code == 400
    assert "mascot_enabled" not in _prefs(db, user) and "mascot_shades" not in _prefs(db, user)


def test_own_switch_needs_an_account(client):
    response = client.post("/settings/mascot", data={"action": "hide"})
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_on_phones_the_mascot_takes_no_more_room_than_the_logo():
    # 375 px: without the pull, the sprite's two empty left columns (4 px) and one more pixel pushed the
    # site name right, so a name that fitted beside the logo was cut ("Banana Labs W…"). They go under the
    # menu button's gap instead (physical left, as the art is not mirrored), and the name starts where it
    # does beside the 32 px logo, which bananawiki.css pulls by the same 5 px (test_site_icons).
    # This holds with a mouse; touch screens give the mascot the shell's touch size instead (pull request
    # #25, test_mascot_motion), 12 px wider than the sprite.
    css = (MASCOT / "static" / "mascot.css").read_text(encoding="utf-8")
    block = css[css.index("@media (max-width: 600px)"):]
    assert ".mascot { margin-left: -5px; }" in block[:block.index("}\n}") + 3]


def test_on_touch_phones_the_finger_sized_mascot_stays_clear_of_the_menu_button():
    # The phone rule pulls the mascot left into the top bar's gap; with the touch size of #25 the whole
    # 2.75rem box moves, so the pull must stay within the gap or the box would cover the menu button's edge.
    css = (MASCOT / "static" / "mascot.css").read_text(encoding="utf-8")
    shell = (Path(__file__).resolve().parents[2] / "bananawiki" / "core" / "static" / "css" / "bananawiki.css"
             ).read_text(encoding="utf-8")
    phone = shell[shell.index("@media (max-width: 600px) {\n  .topbar {"):]
    gap_rem = float(phone[:phone.index("}")].split("gap:")[1].split("rem")[0])
    block = css[css.index("@media (max-width: 600px)"):]
    pull_px = -float(block[:block.index("}")].split("margin-left:")[1].split("px")[0])
    assert "min-width: 2.75rem" in css[css.index("@media (pointer: coarse)"):]
    assert 0 < pull_px < gap_rem * 16


def test_the_button_icons_draw_the_banana_at_twice_its_grid():
    # Under "Customise" and in Appearance the sprite was 28 px, 1.75 screen pixels per art pixel: rows and
    # columns came out one or two pixels wide and the blinking eyes blurred. 32 px is twice the 16-pixel
    # grid, as in the top bar; the negative block margins keep the buttons as tall as the 28 px icon left them.
    from bananawiki.wiki.features.mascot.sprite import SIZE

    css = (MASCOT / "static" / "mascot.css").read_text(encoding="utf-8")
    rule = css.split(".mascot-button__sprite {")[1].split("}")[0]
    width = int(rule.split("width:")[1].split("px")[0])
    height = int(rule.split("height:")[1].split("px")[0])
    assert width == height and width % SIZE == 0
    assert width - 2 * int(rule.split("margin-block: -")[1].split("px")[0]) == 28
