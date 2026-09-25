"""Tests for soft-deletion & admin restoration of wiki pages.

Covers:
- Sidebar icon / styling for pending-deletion pages (admin-only)
- Restore UI on page view (admin sees restore banner, not standard actions)
- Category edge cases (empty categories hidden for non-admins)
- Non-admin users cannot see pending-deletion pages
"""

import time

import db


# -----------------------------------------------------------------------
# DB layer: mark and restore pending deletion
# -----------------------------------------------------------------------

def test_mark_page_pending_deletion(admin_user):
    """Marking a page sets pending_deletion flag."""
    page_id = db.create_page("PD Page", "pd-page", "content")
    result = db.mark_page_pending_deletion(page_id, admin_user)
    assert result is True
    page = db.get_page(page_id)
    assert page["pending_deletion"] == 1


def test_repeated_mark_does_not_reset_timer(admin_user):
    """Calling mark_page_pending_deletion twice must not reset the timestamp."""
    page_id = db.create_page("Repeat PD", "repeat-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    original_at = db.get_page(page_id)["pending_deletion_at"]
    time.sleep(0.05)
    result = db.mark_page_pending_deletion(page_id, admin_user)
    assert result is False
    assert db.get_page(page_id)["pending_deletion_at"] == original_at


def test_restore_page_from_pending_deletion(admin_user):
    """Restoring clears pending_deletion flag."""
    page_id = db.create_page("Restore Page", "restore-page", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    result = db.restore_page_from_pending_deletion(page_id)
    assert result is True
    page = db.get_page(page_id)
    assert page["pending_deletion"] == 0


def test_get_pending_deletion_info(admin_user):
    """get_pending_deletion_info returns metadata for pending pages."""
    page_id = db.create_page("Info Page", "info-page", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    info = db.get_pending_deletion_info(page_id)
    assert info is not None
    assert info["page_id"] == page_id
    assert info["expires_at"] is not None


# -----------------------------------------------------------------------
# Sidebar visibility: pending-deletion pages hidden from non-admins
# -----------------------------------------------------------------------

def test_pending_deletion_page_hidden_from_regular_user(client, admin_user, regular_user):
    """Regular users should not see pending-deletion pages via URL."""
    page_id = db.create_page("Hidden PD", "hidden-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/page/hidden-pd")
    assert resp.status_code == 403


def test_pending_deletion_page_hidden_from_editor(client, editor_user, admin_user):
    """Editors should not see pending-deletion pages."""
    page_id = db.create_page("Editor PD", "editor-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/page/editor-pd")
    assert resp.status_code == 403


def test_pending_deletion_page_visible_to_editor_with_delete_capability(client, editor_user, admin_user):
    """Editors with delete capability (via custom role) can review pending pages."""
    role_id = db.create_custom_role(
        name="Editor Delete Reviewer",
        base_role="editor",
        permission_keys=["page.view_all", "page.delete"],
        created_by=admin_user,
    )
    db.assign_custom_role(editor_user, role_id)
    page_id = db.create_page("Editor Reviewer PD", "editor-reviewer-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.get("/page/editor-reviewer-pd")
    assert resp.status_code == 200


def test_pending_deletion_page_visible_to_admin(logged_in_admin):
    """Admins should still be able to view pending-deletion pages."""
    page_id = db.create_page("Admin PD", "admin-pd", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/page/admin-pd")
    assert resp.status_code == 200


# -----------------------------------------------------------------------
# Admin page view: pending-deletion banner and restore button
# -----------------------------------------------------------------------

def test_admin_sees_pending_deletion_banner(logged_in_admin):
    """Admin viewing a pending-deletion page sees the pending deletion banner."""
    page_id = db.create_page("Banner Page", "banner-page", "Banner content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/page/banner-page")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Pending Deletion" in html
    assert "pending-deletion-banner" in html


def test_admin_sees_restore_button(logged_in_admin):
    """Admin viewing a pending-deletion page sees a Restore button."""
    page_id = db.create_page("Restore Btn", "restore-btn", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/page/restore-btn")
    html = resp.data.decode()
    assert "Restore Page" in html
    assert "btn-restore" in html


def test_admin_no_standard_actions_on_pending_deletion(logged_in_admin):
    """Admin viewing a pending-deletion page should NOT see standard Edit/Delete buttons."""
    page_id = db.create_page("No Actions", "no-actions", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/page/no-actions")
    html = resp.data.decode()
    # The standard edit link should not be present in the page-header
    assert 'href="/page/no-actions/edit"' not in html
    # No delete form for this page
    assert 'action="/page/no-actions/delete"' not in html


def test_admin_restore_from_page_view(logged_in_admin):
    """Admin can restore a page from the pending-deletions admin route."""
    page_id = db.create_page("Restorable", "restorable", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.post(
        f"/admin/pending-deletions/{page_id}/restore",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    page = db.get_page(page_id)
    assert page["pending_deletion"] == 0


def test_admin_restore_redirects_to_page(logged_in_admin):
    """After restoring a page, the admin is redirected to the page itself."""
    page_id = db.create_page("Redirect Test", "redirect-test", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.post(
        f"/admin/pending-deletions/{page_id}/restore",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/page/redirect-test" in resp.headers["Location"]


# -----------------------------------------------------------------------
# Sidebar: pending-deletion icon for admins
# -----------------------------------------------------------------------

def test_admin_sidebar_shows_pending_deletion_icon(logged_in_admin):
    """Pending-deletion pages in sidebar should show trash icon for admins."""
    cat_id = db.create_category("PD Cat")
    page_id = db.create_page("PD Sidebar", "pd-sidebar", "content", category_id=cat_id)
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    assert "pending-deletion-nav-item" in html
    assert "sidebar-pending-deletion-indicator" in html


def test_admin_sidebar_shows_strikethrough_for_pending_deletion(logged_in_admin):
    """Pending-deletion page titles should be struck through in sidebar."""
    page_id = db.create_page("Struck Page", "struck-page", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    assert "<s>Struck Page</s>" in html


# -----------------------------------------------------------------------
# Category edge cases
# -----------------------------------------------------------------------

def test_empty_category_hidden_for_regular_user(client, admin_user, regular_user):
    """A category where all pages are pending deletion should be hidden for non-admins."""
    cat_id = db.create_category("Empty Cat")
    page_id = db.create_page("Only Page", "only-page", "content", category_id=cat_id)
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/")
    html = resp.data.decode()
    assert "Empty Cat" not in html


def test_category_with_visible_pages_still_shown(client, admin_user, regular_user):
    """A category with at least one visible page should remain visible."""
    cat_id = db.create_category("Partial Cat")
    db.create_page("Visible", "visible-page", "content", category_id=cat_id)
    page_id = db.create_page("Deleted", "deleted-page", "content", category_id=cat_id)
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/")
    html = resp.data.decode()
    assert "Partial Cat" in html
    assert "Visible" in html
    assert "/page/deleted-page" not in html


def test_admin_sees_category_with_only_pending_pages(logged_in_admin):
    """Admins should still see categories where all pages are pending deletion."""
    cat_id = db.create_category("Admin Cat")
    page_id = db.create_page("Admin Only", "admin-only", "content", category_id=cat_id)
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    assert "Admin Cat" in html


# -----------------------------------------------------------------------
# Pending deletion page cannot be edited
# -----------------------------------------------------------------------

def test_pending_deletion_page_not_editable(logged_in_admin):
    """Pages pending deletion should not be editable."""
    page_id = db.create_page("No Edit PD", "no-edit-pd", "content")
    admin = db.get_user_by_username("admin")
    db.mark_page_pending_deletion(page_id, admin["id"])
    resp = logged_in_admin.get("/page/no-edit-pd/edit", follow_redirects=True)
    html = resp.data.decode()
    assert "pending deletion" in html.lower()


# -----------------------------------------------------------------------
# Search: pending-deletion pages excluded from search results
# -----------------------------------------------------------------------

def test_search_pages_excludes_pending_deletion(admin_user):
    """search_pages should not return pages that are pending deletion."""
    page_id = db.create_page("Searchable PD", "searchable-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    results = db.search_pages("Searchable PD")
    assert all(r["id"] != page_id for r in results)


def test_search_pages_full_excludes_pending_deletion(admin_user):
    """search_pages_full should not return pages that are pending deletion."""
    page_id = db.create_page("FullSearch PD", "fullsearch-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    results = db.search_pages_full("FullSearch PD")
    assert all(r["id"] != page_id for r in results)


def test_search_pages_full_content_excludes_pending_deletion(admin_user):
    """search_pages_full with search_content=True should not return pending deletion pages."""
    page_id = db.create_page("ContentPD", "content-pd", "unique_body_text_xyz")
    db.mark_page_pending_deletion(page_id, admin_user)
    results = db.search_pages_full("unique_body_text_xyz", search_content=True)
    assert all(r["id"] != page_id for r in results)


def test_search_pages_full_deindexed_excludes_pending_deletion(admin_user):
    """search_pages_full with include_deindexed=True still excludes pending deletion pages."""
    page_id = db.create_page("DeindexPD", "deindex-pd", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    results = db.search_pages_full("DeindexPD", include_deindexed=True)
    assert all(r["id"] != page_id for r in results)


def test_api_sidebar_search_hides_pending_deletion(client, admin_user, regular_user):
    """The sidebar search API should not return pending-deletion pages to regular users."""
    page_id = db.create_page("API PD Page", "api-pd-page", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.get("/api/sidebar/search?q=API+PD+Page")
    data = resp.get_json()
    assert all(p["id"] != page_id for p in data.get("pages", []))


# -----------------------------------------------------------------------
# Pending deletion redirect: editor is brought to the page
# -----------------------------------------------------------------------

def test_pending_deletion_redirects_to_page(client, admin_user, editor_user):
    """After marking a page for pending deletion, the user should be redirected to the page."""
    db.create_page("PDR Page", "pdr-page", "content")
    client.post("/login", data={"username": "editor", "password": "editor123"})

    resp = client.post("/page/pdr-page/delete", follow_redirects=False)
    assert resp.status_code == 302
    assert "/page/pdr-page" in resp.headers["Location"]
