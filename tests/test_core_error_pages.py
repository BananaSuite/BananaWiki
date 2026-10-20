"""Error pages and JSON error bodies are worded in the visitor's language."""

from __future__ import annotations

import json

import pytest
from flask import abort

from bananawiki.wiki import auth

JSON = {"Accept": "application/json"}


@pytest.fixture
def failing_app(app):
    def bad_request():
        abort(400)

    def bad_date():
        abort(400, description="Quella data non esiste.")

    def broken():
        raise RuntimeError("boom")

    app.add_url_rule("/_fail/400", "fail_400", auth.public(bad_request))
    app.add_url_rule("/_fail/date", "fail_date", auth.public(bad_date))
    app.add_url_rule("/_fail/500", "fail_500", auth.public(broken))
    return app


def test_csrf_rejection_uses_the_accounts_own_language(app, client, make_user, login, db):
    user = make_user("giulio")
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
               (json.dumps({"interface_language": "it"}), user["id"]))
    login(client, user)
    app.config["CSRF_DISABLED"] = False
    page = client.post("/_probe/private")
    text = page.get_data(as_text=True)
    assert page.status_code == 400 and 'lang="it"' in text
    assert "Il modulo è scaduto" in text and "The form expired" not in text
    refused = client.post("/_probe/private", headers=JSON)
    assert refused.status_code == 400 and refused.json["error"].startswith("Il modulo è scaduto")


def test_a_rejected_form_shows_the_navigation_only_to_accounts_the_wiki_lets_in(app, client, make_user, login, db):
    user = make_user("giulio", accessibility=json.dumps({"interface_language": "it"}))
    login(client, user)
    app.config["CSRF_DISABLED"] = False
    page = client.post("/_probe/private").get_data(as_text=True)
    assert 'lang="it"' in page and '<header class="topbar">' in page
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    page = client.post("/_probe/private").get_data(as_text=True)
    assert 'lang="it"' in page and '<header class="topbar">' not in page
    db.execute("UPDATE site_settings SET maintenance_mode = 0")
    db.execute("UPDATE users SET approval_status = 'pending' WHERE id = ?", (user["id"],))
    page = client.post("/_probe/private").get_data(as_text=True)
    assert 'lang="it"' in page and '<header class="topbar">' not in page


def test_unknown_addresses_show_the_navigation_only_to_accounts_the_wiki_lets_in(app, client, make_user, login,
                                                                                 db):
    user = make_user("giulio", accessibility=json.dumps({"interface_language": "it"}))
    login(client, user)
    page = client.get("/no-such-address")
    assert page.status_code == 404 and '<header class="topbar">' in page.get_data(as_text=True)
    for change, undo in (("approval_status = 'pending'", "approval_status = 'approved'"),
                         ("suspended = 1", "suspended = 0")):
        db.execute(f"UPDATE users SET {change} WHERE id = ?", (user["id"],))
        page = client.get("/no-such-address")
        text = page.get_data(as_text=True)
        assert page.status_code == 404 and '<header class="topbar">' not in text and 'lang="it"' in text
        assert page.headers["Cache-Control"] == "private, no-store"
        db.execute(f"UPDATE users SET {undo} WHERE id = ?", (user["id"],))
    db.execute("UPDATE site_settings SET maintenance_mode = 1")
    page = client.get("/no-such-address")
    assert page.status_code == 404 and '<header class="topbar">' not in page.get_data(as_text=True)


@pytest.mark.parametrize(("method", "path", "code", "italian", "english"), [
    ("GET", "/_fail/400", 400, "La richiesta non è stata compresa", "The browser (or proxy)"),
    ("DELETE", "/login", 405, "Questo indirizzo non accetta", "The method is not allowed"),
    ("GET", "/_fail/500", 500, "Qualcosa è andato storto.", "Something went wrong"),
])
def test_error_pages_explain_the_status_in_the_visitors_language(failing_app, client, method, path, code,
                                                                  italian, english):
    page = client.open(path + "?lang=it", method=method)
    text = page.get_data(as_text=True)
    assert page.status_code == code and italian in text and english not in text
    refused = client.open(path, method=method, headers=JSON)
    assert refused.status_code == code and refused.json["error"].startswith(italian)


def test_a_message_chosen_by_the_view_replaces_the_status_text(failing_app, client):
    text = client.get("/_fail/date?lang=it").get_data(as_text=True)
    assert "Quella data non esiste." in text and "La richiesta non è stata compresa" not in text


def test_switched_off_features_answer_scripts_in_the_visitors_language(admin_client, db):
    db.execute("UPDATE plugins SET enabled = 0 WHERE id = 'kanban'")
    response = admin_client.get("/api/kanban/list-order-version?lang=de", headers=JSON)
    assert response.status_code == 404 and response.json == {"error": "Diese Funktion ist nicht aktiviert."}
