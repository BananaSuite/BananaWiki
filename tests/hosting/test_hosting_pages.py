"""Every portal page renders for the roles that may see it, in every language."""

from __future__ import annotations

import re

import pytest

from bananawiki.hosting.i18n import LANGUAGES

UNTRANSLATED = re.compile(r"(?<![\w/-])(?:hosting|email)\.[a-z_]+\.[a-z_.]+")

PUBLIC_PAGES = ["/help", "/login", "/forgot-password",
                "/forgot-username", "/health", "/healthz"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(web, path):
    response = web.get(path)
    assert response.status_code == 200, path


def test_legal_pages_point_at_the_main_site(web):
    for path in ("/terms", "/privacy", "/compliance"):
        response = web.get(path)
        assert response.status_code == 302 and response.headers["Location"].startswith("https://wiki.test/"), path


def test_help_article_renders(web):
    index = web.get("/help").get_data(as_text=True)
    assert "/help/" in index
    slug = index.split('href="/help/', 1)[1].split('"', 1)[0]
    assert web.get(f"/help/{slug}").status_code == 200
    assert web.get("/help/does-not-exist").status_code == 404


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_help_renders_in_every_language(web, language):
    index = web.get(f"/help?lang={language}")
    assert index.status_code == 200 and f"lang={language}" in index.get_data(as_text=True)


def test_signup_page_on_empty_platform_needs_bootstrap_token(web):
    assert web.get("/signup").status_code == 404
    assert web.get("/signup?bootstrap_token=bootstrap-token-for-tests").status_code == 200


def test_private_pages_redirect_to_login(web):
    for path in ("/dashboard", "/account", "/admin", "/instances/create", "/account/mfa"):
        response = web.get(path)
        assert response.status_code == 302 and "/login" in response.headers["Location"], path


def _owner_pages(wiki_id: str) -> list[str]:
    return ["/dashboard", "/instances/create", "/account", "/account/mfa", f"/instances/{wiki_id}",
            f"/instances/{wiki_id}/collaborators", f"/instances/{wiki_id}/analytics", f"/instances/{wiki_id}/domain",
            "/account/merge-request", "/account/merge/pending"]


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_owner_pages_render(web, make_account, make_wiki, login, language):
    owner = make_account()
    wiki = make_wiki(owner)
    login(web, owner)
    web.post("/language", data={"language": language})
    for path in _owner_pages(wiki["id"]):
        response = web.get(path)
        assert response.status_code == 200, (path, response.status_code)
        body = response.get_data(as_text=True)
        assert not UNTRANSLATED.search(body), (path, UNTRANSLATED.search(body))


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_admin_pages_render(web, make_account, make_wiki, login, language):
    admin = make_account(admin=True)
    user = make_account()
    wiki = make_wiki(user)
    login(web, admin)
    web.post("/language", data={"language": language})
    pages = ["/admin", "/admin/settings", "/admin/banners", "/admin/merge-requests", "/admin/moderation",
             "/admin/instances/import", "/admin/merge-accounts", f"/admin/accounts/{user['id']}",
             f"/admin/accounts/{user['id']}/delete", f"/admin/instances/{wiki['id']}",
             f"/admin/instances/{wiki['id']}/logs", f"/instances/{wiki['id']}"]
    for path in pages:
        response = web.get(path)
        assert response.status_code == 200, (path, response.status_code)
        body = response.get_data(as_text=True)
        assert not UNTRANSLATED.search(body), (path, UNTRANSLATED.search(body))


@pytest.mark.parametrize("language", sorted(set(LANGUAGES) - {"en"}))
def test_translation_catalogues_have_the_same_keys(language):
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting" / "translations"
    en = json.loads((root / "en.json").read_text(encoding="utf-8"))
    translated = json.loads((root / f"{language}.json").read_text(encoding="utf-8"))
    assert set(en) == set(translated)
    assert all(translated[key] for key in translated)


def test_accept_language_chooses_among_the_portal_languages(web):
    def lang(header: str) -> str:
        page = web.get("/login", headers={"Accept-Language": header}).get_data(as_text=True)
        return re.search(r'<html lang="([a-z-]+)"', page).group(1)

    assert lang("de") == lang("de-CH, de;q=0.9, en;q=0.5") == lang("fr, de-AT;q=0.8") == "de"
    assert lang("it-IT, it;q=0.9") == "it"
    assert lang("*") == lang("fr") == "en"  # a wildcard or an unknown language gets English


def test_pages_render_in_less_common_states(portal, make_account, make_wiki, login, query):
    admin, owner, other = make_account(admin=True), make_account(), make_account()
    gone = make_wiki(owner, "gone-wiki")
    kept = make_wiki(owner, "kept-wiki")
    query("UPDATE hosting_settings SET api_enabled = 1, platform_oauth_enabled = 1 WHERE id = 1")
    owner_client, other_client, admin_client = (portal.test_client() for _ in range(3))
    login(owner_client, owner)
    login(other_client, other)
    login(admin_client, admin)
    owner_client.post(f"/instances/{gone['id']}/terminate")
    owner_client.post(f"/instances/{kept['id']}/transfer", data={"username": other["username"]})
    owner_client.post(f"/instances/{kept['id']}/stop")
    other_client.post("/account/merge-request", data={"target_username": owner["username"]})
    admin_client.post(f"/admin/instances/{kept['id']}/suspend", data={"suspend_duration": "permanent"})
    checks = [
        (admin_client, f"/admin/instances/{gone['id']}"), (admin_client, "/admin"), (admin_client, "/admin/merge-requests"),
        (other_client, "/dashboard"), (other_client, "/account"), (other_client, "/account/merge/pending"),
        (owner_client, "/account/merge/pending"), (owner_client, f"/instances/{kept['id']}"),
        (owner_client, "/dashboard"), (owner_client, "/account"),
    ]
    for client, path in checks:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert not UNTRANSLATED.search(response.get_data(as_text=True)), (path, UNTRANSLATED.search(response.get_data(as_text=True)))
