"""Mascot choices: only the two mascot keys are ever written, and reading them stays cheap."""

from __future__ import annotations

import json

import pytest

from bananawiki.wiki.features.mascot import service
from bananawiki.wiki.features.users import preferences

from .pages_support import in_app


def _raw(db, user):
    return db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],))


def _prefs(db, user):
    return json.loads(_raw(db, user) or "{}")


def _store(db, user, raw):
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (raw, user["id"]))


def _switch_off_italian(db):
    db.execute("UPDATE site_settings SET interface_languages_json = ? WHERE id = 1",
               (json.dumps({"it": {"name": "Italiano", "enabled": False}}),))


def _forbid_full_cleaning(monkeypatch):
    """Fail on validating the whole preference set: it lists the translation folders every time."""

    def refuse(*args, **kwargs):
        raise AssertionError("the mascot validated every display preference")

    monkeypatch.setattr(preferences, "clean", refuse)
    monkeypatch.setattr(preferences, "enabled_languages", refuse)


# ── Writing ───────────────────────────────────────────────────────────────────


def test_own_choice_writes_only_the_mascot_keys(app, client, make_user, login, db):
    # A language switched off for a while, and a key some later version will understand.
    _switch_off_italian(db)
    user = make_user("mascot_linguist")
    saved = {"interface_language": "it", "theme_mode": "light", "later_key": {"kept": True}}
    _store(db, user, json.dumps(saved))
    login(client, user)
    assert client.post("/settings/mascot", data={"action": "hide"}).status_code == 302
    assert _prefs(db, user) == {**saved, "mascot_enabled": 0}
    client.post("/settings/mascot", data={"action": "show"})
    assert client.post("/mascot/shades").status_code == 200
    assert _prefs(db, user) == {**saved, "mascot_enabled": 1, "mascot_shades": 1}

    # An account that never saved anything gets just the mascot key.
    blank = make_user("mascot_blank")
    _store(db, blank, None)
    client.post("/logout")
    login(client, blank)
    client.post("/settings/mascot", data={"action": "hide"})
    assert _prefs(db, blank) == {"mascot_enabled": 0}


def test_bulk_actions_write_only_the_mascot_keys(app, admin_client, make_user, db):
    _switch_off_italian(db)
    linguist, blank, empty = (make_user(f"mascot_bulk_{name}") for name in ("it", "null", "empty"))
    saved = {"interface_language": "it", "later_key": [1, 2]}
    _store(db, linguist, json.dumps(saved))
    _store(db, blank, None)
    _store(db, empty, "")

    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    assert _prefs(db, linguist) == {**saved, "mascot_shades": 1}
    assert _prefs(db, blank) == _prefs(db, empty) == {"mascot_shades": 1}

    admin_client.post("/admin/appearance/mascot", data={"action": "hide"})
    assert _prefs(db, linguist) == {**saved, "mascot_shades": 1, "mascot_enabled": 0}
    assert _prefs(db, blank) == _prefs(db, empty) == {"mascot_shades": 1, "mascot_enabled": 0}


# ── Cost ──────────────────────────────────────────────────────────────────────


def test_bulk_action_reads_the_raw_keys_and_counts_exactly(app, admin_client, make_user, db, monkeypatch):
    db.execute("UPDATE users SET accessibility = ?", (json.dumps({"mascot_enabled": 0}),))
    accounts = {name: make_user(f"mascot_mix_{name}") for name in
                ("never_saved", "hidden", "hidden_as_text", "broken", "out_of_range", "not_an_object")}
    _store(db, accounts["never_saved"], None)
    _store(db, accounts["hidden"], json.dumps({"mascot_enabled": 0, "theme_mode": "dark"}))
    _store(db, accounts["hidden_as_text"], json.dumps({"mascot_enabled": "0"}))
    _store(db, accounts["broken"], "{not json")
    _store(db, accounts["out_of_range"], json.dumps({"mascot_enabled": 7}))
    _store(db, accounts["not_an_object"], "[1, 2]")
    everyone = db.scalar("SELECT COUNT(*) FROM users")

    _forbid_full_cleaning(monkeypatch)
    # Shown by default: the account that never saved, the unreadable ones and the out-of-range value.
    assert in_app(app, lambda: service.apply_to_everyone("hide")) == 4
    assert _prefs(db, accounts["broken"]) == _prefs(db, accounts["not_an_object"]) == {"mascot_enabled": 0}
    # Already hidden: left exactly as stored.
    assert _raw(db, accounts["hidden_as_text"]) == json.dumps({"mascot_enabled": "0"})
    assert in_app(app, lambda: service.apply_to_everyone("hide")) == 0
    assert in_app(app, lambda: service.apply_to_everyone("show")) == everyone
    assert _prefs(db, accounts["hidden"]) == {"mascot_enabled": 1, "theme_mode": "dark"}
    monkeypatch.undo()

    # The audit entry still says how many accounts changed.
    admin_client.post("/admin/appearance/mascot", data={"action": "shades_on"})
    details = db.scalar("SELECT details FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT 1",
                        ("mascot.bulk_updated",))
    assert json.loads(details) == {"action": "shades_on", "accounts": everyone}


@pytest.mark.parametrize(("raw", "enabled", "shades"), [
    (None, True, False),
    ("", True, False),
    ("{not json", True, False),
    ('{"mascot_enabled": 0, "mascot_shades": 1}', False, True),
    ('{"mascot_enabled": "0", "mascot_shades": "1"}', False, True),
    ('{"mascot_enabled": 2, "mascot_shades": -1}', True, False),
    ('{"mascot_enabled": null, "mascot_shades": [1]}', True, False),
    ('{"mascot_enabled": 1e999}', True, False),
    ('{"interface_language": "xx", "mascot_shades": 1}', True, True),
])
def test_state_reads_just_the_two_keys(monkeypatch, raw, enabled, shades):
    _forbid_full_cleaning(monkeypatch)
    assert service.state({"id": "someone", "accessibility": raw}) == {"enabled": enabled, "shades": shades}
    assert service.state(None) == {"enabled": False, "shades": False}


def test_top_bar_reads_the_state_once_per_page(app, client, make_user, login, monkeypatch):
    login(client, make_user("mascot_once"))
    calls = []
    real = service.state
    monkeypatch.setattr(service, "state", lambda user: calls.append(user) or real(user))
    html = client.get("/", follow_redirects=True).get_data(as_text=True)
    assert "data-mascot" in html and len(calls) == 1
