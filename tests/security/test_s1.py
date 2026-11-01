"""Regression tests for findings SW1-1 and SW3-1 of full re-audit pass 13.

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

from bananawiki.wiki.db import connection_scope

JSON = {"Accept": "application/json"}


def _settings(app, **values) -> None:
    with app.app_context(), connection_scope() as session:
        for name, value in values.items():
            session.execute(f"UPDATE site_settings SET {name} = ? WHERE id = 1", (value,))


# SW1-1


def test_sw1_1_maintenance_keeps_anonymous_public_readers_out(app, client, admin, make_user, login):
    with app.test_request_context(), connection_scope():
        from bananawiki.wiki.features.pages import service

        service.create("Incident notes", "the content MARKERQ77", author_id=None)
    _settings(app, public_mode=1, maintenance_mode=1)
    for path in ("/", "/page/incident-notes", "/search?q=MARKERQ77", "/_probe/public-read"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/maintenance"), path
        assert b"MARKERQ77" not in response.data
    response = client.get("/page/incident-notes", headers=JSON)
    assert response.status_code == 503 and response.json["code"] == "maintenance"
    # The maintenance page itself and the sign-in page stay open; administrators still get in.
    page = client.get("/maintenance")
    assert page.status_code == 200 and b"Incident notes" not in page.data
    assert client.get("/login").status_code == 200
    staff = app.test_client()
    login(staff, admin)
    assert b"MARKERQ77" in staff.get("/page/incident-notes").data
    # Without maintenance, public mode serves visitors as before.
    _settings(app, maintenance_mode=0)
    assert b"MARKERQ77" in client.get("/page/incident-notes").data


def test_sw1_1_public_views_show_no_wiki_content_during_maintenance(app, client, db, admin, make_user, login):
    """Published custom pages stay open during maintenance, but their shell lists no pages."""
    from bananawiki.wiki.features.page_builder import document
    from bananawiki.wiki.features.pages import categories, service

    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('custom_pages', 'api_service')")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1 WHERE id = 1")
    with app.test_request_context(), connection_scope():
        category = categories.create("Board Zq9")
        service.create("Secret plan", "XYZZY body", category_id=category["id"], author_id=None)
        doc = document.validate({"version": 2, "blocks": [
            {"type": "heading", "text": "Welcome"},
            {"type": "pages", "source": "recent", "limit": 12, "excerpt": True}]})
        raw, text = document.dump(doc), document.to_markdown(doc)
    db.insert("custom_pages", {"path": "/imprint", "title": "Imprint", "content_type": "wiki_page",
                               "is_published": 1, "content": "Company Zq9"})
    db.insert("custom_pages", {"path": "/landing", "title": "Landing", "content_type": "wiki_page",
                               "is_published": 1, "content": text, "builder_json": raw})
    _settings(app, public_mode=1, maintenance_mode=1)
    leaks = (b"secret-plan", b"Secret plan", b"Board Zq9", b"XYZZY")
    member = app.test_client()
    login(member, make_user("member", role="editor"))
    for visitor in (client, member):
        for path in ("/imprint", "/landing", "/api-docs", "/no-such-address"):
            response = visitor.get(path)
            assert response.status_code == (404 if path == "/no-such-address" else 200), path
            assert not [leak for leak in leaks if leak in response.data], path
        assert b"Company Zq9" in visitor.get("/imprint").data
    # Administrators keep the whole shell, and so does everyone once maintenance ends.
    staff = app.test_client()
    login(staff, admin)
    assert b"secret-plan" in staff.get("/imprint").data
    _settings(app, maintenance_mode=0)
    assert b"secret-plan" in client.get("/imprint").data
    assert b"XYZZY" in member.get("/landing").data


# SW3-1


def _group_id(app, name: str) -> int:
    with app.app_context(), connection_scope() as session:
        return session.scalar("SELECT id FROM group_chats WHERE name = ?", (name,))


def test_sw3_1_custom_invite_code_checks_spend_the_join_budget(app, make_user, login):
    owner = app.test_client()
    login(owner, make_user("alice"))
    assert owner.post("/groups/new", data={"name": "Family"}).status_code == 302
    family = _group_id(app, "Family")
    assert owner.post(f"/groups/{family}/regenerate_code", data={"custom_code": "smith2024"},
                      headers=JSON).status_code == 200

    attacker = app.test_client()
    login(attacker, make_user("mallory"))
    assert attacker.post("/groups/new", data={"name": "Mine"}).status_code == 302
    mine = _group_id(app, "Mine")
    statuses = [attacker.post(f"/groups/{mine}/regenerate_code", data={"custom_code": f"wordx{i:03d}"},
                              headers=JSON).status_code for i in range(10)]
    assert statuses == [200] * 10
    # The eleventh check, the one that would find the code, is refused like an eleventh join guess.
    response = attacker.post(f"/groups/{mine}/regenerate_code", data={"custom_code": "smith2024"}, headers=JSON)
    assert response.status_code == 429
    assert attacker.post("/groups/join", data={"invite_code": "smith2024"}, headers=JSON).status_code == 429
    # A taken code spends a guess too, and a random new code costs none.
    other = app.test_client()
    login(other, make_user("mallory2"))
    assert other.post("/groups/new", data={"name": "Other"}).status_code == 302
    theirs = _group_id(app, "Other")
    assert other.post(f"/groups/{theirs}/regenerate_code", data={"custom_code": "smith2024"},
                      headers=JSON).status_code == 409
    for _ in range(3):
        assert other.post(f"/groups/{theirs}/regenerate_code", headers=JSON).status_code == 200
    with app.app_context(), connection_scope() as session:
        spent = session.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'group_join'")
    assert spent == 12  # alice 1, mallory 10, mallory2 1
