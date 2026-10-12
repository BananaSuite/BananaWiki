"""Mascot: who sees it, the sunglasses easter egg, taking them off and the administrator's actions."""

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki.features.audit import record

from .pages_support import in_app, set_feature


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
def test_a_mascot_hidden_by_an_earlier_build_is_shown_again(app, client, make_user, login, db, saved):
    # Earlier builds of 1.7 let people hide it (mascot_enabled 0); that choice is ignored now.
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
    # The switch is gone: hiding or showing it answers 400 and writes nothing.
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


def test_display_preferences_do_not_touch_the_mascot(app, client, make_user, login, db):
    user = make_user("mascot_prefs")
    login(client, user)
    client.post("/mascot/shades")
    # The 1.4 JSON endpoint cannot set the sunglasses, nor bring back the old switch...
    client.post("/api/accessibility", json={"mascot_shades": 0, "mascot_enabled": 0, "theme_mode": "light"})
    prefs = _prefs(db, user)
    assert prefs["theme_mode"] == "light" and prefs["mascot_shades"] == 1 and "mascot_enabled" not in prefs
    assert "mascot_enabled" not in client.get("/api/accessibility").get_json()
    # ...saving the display form keeps them, and drops a switch an earlier build of 1.7 saved...
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
    assert "under Customise" in section and "switch it back on" not in section

    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    for user in (alice, bob, admin):
        assert _prefs(db, user)["mascot_shades"] == 1
    assert _prefs(db, bob)["theme_mode"] == "light"
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_off"})
    assert all(_prefs(db, user)["mascot_shades"] == 0 for user in (alice, bob, admin))

    # Showing and hiding it for everyone are gone: 400, nothing written and nothing audited.
    for action in ("show", "hide", "explode"):
        assert admin_client.post("/admin/appearance/mascot", data={"action": action}).status_code == 400
    assert all("mascot_enabled" not in _prefs(db, user) for user in (alice, bob, admin))
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action IN (?, ?)",
                     ("mascot.everyone.show", "mascot.everyone.hide")) == 0
    assert "data-mascot" in _home(admin_client)


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


@pytest.mark.parametrize("action", ["shades_on", "shades_off"])
def test_bulk_messages_have_a_singular_in_both_languages(action):
    key = f"mascot.admin.done.{action}_one"
    assert key in _catalogue("en") and key in _catalogue("it")


def test_bulk_message_for_one_account_is_singular(app, admin_client, make_user, login, db):
    alice = make_user("mascot_single")
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", ('{"mascot_shades": 0}', alice["id"]))
    page = admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"},
                             follow_redirects=True).get_data(as_text=True)
    assert "Sunglasses put on for 1 account." in page


def test_bulk_actions_are_audited_in_the_readers_language(app, admin_client, make_user, db):
    make_user("mascot_audited")
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    row = db.one("SELECT action, details FROM audit_log WHERE action LIKE 'mascot.%'")
    accounts = db.scalar("SELECT COUNT(*) FROM users")
    assert row["action"] == "mascot.everyone.shades_on" and json.loads(row["details"]) == {"count": accounts}
    page = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "Sunglasses put on the mascot for every account" in page
    assert '<strong title="count">Count</strong>' in page
    # Show and hide are gone as actions, but entries of earlier builds of 1.7 still need their names.
    for lang in ("en", "it", "de"):
        strings = _catalogue(lang)
        assert all(f"audit.action.mascot.everyone.{name}" in strings
                   for name in ("show", "hide", "shades_on", "shades_off")), lang


@pytest.mark.parametrize(("lang", "hidden", "shown"), [
    ("de", "Maskottchen für alle Konten ausgeblendet", "Maskottchen für alle Konten angezeigt"),
    ("en", "Mascot hidden for every account", "Mascot shown to every account"),
    ("it", "Mascotte nascosta a tutti gli account", "Mascotte mostrata a tutti gli account"),
])
def test_audit_entries_of_the_removed_actions_still_read_well(app, admin_client, admin, lang, hidden, shown):
    for action in ("mascot.everyone.hide", "mascot.everyone.show"):
        in_app(app, lambda action=action: record(action, actor_id=admin["id"], ip=None, details={"count": 2}))
    page = admin_client.get(f"/admin/audit?lang={lang}").get_data(as_text=True)
    assert f"<td>{hidden}</td>" in page and f"<td>{shown}</td>" in page
    assert "<td>mascot.everyone." not in page and ">mascot.everyone." not in page


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
    assert client.post("/settings/mascot", data={"action": "shades_off"}).status_code == 400
    assert "mascot_shades" not in _prefs(db, user)


def test_taking_the_sunglasses_off_needs_an_account(client):
    response = client.post("/settings/mascot", data={"action": "shades_off"})
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
