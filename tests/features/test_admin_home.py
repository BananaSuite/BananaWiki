"""The administration overview replaces the side menu: every admin page is listed once, grouped, on /admin/home."""

from __future__ import annotations


def test_overview_lists_every_admin_page_by_group(admin_client):
    page = admin_client.get("/admin/home").data.decode()
    for group in ("People and access", "Wiki settings", "Content and apps", "System"):
        assert group in page
    for url in ("/admin/dashboard", "/admin/users", "/admin/settings", "/admin/canvas", "/admin/audit"):
        assert f'href="{url}"' in page, url


def test_admin_pages_have_a_breadcrumb_and_no_side_menu(admin_client):
    page = admin_client.get("/admin/canvas").data.decode()
    assert "admin-nav" not in page
    assert 'href="/admin/home"' in page and "Content and apps" in page
    assert "data-admin-jump" in page


def test_overview_is_for_administrators(client, login, make_user):
    assert client.get("/admin/home").status_code in (302, 401, 403, 404)
    login(client, make_user("plain_editor", role="editor"))
    assert client.get("/admin/home").status_code in (403, 404)


def test_the_account_menu_and_the_old_address_lead_to_the_overview(admin_client):
    assert 'href="/admin/home"' in admin_client.get("/").data.decode()
    assert admin_client.get("/admin").headers["Location"].endswith("/admin/home")
