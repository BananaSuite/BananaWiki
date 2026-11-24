"""The top bar's "Go to" menu: the platform's main wikis that are online, BananaChat and BananaGit."""

from __future__ import annotations

import re

import pytest

MENU = re.compile(r'<details class="goto-menu" data-goto-menu>.*?</details>', re.S)


def _menu(web, path="/login"):
    match = MENU.search(web.get(path).get_data(as_text=True))
    return match.group(0) if match else None


def _links(menu):
    return re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', menu or "")


def _main(query, wiki, **values):
    query("UPDATE instances SET is_main = 1, expires_at = NULL WHERE id = ?", (wiki["id"],))
    for column, value in values.items():
        query(f"UPDATE instances SET {column} = ? WHERE id = ?", (value, wiki["id"]))


def _apps(query, chat="", git=""):
    query("UPDATE hosting_settings SET goto_chat_url = ?, goto_git_url = ? WHERE id = 1", (chat, git))


@pytest.fixture
def admin(make_account):
    return make_account("chief", admin=True)


def test_without_anything_to_list_there_is_no_menu(web, make_account, make_wiki):
    make_wiki(make_account())  # an ordinary wiki is never listed
    assert _menu(web) is None


def test_main_wikis_that_are_online_are_listed_with_their_address(web, admin, make_wiki, query):
    wiki = make_wiki(admin, "handbook")
    _main(query, wiki)
    assert query("SELECT status, provisioning_state FROM instances WHERE id = ?", (wiki["id"],), one=True) == {
        "status": "running", "provisioning_state": "ready"}
    assert _links(_menu(web)) == [("https://handbook-hosting.wiki.test", "handbook")]


@pytest.mark.parametrize("state", [
    {"status": "stopped"}, {"status": "suspended"}, {"status": "terminated"},
    {"provisioning_state": "pending"}, {"expires_at": "2001-01-01 00:00:00"},
])
def test_main_wikis_that_are_not_serving_are_left_out(web, admin, make_wiki, query, state):
    _main(query, make_wiki(admin, "handbook"), **state)
    assert _menu(web) is None


def test_a_main_wiki_of_a_suspended_account_is_left_out(web, make_account, make_wiki, query):
    owner = make_account("owner")
    _main(query, make_wiki(owner, "handbook"))
    assert _menu(web) is not None
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (owner["id"],))
    assert _menu(web) is None


def test_the_apps_are_listed_with_what_signing_in_takes(web, make_account, login, query):
    _apps(query, chat="https://chat.example.org", git="https://git.example.org/")
    menu = _menu(web)
    assert _links(menu) == [("https://chat.example.org", "BananaChat"), ("https://git.example.org/", "BananaGit")]
    assert "Sign in with your hosting account." in menu
    assert "works only if an administrator has given you access" in menu
    assert 'aria-describedby="goto-note-git"' in menu and 'id="goto-note-git"' in menu
    # Signed in too, and only the app that has an address.
    _apps(query, git="https://git.example.org/")
    login(web, make_account("member"))
    assert _links(_menu(web, "/account")) == [("https://git.example.org/", "BananaGit")]


def test_the_menu_works_without_scripts_and_carries_no_inline_code(web, query):
    _apps(query, chat="https://chat.example.org")
    menu = _menu(web)
    assert "<summary" in menu and 'id="goto-menu-list"' in menu and "Go to" in menu
    assert not re.search(r"\son[a-z]+=", menu) and "<script" not in menu and "style=" not in menu


def _save(web, **values):
    return web.post("/admin/settings", data={"action": "save_apps", **values})


def test_administrators_set_the_app_addresses(web, admin, login, query):
    login(web, admin)
    page = web.get("/admin/settings").get_data(as_text=True)
    assert 'id="save_apps"' in page and 'name="goto_chat_url"' in page and 'name="goto_git_url"' in page
    assert _save(web, goto_chat_url=" https://chat.example.org ", goto_git_url="").status_code == 302
    row = query("SELECT goto_chat_url, goto_git_url FROM hosting_settings", one=True)
    assert row == {"goto_chat_url": "https://chat.example.org", "goto_git_url": ""}
    events = query("SELECT reason FROM hosting_events WHERE action = 'settings.changed'")
    assert any("goto_chat_url" in event["reason"] for event in events)


@pytest.mark.parametrize("url", ["http://chat.example.org", "javascript:alert(1)", "chat.example.org",
                                 "https://user:pw@chat.example.org", "https://chat example.org", "https://localhost",
                                 "https://chat.example.org:99999", "https://" + "a" * 300 + ".org", "//chat.example.org"])
def test_only_https_addresses_are_taken(web, admin, login, query, url):
    login(web, admin)
    response = _save(web, goto_chat_url=url, goto_git_url="https://git.example.org")
    assert "open=save_apps" in response.headers["Location"]
    page = web.get(response.headers["Location"]).get_data(as_text=True)
    assert "The address of BananaChat must be a complete https address." in page
    assert query("SELECT goto_chat_url, goto_git_url FROM hosting_settings", one=True) == {
        "goto_chat_url": "", "goto_git_url": ""}


def test_only_administrators_set_them(web, make_account, login, query):
    login(web, make_account("member"))
    assert _save(web, goto_chat_url="https://chat.example.org").status_code == 403
    assert query("SELECT goto_chat_url FROM hosting_settings", one=True)["goto_chat_url"] == ""


def test_a_stored_address_that_is_not_https_is_never_shown(web, query):
    _apps(query, chat="javascript:alert(1)")
    assert _menu(web) is None


def test_version_13_adds_the_columns_and_the_finds_table(query):
    row = query("SELECT easter_egg_configs, goto_chat_url, goto_git_url FROM hosting_settings", one=True)
    assert row == {"easter_egg_configs": None, "goto_chat_url": "", "goto_git_url": ""}
    table = query("SELECT sql FROM sqlite_master WHERE name = 'account_egg_finds'", one=True)["sql"]
    assert table == ("CREATE TABLE account_egg_finds (account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, "
                     "egg_id TEXT NOT NULL, found_at TEXT NOT NULL, PRIMARY KEY (account_id, egg_id))")
