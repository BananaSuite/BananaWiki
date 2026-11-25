"""One Save per portal settings page, at the top right in its sticky header: none at the bottom or per section.

Also the forms that were split up for it: a wiki's settings on its administration page (sign-in, storage limit,
upload policy) are saved together, a banner is edited on its own page, and every collaborator's access is saved
together.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.save_controls import assert_one_save_in_header, assert_save_is_the_only_primary, save_controls

CSS = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting" / "static" / "css" / "hosting.css"
BANNER = {"content": "Planned maintenance tonight", "color": "orange", "visibility": "both", "audience_mode": "all"}


@pytest.fixture
def site(web, make_account, make_wiki, make_app, login, query):
    """An administrator signed in on *web*, an owner's wiki with a collaborator, a developer app and a banner."""
    admin, owner, helper = make_account("chief", admin=True), make_account("owner"), make_account("helper")
    wiki = make_wiki(owner, "team")
    app = make_app(admin)
    login(web, admin)
    web.post("/admin/banners/create", data=BANNER)
    web.post(f"/instances/{wiki['id']}/collaborators/add",
             data={"username": helper["username"], "role": "custom", "permissions": ["view"]})
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators", one=True)["n"] == 1
    banner_id = query("SELECT id FROM hosting_banners", one=True)["id"]
    return SimpleNamespace(admin=admin, owner=owner, helper=helper, wiki=wiki, app=app, banner_id=banner_id)


def _settings_pages(s) -> list[str]:
    return ["/admin/settings", "/account", f"/account/developer-apps/{s.app['id']}", f"/admin/accounts/{s.owner['id']}",
            f"/admin/instances/{s.wiki['id']}", f"/instances/{s.wiki['id']}", f"/admin/banners/{s.banner_id}",
            f"/instances/{s.wiki['id']}/collaborators"]


def _other_pages(s) -> list[str]:
    return ["/dashboard", "/admin", "/admin/accounts", "/admin/wikis", "/admin/banners", "/admin/moderation",
            "/admin/merge-requests", "/account/mfa", "/account/developer-apps", f"/instances/{s.wiki['id']}/domain",
            f"/instances/{s.wiki['id']}/analytics", f"/admin/accounts/{s.admin['id']}",
            f"/admin/instances/{s.wiki['id']}/account-links"]


def _page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def test_every_settings_page_has_one_save_in_its_header(web, site):
    for path in _settings_pages(site):
        page = _page(web, path)
        assert_one_save_in_header(page, where=path)
        assert_save_is_the_only_primary(page, where=path)
        assert len(save_controls(page)) == 1, path


def test_no_other_page_has_a_save_outside_its_header(web, site):
    for path in _other_pages(site):
        assert_one_save_in_header(_page(web, path), where=path)
    assert save_controls(_page(web, "/admin/banners")) == []  # the list: Create, Edit and Delete


def test_an_owner_sees_one_save_in_the_header_too(portal, site, login):
    client = portal.test_client()
    login(client, site.owner)
    for path in ("/account", f"/instances/{site.wiki['id']}", f"/instances/{site.wiki['id']}/collaborators"):
        page = _page(client, path)
        assert_one_save_in_header(page, where=path)
        assert_save_is_the_only_primary(page, where=path)
        assert len(save_controls(page)) == 1, path


def test_no_rule_hides_a_second_save():
    css = CSS.read_text(encoding="utf-8")
    assert ".form-save" not in css
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"display\s*:\s*none", body):
            assert not re.search(r"save|submit", selector, re.I), selector.strip()


def test_the_set_expiry_button_names_its_action(web, site):
    page = _page(web, f"/admin/instances/{site.wiki['id']}")
    assert ">Set expiry</button>" in page and ">Take snapshot</button>" in page


# A wiki's settings on its administration page


def _instance_settings(client, wiki, **changed):
    data = {"mode": "", "storage_limit_mb": "", "upload_max_size_mb": "", "inherit_upload_size": "1",
            "upload_blocked_extensions": "", **changed}
    return client.post(f"/admin/instances/{wiki['id']}/settings", data=data)


def _stored(query, wiki):
    return query("SELECT platform_signin_mode, storage_limit_mb, upload_max_size_mb, upload_blocked_extensions "
                 "FROM instances WHERE id = ?", (wiki["id"],), one=True)


def _events(query, wiki) -> list[str]:
    return [row["action"] for row in query("SELECT action FROM hosting_events WHERE subject_id = ? ORDER BY id",
                                           (wiki["id"],))]


def test_the_wiki_settings_save_every_part_with_one_restart(web, site, query, runtime):
    before = len(runtime.called("restart"))
    response = _instance_settings(web, site.wiki, mode="local", storage_limit_mb="200", upload_max_size_mb="5",
                                  inherit_upload_size="", upload_blocked_extensions="EXE, .bat")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/admin/instances/{site.wiki['id']}")
    assert _stored(query, site.wiki) == {"platform_signin_mode": "local", "storage_limit_mb": 200,
                                         "upload_max_size_mb": 5, "upload_blocked_extensions": "exe,bat"}
    actions = _events(query, site.wiki)
    assert {"oauth.signin_mode", "instance.storage_limit", "instance.upload_policy"} <= set(actions)
    assert len(runtime.called("restart")) == before + 1
    page = _page(web, f"/admin/instances/{site.wiki['id']}")
    assert 'value="local" selected' in page and 'value="200"' in page and "Settings saved." in page


def test_saving_the_wiki_settings_unchanged_stores_nothing(web, site, query, runtime):
    _instance_settings(web, site.wiki, storage_limit_mb="300")
    actions, restarts = _events(query, site.wiki), len(runtime.called("restart"))
    assert _instance_settings(web, site.wiki, storage_limit_mb="300").status_code == 302
    assert _events(query, site.wiki) == actions and len(runtime.called("restart")) == restarts


def test_each_limit_is_one_choice(web, site, query):
    page = _page(web, f"/admin/instances/{site.wiki['id']}")
    assert '<option value="platform" selected>' in page and 'name="unlimited"' not in page
    assert 'name="inherit_upload_size"' not in page
    _instance_settings(web, site.wiki, storage_limit="none", storage_limit_mb="50")
    assert _stored(query, site.wiki)["storage_limit_mb"] == 0
    assert '<option value="none" selected>No limit</option>' in _page(web, f"/admin/instances/{site.wiki['id']}")
    _instance_settings(web, site.wiki, storage_limit="custom", storage_limit_mb="70", upload_size="custom",
                       upload_max_size_mb="9")
    assert _stored(query, site.wiki)["storage_limit_mb"] == 70 and _stored(query, site.wiki)["upload_max_size_mb"] == 9
    _instance_settings(web, site.wiki, storage_limit="platform", storage_limit_mb="70", upload_size="platform",
                       upload_max_size_mb="9")
    stored = _stored(query, site.wiki)
    assert stored["storage_limit_mb"] is None and stored["upload_max_size_mb"] is None
    assert _instance_settings(web, site.wiki, storage_limit="custom", storage_limit_mb="").status_code == 400


def test_a_form_from_before_the_choices_still_saves(web, site, query):
    _instance_settings(web, site.wiki, unlimited="1", storage_limit_mb="50")
    assert _stored(query, site.wiki)["storage_limit_mb"] == 0
    _instance_settings(web, site.wiki)
    assert _stored(query, site.wiki)["storage_limit_mb"] is None


@pytest.mark.parametrize("changed", [
    {"storage_limit_mb": "lots"}, {"storage_limit_mb": "0"}, {"upload_max_size_mb": "5000", "inherit_upload_size": ""},
    {"mode": "nonsense"}, {"mode": "platform"},  # nobody of the wiki is linked to a hosting account yet
])
def test_a_refused_part_stores_nothing_and_keeps_what_was_typed(web, site, query, changed):
    stored, actions = _stored(query, site.wiki), _events(query, site.wiki)
    typed = {"mode": "local", "storage_limit_mb": "120", "upload_blocked_extensions": "exe", **changed}
    response = _instance_settings(web, site.wiki, **typed)
    assert response.status_code == 400
    assert _stored(query, site.wiki) == stored and _events(query, site.wiki) == actions
    page = response.get_data(as_text=True)
    assert 'id="panel-3" data-remember open' in page and 'class="alert' in page
    assert f'value="{typed["storage_limit_mb"]}"' in page and 'name="upload_blocked_extensions" type="text" value="exe"' in page
    assert_one_save_in_header(page, where="refused settings")


def test_only_administrators_save_a_wikis_settings(portal, site, login, query):
    client = portal.test_client()
    login(client, site.owner)
    stored = _stored(query, site.wiki)
    assert _instance_settings(client, site.wiki, storage_limit_mb="900").status_code == 403
    assert _stored(query, site.wiki) == stored


def test_the_parts_old_addresses_still_save_one_part(web, site, query):
    web.post(f"/admin/instances/{site.wiki['id']}/storage-limit", data={"storage_limit_mb": "77"})
    web.post(f"/admin/instances/{site.wiki['id']}/upload-policy",
             data={"upload_max_size_mb": "9", "upload_blocked_extensions": "exe"})
    web.post(f"/admin/instances/{site.wiki['id']}/signin-mode", data={"mode": "both"})
    assert _stored(query, site.wiki) == {"platform_signin_mode": "both", "storage_limit_mb": 77,
                                         "upload_max_size_mb": 9, "upload_blocked_extensions": "exe"}


# Banners


def test_each_banner_is_edited_on_its_own_page(web, site, query):
    listing = _page(web, "/admin/banners")
    assert f'href="/admin/banners/{site.banner_id}"' in listing and f"/admin/banners/{site.banner_id}/delete" in listing
    page = _page(web, f"/admin/banners/{site.banner_id}")
    assert BANNER["content"] in page and f'action="/admin/banners/{site.banner_id}/edit"' in page
    response = web.post(f"/admin/banners/{site.banner_id}/edit", data={**BANNER, "content": "Back at nine",
                                                                         "is_active": "1"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/banners")
    assert query("SELECT content, revision FROM hosting_banners", one=True)["content"] == "Back at nine"


def test_a_refused_banner_comes_back_with_what_was_typed(web, site, query):
    response = web.post(f"/admin/banners/{site.banner_id}/edit",
                        data={**BANNER, "content": "Only for some", "audience_mode": "allowlist", "color": "blue"})
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert "Only for some" in page and 'value="allowlist" selected' in page and 'value="blue" selected' in page
    assert_one_save_in_header(page, where="refused banner")
    assert query("SELECT content FROM hosting_banners", one=True)["content"] == BANNER["content"]


def test_only_administrators_open_a_banner(portal, web, site, login):
    assert web.get("/admin/banners/999999").status_code == 404
    client = portal.test_client()
    login(client, site.owner)
    assert client.get(f"/admin/banners/{site.banner_id}").status_code == 403
    assert client.post(f"/admin/banners/{site.banner_id}/edit", data=BANNER).status_code == 403


# Collaborators


def test_every_collaborators_access_is_saved_together(portal, site, make_account, login, query):
    other = make_account("other")
    client = portal.test_client()
    login(client, site.owner)
    wiki_id = site.wiki["id"]
    client.post(f"/instances/{wiki_id}/collaborators/add", data={"username": "other", "role": "custom",
                                                                  "permissions": ["view"]})
    page = _page(client, f"/instances/{wiki_id}/collaborators")
    assert f'name="role-{site.helper["id"]}"' in page and f'name="role-{other["id"]}"' in page
    response = client.post(f"/instances/{wiki_id}/collaborators/save", data={
        "members": [site.helper["id"], other["id"]], f"role-{site.helper['id']}": "full_access",
        f"role-{other['id']}": "custom", f"permissions-{other['id']}": ["view", "start_stop"]})
    assert response.status_code == 302
    rows = {row["account_id"]: row for row in query("SELECT account_id, role, permissions FROM instance_collaborators")}
    assert rows[site.helper["id"]]["role"] == "full_access"
    assert rows[other["id"]]["role"] == "custom" and "start_stop" in rows[other["id"]]["permissions"]
    assert "Access updated." in _page(client, f"/instances/{wiki_id}/collaborators")


def test_a_collaborator_without_the_right_cannot_save_access(portal, site, login, query):
    client = portal.test_client()
    login(client, site.helper)  # may only view the wiki
    before = query("SELECT role, permissions FROM instance_collaborators")
    response = client.post(f"/instances/{site.wiki['id']}/collaborators/save", data={
        "members": [site.helper["id"]], f"role-{site.helper['id']}": "full_access"})
    assert response.status_code == 404
    assert query("SELECT role, permissions FROM instance_collaborators") == before


def test_a_manager_sees_full_access_collaborators_without_controls(portal, site, make_account, login):
    manager = make_account("manager")
    owner = portal.test_client()
    login(owner, site.owner)
    wiki_id = site.wiki["id"]
    owner.post(f"/instances/{wiki_id}/collaborators/save", data={
        "members": [site.helper["id"]], f"role-{site.helper['id']}": "full_access"})
    owner.post(f"/instances/{wiki_id}/collaborators/add", data={
        "username": "manager", "role": "custom", "permissions": ["view", "manage_collaborators"]})
    client = portal.test_client()
    login(client, manager)
    page = _page(client, f"/instances/{wiki_id}/collaborators")
    assert f'name="role-{site.helper["id"]}"' not in page and f"remove-collab-{site.helper['id']}" not in page
    assert f'name="role-{manager["id"]}"' in page
    assert_one_save_in_header(page, where="manager")
