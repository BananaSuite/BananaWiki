"""The administration menu: every admin page is listed once, grouped, in the sidebar of each admin page."""

from __future__ import annotations


def _menu(page: str) -> str:
    return page[page.index('id="sidebar"'):page.index("</nav>", page.index('id="sidebar"'))]


def test_every_admin_page_has_the_grouped_menu(admin_client):
    for url in ("/admin/dashboard", "/admin/canvas", "/admin/settings"):
        menu = _menu(admin_client.get(url).get_data(as_text=True))
        assert 'aria-label="Administration"' in admin_client.get(url).get_data(as_text=True)
        for group in ("People", "Wiki", "Content", "System"):
            assert f">{group}</p>" in menu, (url, group)
        for link in ("/admin/dashboard", "/admin/users", "/admin/settings", "/admin/canvas", "/admin/audit"):
            assert f'href="{link}"' in menu, (url, link)
        assert 'href="/">' in menu, "a way back to the wiki"


def test_the_menu_marks_the_current_page_only(admin_client):
    menu = _menu(admin_client.get("/admin/canvas").get_data(as_text=True))
    assert menu.count('aria-current="page"') == 1
    assert 'href="/admin/canvas" aria-current="page"' in menu


def test_admin_pages_have_no_page_tree_breadcrumb_or_jump_list(admin_client):
    page = admin_client.get("/admin/canvas").get_data(as_text=True)
    assert "data-admin-jump" not in page and "admin-crumbs" not in page
    assert 'class="nav-tree' not in page


def test_the_overview_address_leads_to_the_dashboard(client, login, make_user, admin_client):
    assert admin_client.get("/admin/home").headers["Location"].endswith("/admin/dashboard")
    assert client.get("/admin/home").status_code in (302, 401, 403, 404)
    login(client, make_user("plain_editor", role="editor"))
    assert client.get("/admin/home").status_code in (403, 404)


def test_the_account_menu_leads_to_the_dashboard(admin_client):
    page = admin_client.get("/").get_data(as_text=True)
    assert 'href="/admin/dashboard"' in page
    assert admin_client.get("/admin").headers["Location"].endswith("/admin/dashboard")
