"""Key-sequence easter eggs (BananaSuite banana-eggs/v1): the page config, finds, the easter egg page, the
administrators' editor and removing an account's finds."""

from __future__ import annotations

import html as htmllib
import json
import re

import pytest

from bananawiki.core import eggs
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.mascot import service

from .pages_support import set_feature

CONFIG = re.compile(r'<script type="application/json" id="banana-eggs">(.*?)</script>', re.S)


def _config(client, path="/"):
    page = client.get(path, follow_redirects=True).get_data(as_text=True)
    match = CONFIG.search(page)
    return (json.loads(match.group(1)), page) if match else (None, page)


def _egg(egg_id, **values):
    return {**eggs.default_egg(), "id": egg_id, "keys": ["x", "y", egg_id[0]], **values}


def _store(db, *items):
    db.execute("UPDATE site_settings SET easter_egg_configs = ?", (eggs.dumps({"version": 1, "eggs": list(items)}),))


def _found(client, egg_id="konami"):
    return client.post(f"/easter-eggs/{egg_id}/found", json={})


def _finds(db, user):
    return db.column("SELECT egg_id FROM easter_egg_finds WHERE user_id = ? ORDER BY egg_id", (user["id"],))


@pytest.fixture
def member(make_user):
    return make_user("finder")


@pytest.fixture
def member_client(app, member, login):
    client = app.test_client()
    login(client, member)
    return client


# The page config


def test_every_page_carries_the_default_egg_in_its_head(member_client):
    config, page = _config(member_client)
    assert page.index('id="banana-eggs"') < page.index("</head>") < page.index("<main")
    assert [egg["id"] for egg in config["eggs"]] == ["konami"]
    assert config["eggs"][0]["keys"] == eggs.KONAMI and config["eggs"][0]["sprite"] == {"type": "mascot"}
    assert config["signedIn"] is True and config["foundUrl"] == "/easter-eggs/{id}/found"
    assert config["pageUrl"] == "/easter-egg" and config["csrf"]["header"] == "X-CSRF-Token" and config["csrf"]["token"]
    assert config["strings"]["found"] == "You found an easter egg!"
    assert "badge" not in config["eggs"][0]
    assert re.search(r'<script src="/static/eggs/banana-eggs\.js\?v=[0-9a-f]+" defer></script>', page)
    assert "/static/eggs/banana-eggs.css" in page


def test_visitors_get_the_eggs_too_without_an_address_to_record_them(client):
    config, _page = _config(client, "/login")
    assert config["signedIn"] is False and config["foundUrl"] is None and config["csrf"] is None


def test_only_enabled_eggs_are_sent_and_badges_never(member_client, db):
    _store(db, _egg("one", badge="3"), _egg("two", enabled=False))
    config, _page = _config(member_client)
    assert [egg["id"] for egg in config["eggs"]] == ["one"] and "badge" not in config["eggs"][0]
    _store(db, _egg("two", enabled=False))
    assert _config(member_client)[0] is None


def test_page_content_cannot_reach_the_config(member_client, db):
    _store(db, _egg("one", message="</script><script>alert(1)</script>"))
    config, page = _config(member_client)
    assert config["eggs"][0]["message"] == "</script><script>alert(1)</script>"
    assert "<script>alert(1)" not in page


def test_the_account_asking_for_less_motion_gets_it_in_the_config(app, make_user, login):
    calm = make_user("calm", accessibility='{"reduce_motion": 1}')
    client = app.test_client()
    login(client, calm)
    assert _config(client)[0]["reduceMotion"] is True


def test_switching_the_mascot_off_takes_the_eggs_away(app, member_client):
    set_feature(app, "mascot", False)
    assert _config(member_client)[0] is None
    assert _found(member_client).status_code == 404


# Finds


def test_a_find_is_stored_once_and_audited_once(member_client, member, db):
    assert _found(member_client).get_json() == {"ok": True, "new": True}
    assert _found(member_client).get_json() == {"ok": True, "new": False}
    assert _finds(db, member) == ["konami"]
    rows = db.all("SELECT target_id, details FROM audit_log WHERE action = 'mascot.egg_found'")
    assert [(row["target_id"], json.loads(row["details"])) for row in rows] == [(member["id"], {"egg": "konami"})]


@pytest.mark.parametrize("egg_id", ["nope", "off", "quiet", "KONAMI", "x" * 40])
def test_eggs_that_cannot_be_found_answer_404(member_client, member, db, egg_id):
    _store(db, _egg("konami"), _egg("off", enabled=False), _egg("quiet", record=False))
    assert _found(member_client, egg_id).status_code == 404
    assert _finds(db, member) == []


def test_with_the_key_sequences_off_nothing_is_found(member_client, member, admin_client, db):
    admin_client.post("/admin/appearance/easter-eggs", data={"egg_sunglasses": "1", "egg_peek": "1"})
    assert _found(member_client).status_code == 404
    assert _finds(db, member) == []


def test_finding_needs_an_account(client, db):
    response = _found(client)
    assert response.status_code in (302, 401, 403)
    assert db.scalar("SELECT COUNT(*) FROM easter_egg_finds") == 0


def test_finding_needs_the_csrf_header(app, member, login, db):
    app.config["CSRF_DISABLED"] = False
    client = app.test_client()
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    assert client.post("/login", data={"username": member["username"], "password": "correct horse battery",
                                       "csrf_token": token}).status_code == 302
    assert _found(client).status_code == 400
    config, _page = _config(client)
    response = client.post("/easter-eggs/konami/found", json={},
                           headers={config["csrf"]["header"]: config["csrf"]["token"]})
    assert response.get_json() == {"ok": True, "new": True}
    assert _finds(db, member) == ["konami"]


def test_finds_are_rate_limited_per_account(member_client, frozen_limiter_clock):
    answers = [_found(member_client, "nope").status_code for _ in range(10)]
    assert answers == [404] * 10
    assert _found(member_client).status_code == 429


def test_impersonation_finds_nothing(app, make_user, login, db):
    superuser = make_user("super", role="admin", is_superuser=1)
    other = make_user("viewed")
    client = app.test_client()
    login(client, superuser)
    assert client.post(f"/admin/users/{other['id']}/impersonate").status_code == 302
    assert _found(client).status_code == 403
    assert _config(client)[0]["foundUrl"] is None
    assert db.scalar("SELECT COUNT(*) FROM easter_egg_finds") == 0


def _badge(db, name="Egg hunter", **values):
    db.execute("INSERT INTO badge_types (name, description, icon, color, enabled, auto_trigger, trigger_type, "
               "trigger_threshold, allow_multiple, created_at) VALUES (?, '', '🥚', '#ffd700', 1, ?, ?, ?, 0, "
               "datetime('now'))", (name, values.get("auto", 0), values.get("trigger", ""), values.get("threshold", 0)))
    return db.scalar("SELECT id FROM badge_types WHERE name = ?", (name,))


def test_a_first_find_awards_the_eggs_badge(member_client, member, db):
    badge = _badge(db)
    _store(db, _egg("one", badge=str(badge)))
    assert _found(member_client, "one").get_json()["new"] is True
    assert db.column("SELECT badge_type_id FROM user_badges WHERE user_id = ?", (member["id"],)) == [badge]
    _found(member_client, "one")
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (member["id"],)) == 1


def test_the_easter_egg_trigger_counts_eggs_found(member_client, member, db):
    badge = _badge(db, "Two eggs", auto=1, trigger="easter_egg", threshold=2)
    _store(db, _egg("one"), _egg("two"))
    _found(member_client, "one")
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (member["id"],)) == 0
    _found(member_client, "two")
    assert db.column("SELECT badge_type_id FROM user_badges WHERE user_id = ?", (member["id"],)) == [badge]


def test_the_trigger_is_offered_to_badges(admin_client):
    page = admin_client.get("/admin/badges").get_data(as_text=True)
    assert 'value="easter_egg"' in page and "Easter eggs found" in page


def test_without_the_badges_feature_finds_award_nothing(app, member_client, member, db):
    badge = _badge(db)
    _store(db, _egg("one", badge=str(badge)))
    set_feature(app, "badges", False)
    assert _found(member_client, "one").get_json()["new"] is True
    assert db.scalar("SELECT COUNT(*) FROM user_badges") == 0


def test_finds_are_in_the_accounts_data_export(app):
    from bananawiki.wiki.features.user_data_export import service as export

    assert any(rows.table == "easter_egg_finds" for rows in export.EXPORTED)


# The easter egg page


def test_the_page_lists_each_egg_found_with_its_date_and_the_others_as_a_question_mark(member_client, db):
    _store(db, _egg("one"), _egg("two"), _egg("off", enabled=False), _egg("quiet", record=False))
    _found(member_client, "two")
    page = member_client.get("/easter-egg").get_data(as_text=True)
    assert "1 of 2 found" in page
    assert 'id="egg-one"' in page and 'id="egg-two"' in page and 'id="egg-off"' not in page
    assert 'id="egg-quiet"' not in page
    two = page[page.index('id="egg-two"'):page.index("</li>", page.index('id="egg-two"'))]
    assert "Found: two" in two
    one = page[page.index('id="egg-one"'):page.index("</li>", page.index('id="egg-one"'))]
    assert ">?<" in one and "Not found yet" in one


def test_the_page_follows_the_switches(app, member_client, admin_client, db):
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    visitor = app.test_client().get("/easter-egg")
    assert visitor.status_code == 200 and "Finds are kept for signed-in people." in visitor.get_data(as_text=True)
    admin_client.post("/admin/appearance/easter-eggs", data={"egg_sunglasses": "1", "egg_peek": "1"})
    assert member_client.get("/easter-egg").status_code == 404


# The editor


def _editor_options(page):
    match = re.search(r"data-banana-eggs-editor-options>(.*?)</script>", page, re.S)
    return json.loads(match.group(1))


def _list_value(page):
    return json.loads(htmllib.unescape(re.search(r'id="mascot-egg-list" name="easter_egg_configs" value="([^"]*)"',
                                                 page).group(1)))


def test_the_editor_starts_from_the_stored_list_with_the_wikis_badges(admin_client, db):
    badge = _badge(db)
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    assert _list_value(page) == eggs.default_config()
    options = _editor_options(page)
    assert options["badges"] == [{"value": str(badge), "label": "Egg hunter"}]
    assert options["strings"]["title"] == "Easter egg {n}" and options["strings"]["stop"] == "Stop ({s})"
    assert options["emojis"] == eggs.EMOJIS and options["mascot"] is True
    assert "/static/eggs/banana-eggs-admin.js" in page


def test_saving_the_list_cleans_it_and_audits_it(app, admin_client, db):
    party = _egg("party", keys=["P", "a"], message="  Party   time ")
    response = admin_client.post("/admin/appearance/easter-eggs/list",
                                 data={"easter_egg_configs": json.dumps({"version": 1, "eggs": [party]})})
    assert response.status_code == 302
    stored = json.loads(db.scalar("SELECT easter_egg_configs FROM site_settings"))
    assert stored["eggs"][0]["keys"] == ["p", "a"] and stored["eggs"][0]["message"] == "Party time"
    rows = db.all("SELECT details FROM audit_log WHERE action = 'mascot.easter_egg_list_changed'")
    assert [json.loads(row["details"]) for row in rows] == [{"eggs": ["party"]}]
    # The same list again changes nothing and records nothing.
    admin_client.post("/admin/appearance/easter-eggs/list", data={"easter_egg_configs": json.dumps(stored)})
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'mascot.easter_egg_list_changed'") == 1


def test_a_wrong_list_is_refused_with_each_problem(admin_client, db):
    wrong = {"version": 1, "eggs": [_egg("ok"), {**_egg("bad"), "keys": ["x"], "count": 999}]}
    response = admin_client.post("/admin/appearance/easter-eggs/list", data={"easter_egg_configs": json.dumps(wrong)})
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert "Easter egg 2 · Key sequence: needs 2 to 32 keys" in page
    assert "Easter egg 2 · How many: is out of range" in page
    assert _list_value(page) == wrong, "the editor shows what was sent, to correct it"
    assert db.scalar("SELECT easter_egg_configs FROM site_settings") is None


def test_only_administrators_save_the_list(app, make_user, login, db):
    client = app.test_client()
    login(client, make_user("editor", role="editor"))
    response = client.post("/admin/appearance/easter-eggs/list", data={"easter_egg_configs": "[]"})
    assert response.status_code in (302, 403)
    assert db.scalar("SELECT easter_egg_configs FROM site_settings") is None


def test_the_list_cannot_be_saved_while_impersonating(app, make_user, login, db):
    superuser = make_user("super", role="admin", is_superuser=1)
    other_admin = make_user("other_admin", role="admin")
    client = app.test_client()
    login(client, superuser)
    client.post(f"/admin/users/{other_admin['id']}/impersonate")
    assert client.post("/admin/appearance/easter-eggs/list", data={"easter_egg_configs": "[]"}).status_code == 403
    assert db.scalar("SELECT easter_egg_configs FROM site_settings") is None


def test_saving_the_list_needs_the_csrf_token(app, admin_client, db):
    app.config["CSRF_DISABLED"] = False
    assert admin_client.post("/admin/appearance/easter-eggs/list",
                             data={"easter_egg_configs": "[]"}).status_code == 400
    assert db.scalar("SELECT easter_egg_configs FROM site_settings") is None


def test_an_empty_list_means_no_eggs_not_the_default(admin_client, member_client, db):
    admin_client.post("/admin/appearance/easter-eggs/list", data={"easter_egg_configs": '{"version": 1, "eggs": []}'})
    assert _config(member_client)[0] is None


# Removing an account's finds


def test_administrators_remove_an_accounts_finds(admin_client, member_client, member, db):
    _found(member_client)
    page = admin_client.get(f"/admin/users/{member['id']}").get_data(as_text=True)
    assert 'id="mascot-finds"' in page and "Found 1 easter egg." in page
    response = admin_client.post(f"/admin/users/{member['id']}/easter-eggs/revoke")
    assert response.status_code == 302
    assert _finds(db, member) == []
    rows = db.all("SELECT target_id, details FROM audit_log WHERE action = 'mascot.egg_finds_revoked'")
    assert [(row["target_id"], json.loads(row["details"])) for row in rows] == [(member["id"], {"count": 1})]
    assert 'id="mascot-finds"' not in admin_client.get(f"/admin/users/{member['id']}").get_data(as_text=True)


def test_members_cannot_remove_finds(app, member_client, member, make_user, login, db):
    _found(member_client)
    other = app.test_client()
    login(other, make_user("someone"))
    assert other.post(f"/admin/users/{member['id']}/easter-eggs/revoke").status_code in (302, 403)
    assert _finds(db, member) == ["konami"]


# 1.4's list in the switches' column


def test_the_switches_never_overwrite_a_list_1_4_left_until_it_is_imported(app, admin_client, db):
    legacy = '[{"keys": "38,38,40,40,37,39,37,39,66,65", "emoji": "x"}]'
    db.execute("UPDATE site_settings SET easter_eggs = ?, easter_egg_configs = NULL", (legacy,))
    admin_client.post("/admin/appearance/easter-eggs", data={"egg_sunglasses": "1"})
    assert db.scalar("SELECT easter_eggs FROM site_settings") == legacy
    with app.test_request_context(), connection_scope():
        assert service.eggs() == {"sunglasses": True, "konami": True, "peek": True}
    # Once imported (version 21 always stores what it imported), the switches take the column back.
    db.execute("UPDATE site_settings SET easter_egg_configs = '{\"version\":1,\"eggs\":[]}'")
    admin_client.post("/admin/appearance/easter-eggs", data={"egg_sunglasses": "1"})
    assert json.loads(db.scalar("SELECT easter_eggs FROM site_settings")) == {"sunglasses": 1, "konami": 0, "peek": 0}
