"""First-run wizard, new-user introduction, guided tour and built-in docs."""

from __future__ import annotations

import pytest
from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import docs
from bananawiki.wiki.registry import registry

from .pages_support import restrict


@pytest.fixture
def owner(make_user):
    return make_user("owner_user", role="owner", onboarding_required=1)


def _wizard(client, **data):
    form = {"site_name": "Team Wiki", "language": "en", "default_theme_mode": "light", "mode": "easy"}
    form.update(data)
    return client.post("/onboarding", data=form)


def test_onboarding_is_forced_until_done(client, owner, login, db):
    login(client, owner)
    assert client.get("/_probe/private").headers["Location"].endswith("/onboarding")
    assert client.post("/_probe/private").status_code == 403
    assert client.get("/onboarding").status_code == 200
    response = _wizard(client)
    assert response.headers["Location"].endswith("/intro")
    row = db.one("SELECT onboarding_required, intro_required FROM users WHERE id = ?", (owner["id"],))
    assert row == {"onboarding_required": 0, "intro_required": 1}
    settings = db.one("SELECT site_name, default_theme_mode FROM site_settings")
    assert settings == {"site_name": "Team Wiki", "default_theme_mode": "light"}
    assert client.get("/_probe/private").data == b"private"


def test_onboarding_validates_site_name(client, owner, login, db):
    login(client, owner)
    assert _wizard(client, site_name="x" * 101).status_code == 400
    assert db.scalar("SELECT onboarding_required FROM users WHERE id = ?", (owner["id"],)) == 1


def test_onboarding_advanced_mode_switches_features(app, client, owner, login):
    login(client, owner)
    with app.app_context():
        switchable = [f.id for f in registry().ordered() if f.toggle != "always"]
    if not switchable:
        pytest.skip("no switchable features installed")
    keep = switchable[0]
    assert _wizard(client, mode="advanced", features=[keep]).status_code == 302
    with app.test_request_context(), connection_scope():
        from bananawiki.wiki.registry import is_enabled

        assert is_enabled(keep)
        assert not any(is_enabled(feature_id) for feature_id in switchable[1:])


def test_onboarding_creates_first_users_atomically(client, owner, login, db):
    login(client, owner)
    response = _wizard(client, new_username=["alice", "bad name"], new_password=[PASSWORD, PASSWORD],
                       new_role=["editor", "user"], new_user_intro_enabled="1")
    assert response.status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0
    response = _wizard(client, new_username=["alice", "bobby"], new_password=[PASSWORD, PASSWORD],
                       new_role=["editor", "superadmin"])
    assert response.status_code == 400
    response = _wizard(client, new_username=["alice", ""], new_password=[PASSWORD, ""],
                       new_role=["editor", "user"], new_force_password_change=["0"], new_user_intro_enabled="1")
    assert response.status_code == 302
    alice = db.one("SELECT role, force_password_change, intro_required FROM users WHERE username = 'alice'")
    assert alice == {"role": "editor", "force_password_change": 1, "intro_required": 1}


def test_onboarding_is_admin_only_and_replay_can_be_disabled(client, make_user, login, db):
    user = make_user("bob", onboarding_required=1)
    login(client, user)
    # A non-administrator flagged by a 1.4 database is released, not stuck.
    assert client.get("/onboarding").status_code == 302
    assert db.scalar("SELECT onboarding_required FROM users WHERE id = ?", (user["id"],)) == 0
    assert client.post("/onboarding", data={"site_name": "Hacked"}).status_code == 302
    assert db.scalar("SELECT site_name FROM site_settings") != "Hacked"
    client.post("/logout")
    admin = make_user("chief", role="admin")
    login(client, admin)
    assert client.get("/onboarding").status_code == 200
    db.execute("UPDATE site_settings SET onboarding_replay_disabled = 1")
    assert client.get("/onboarding").status_code == 302


def test_onboarding_spawns_builtin_docs(client, owner, login, db):
    login(client, owner)
    response = _wizard(client, spawn_docs="1", docs_variant="simplified", docs_language="it")
    assert response.status_code == 302
    category_id = db.scalar("SELECT docs_category_id FROM site_settings")
    assert db.scalar("SELECT name FROM categories WHERE id = ?", (category_id,)) == "BananaWiki"
    count = db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category_id,))
    assert count == len(docs.pages("simplified", "it"))
    assert db.scalar("SELECT COUNT(*) FROM page_history ph JOIN pages p ON p.id = ph.page_id "
                     "WHERE p.category_id = ?", (category_id,)) == count


def test_respawning_docs_replaces_the_previous_set(app, db, admin):
    with app.test_request_context(), connection_scope():
        first = docs.spawn(variant="simplified")
        second = docs.spawn(variant="full", language="en")
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (first["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (second["id"],)) == len(docs.pages())
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE slug = 'bananawiki-welcome'") == 1


def test_builtin_docs_have_titles_in_both_languages():
    for variant in docs.VARIANTS:
        english, italian = docs.pages(variant, "en"), docs.pages(variant, "it")
        assert english and len(english) == len(italian)
        assert [slug for _t, slug, _c in english] == [slug for _t, slug, _c in italian]
        assert all(title and content.startswith("# ") for title, _s, content in english + italian)


# ── Introduction and tour ─────────────────────────────────────────────────────


def test_new_accounts_get_the_intro_when_enabled(client, db, make_user, login):
    db.execute("UPDATE site_settings SET new_user_intro_enabled = 1, open_signup = 1, bot_protection_enabled = 0")
    client.post("/signup", data={"username": "newbie", "password": PASSWORD, "confirm_password": PASSWORD})
    assert db.scalar("SELECT intro_required FROM users WHERE username = 'newbie'") == 1


def test_intro_skip_clears_flag(client, db, make_user, login):
    user = make_user("bob", intro_required=1)
    login(client, user)
    assert client.get("/intro").status_code == 200
    response = client.post("/intro", data={"next": "/_probe/private"})
    assert response.headers["Location"].endswith("/_probe/private")
    assert db.scalar("SELECT intro_required FROM users WHERE id = ?", (user["id"],)) == 0


def test_intro_replay_setting(client, db, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/intro").status_code == 200
    db.execute("UPDATE site_settings SET onboarding_replay_disabled = 1")
    assert client.get("/intro").status_code == 302


def test_tour_walkthrough_and_finish(client, db, make_user, login):
    user = make_user("bob", intro_required=1)
    login(client, user)
    response = client.post("/tour/start", data={"tour_role": "user"})
    assert "/tour/step/0" in response.headers["Location"]
    page = client.get(response.headers["Location"])
    assert page.status_code == 200 and b"tour-mock" in page.data
    assert client.get("/tour/step/99?role=user").status_code == 404
    response = client.post("/tour/finish")
    assert response.status_code == 302
    assert db.scalar("SELECT intro_required FROM users WHERE id = ?", (user["id"],)) == 0


def test_admin_perspective_is_illustration_only(client, db, make_user, login):
    """Audit H1: previewing the admin tour must not grant any access."""
    db.execute("INSERT INTO categories (name) VALUES ('SecretHR')")
    category_id = db.scalar("SELECT id FROM categories WHERE name = 'SecretHR'")
    db.execute("INSERT INTO pages (title, slug, content, category_id) VALUES ('Salaries 2026', 'salaries', 'x', ?)",
               (category_id,))
    user = make_user("bob", intro_required=1)
    restrict(db, user, read=[])  # role defaults, no readable category
    login(client, user)
    response = client.post("/tour/start", data={"tour_role": "admin"})
    assert "role=admin" in response.headers["Location"]
    for step in range(12):
        page = client.get(f"/tour/step/{step}?role=admin")
        if page.status_code == 404:
            break
        assert b"Salaries 2026" not in page.data and b"SecretHR" not in page.data
    assert step > 5  # the admin perspective has more steps than the user one
    assert client.get("/_probe/admin").status_code == 403


def test_role_switching_can_be_disabled(client, db, make_user, login):
    db.execute("UPDATE site_settings SET intro_role_switching_enabled = 0")
    login(client, make_user("bob", intro_required=1))
    response = client.post("/tour/start", data={"tour_role": "admin"})
    assert "role=user" in response.headers["Location"]
    response = client.post("/tour/role/admin")
    assert "role=user" in response.headers["Location"]


def test_legacy_tour_urls_redirect(client, make_user, login):
    login(client, make_user("bob", intro_required=1))
    assert "/tour/step/1" in client.get("/tour/presenter/user/1").headers["Location"]
    assert "/tour/step/2" in client.post("/tour/step/2", data={"role": "user"}).headers["Location"]
