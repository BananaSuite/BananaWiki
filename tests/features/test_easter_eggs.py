"""Easter eggs: each one can be switched off for the whole wiki under Admin -> Appearance."""

from __future__ import annotations

import json
import re

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.mascot import service

from .pages_support import set_feature

ALL_ON = {"sunglasses": True, "konami": True, "peek": True}


def _button(html):
    match = re.search(r"<button[^>]*\bdata-mascot\b[^>]*>", html)
    assert match, "no mascot button"
    return match.group(0)


def _home(client):
    return client.get("/", follow_redirects=True).get_data(as_text=True)


def _peeks(client):
    return 'class="mascot-peek"' in client.get("/no-such-address").get_data(as_text=True)


def _save(client, *on):
    return client.post("/admin/appearance/easter-eggs", data={f"egg_{egg}": "1" for egg in on})


def _eggs(app):
    with app.test_request_context(), connection_scope():
        return service.eggs()


def test_every_easter_egg_is_on_by_default(app, admin_client):
    assert _eggs(app) == ALL_ON
    button = _button(_home(admin_client))
    assert 'data-mascot-clicks="11"' in button and 'data-mascot-url="/mascot/shades"' in button
    assert "data-mascot-konami" not in button
    assert _peeks(admin_client)
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    assert 'id="mascot-eggs-heading"' in page
    for egg in ALL_ON:
        assert f'name="egg_{egg}" value="1" checked' in page
    assert "Clicking the mascot 11 times in a row" in page


def test_switching_the_konami_code_off_marks_the_button(app, admin_client):
    assert _save(admin_client, "sunglasses", "peek").status_code == 302
    assert _eggs(app) == {**ALL_ON, "konami": False}
    button = _button(_home(admin_client))
    assert 'data-mascot-konami="off"' in button and 'data-mascot-clicks="11"' in button
    assert 'name="egg_konami" value="1">' in admin_client.get("/admin/appearance").get_data(as_text=True)


def test_switching_the_peek_off_keeps_the_plain_not_found_card(app, admin_client):
    _save(admin_client, "sunglasses", "konami")
    assert not _peeks(admin_client)
    _save(admin_client, *ALL_ON)
    assert _peeks(admin_client)


def test_without_the_sunglasses_egg_nobody_wears_them_until_it_is_back(app, admin_client, make_user, login, db):
    member = make_user("shaded", accessibility='{"mascot_shades": 1}')
    client = app.test_client()  # admin_client is the default client, signed in as the administrator
    login(client, member)
    assert "mascot-sprite--shades" in _home(client)
    _save(admin_client, "konami", "peek")
    html = _home(client)
    button = _button(html)
    assert 'data-mascot-clicks="0"' in button and "data-mascot-url" not in button
    assert "mascot-sprite--shades" not in html
    # Nothing to take off under Customise, and the clicks' endpoint is gone.
    assert 'id="mascot-heading"' not in client.get("/settings/display").get_data(as_text=True)
    assert client.post("/mascot/shades").status_code == 404
    assert client.delete("/mascot/shades").status_code == 404
    # The administrators' buttons for every account give way to a note, and refuse a crafted request.
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    assert 'name="action" value="shades_on"' not in page and "The sunglasses easter egg is off" in page
    assert admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"}).status_code == 404
    # The account's own choice was kept: back on, the sunglasses are back.
    assert json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (member["id"],)))["mascot_shades"] == 1
    _save(admin_client, *ALL_ON)
    assert "mascot-sprite--shades" in _home(client)
    assert client.post("/mascot/shades").status_code == 200


def test_only_administrators_switch_easter_eggs(app, client, make_user, login):
    login(client, make_user("curious", role="editor"))
    assert client.post("/admin/appearance/easter-eggs", data={}).status_code in (302, 403)
    assert _eggs(app) == ALL_ON


def test_easter_eggs_cannot_be_switched_while_impersonating(app, make_user, login):
    superuser = make_user("super", role="admin", is_superuser=1)
    other_admin = make_user("other_admin", role="admin")
    client = app.test_client()
    login(client, superuser)
    assert client.post(f"/admin/users/{other_admin['id']}/impersonate").status_code == 302
    assert client.post("/admin/appearance/easter-eggs", data={}).status_code == 403
    assert client.post("/admin/appearance/mascot", data={"action": "shades_on"}).status_code == 403
    assert _eggs(app) == ALL_ON


def test_changes_are_audited_and_saving_the_same_choice_records_nothing(app, admin_client, db):
    _save(admin_client, "sunglasses")
    entries = db.all("SELECT details FROM audit_log WHERE action = 'mascot.easter_eggs_changed'")
    assert [json.loads(row["details"]) for row in entries] == [{"switched_on": [], "switched_off": ["konami", "peek"]}]
    _save(admin_client, "sunglasses")
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'mascot.easter_eggs_changed'") == 1
    log = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "Easter eggs changed" in log


def test_the_switches_need_the_csrf_token(app, admin_client):
    app.config["CSRF_DISABLED"] = False
    assert admin_client.post("/admin/appearance/easter-eggs", data={}).status_code == 400
    assert _eggs(app) == ALL_ON


@pytest.mark.parametrize("stored, expected", [
    ("not json", ALL_ON),
    ("[0, 0]", ALL_ON),
    ('{"konami": 0}', {**ALL_ON, "konami": False}),
    ('{"konami": false, "peek": "0", "sunglasses": null}', {**ALL_ON, "konami": False}),
    ('{"later_egg": 0}', ALL_ON),
])
def test_unreadable_settings_leave_the_eggs_on(app, db, stored, expected):
    db.execute("UPDATE site_settings SET easter_eggs = ?", (stored,))
    assert _eggs(app) == expected


def test_eggs_a_later_version_added_survive_a_save(app, admin_client, db):
    db.execute("""UPDATE site_settings SET easter_eggs = '{"later_egg": 0}'""")
    _save(admin_client, "sunglasses", "peek")
    assert json.loads(db.scalar("SELECT easter_eggs FROM site_settings")) == {
        "later_egg": 0, "sunglasses": 1, "konami": 0, "peek": 1}


def test_the_easter_egg_settings_go_with_the_mascot_plugin(app, admin_client):
    set_feature(app, "mascot", False)
    assert "mascot-eggs-heading" not in admin_client.get("/admin/appearance").get_data(as_text=True)
    assert admin_client.post("/admin/appearance/easter-eggs", data={}).status_code == 404
    assert _eggs(app) == ALL_ON
