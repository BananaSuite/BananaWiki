"""Every settings page of the wiki has one Save, at the top right in its sticky header.

The owner's rule: no Save at the bottom of a page, none per section and no second one hidden with CSS. Pages
that hold several settings save them with that one button (a section another feature adds joins the page's
form); actions (delete, revoke, upload, change role…) are named for what they do. See tests/save_controls.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.save_controls import assert_one_save_in_header, assert_save_is_the_only_primary, save_controls

from .pages_support import in_app, make_page

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ("announcements", "assessments", "audit", "badges", "canvas", "custom_pages", "deletion_slowdown",
           "mascot", "page_governance", "tts", "user_profiles", "api_service", "temporary_accounts", "chat")


@pytest.fixture
def site(app, db, make_user, login, client):
    """Every plugin with a settings page on, an owner signed in and something to open on each page."""
    placeholders = ", ".join("?" for _ in PLUGINS)
    db.execute(f"UPDATE plugins SET enabled = 1 WHERE id IN ({placeholders})", PLUGINS)
    db.execute("UPDATE site_settings SET contribution_approval_enabled = 1, page_builder_enabled = 1")
    owner = make_user("site_owner", role="owner")
    member = make_user("member_one", role="editor")
    login(client, owner)
    client.post("/admin/roles/create", data={"name": "Reviewers", "description": "", "base_role": "editor",
                                              "permissions": ["page.view_all"]})
    client.post("/admin/badges/create", data={"name": "Helper", "description": "Helps", "icon": "*",
                                              "color": "#123456", "enabled": "1"})
    client.post("/admin/announcements/create", data={"content": "Hello", "color": "blue", "text_size": "normal",
                                                     "visibility": "both", "audience_mode": "all"})
    client.post("/admin/custom-pages/create", data={"path": "/landing", "title": "Landing",
                                                    "content_type": "markdown", "is_published": "1"},
                content_type="multipart/form-data")
    client.post("/admin/profile-fields", data={"action": "create", "label": "Team", "field_type": "text"})
    client.post(f"/admin/users/{member['id']}/tags", data={"action": "add_tag", "tag_label": "Crew",
                                                           "tag_color": "#9b59b6"})
    from bananawiki.wiki.features.canvas import service as canvas

    layout = in_app(app, lambda: canvas.create("Plan", "", owner["id"]))
    page = make_page(app, "Quiz page", "Text", author_id=owner["id"])
    ids = {
        "member": member["id"], "canvas": layout["slug"], "page": page["slug"],
        "role": db.scalar("SELECT id FROM custom_roles WHERE name = 'Reviewers'"),
        "badge": db.scalar("SELECT id FROM badge_types WHERE name = 'Helper'"),
        "announcement": db.scalar("SELECT MAX(id) FROM announcements"),
        "custom_page": db.scalar("SELECT id FROM custom_pages WHERE path = '/landing'"),
    }
    assert all(ids.values()), ids
    return ids


# (address, whether it must have a Save): every page that edits settings, a profile or a configuration.
PAGES = [
    ("/admin/settings", True), ("/admin/appearance", True), ("/admin/interface-languages", True),
    ("/admin/sessions", False), ("/admin/notifications", True), ("/admin/bm-mode", True),
    ("/admin/chats/settings", True),
    ("/admin/governance", True), ("/admin/api-service", False), ("/admin/tts", True), ("/admin/audit", False),
    ("/admin/pending-deletions", True), ("/admin/contributions", True), ("/admin/custom-pages", True),
    ("/admin/access-requests", True),
    ("/admin/custom-pages/create", False), ("/admin/custom-pages/{custom_page}/edit", True),
    ("/admin/assessments", True), ("/admin/plugins", True), ("/admin/profile-fields", True),
    ("/admin/roles/create", False), ("/admin/roles/{role}", True), ("/admin/roles/base/user", True),
    ("/admin/roles/base/editor", True), ("/admin/badges/{badge}/edit", True),
    ("/admin/announcements/{announcement}/edit", True),
    ("/admin/users/{member}", False), ("/admin/users/{member}/permissions", True),
    ("/admin/users/{member}/profile", True),
    ("/admin/users/{member}/profile-fields", True), ("/admin/users/{member}/reservation-quota", True),
    ("/admin/temporary", False), ("/admin/badges", False), ("/admin/announcements", False), ("/admin/roles", False),
    ("/settings", False), ("/settings/security", False), ("/settings/notifications", True),
    ("/settings/privacy", False), ("/settings/display", True), ("/settings/profile", True),
    ("/settings/profile-fields", True), ("/settings/reservation-quota", True),
    ("/canvas/{canvas}/settings", True), ("/page/{page}/assessment/manage", True), ("/page/{page}/edit", True),
]


@pytest.mark.parametrize(("path", "has_save"), PAGES, ids=[path for path, _ in PAGES])
def test_a_settings_page_has_one_save_in_its_header(site, client, path, has_save):
    address = path.format(**site)
    response = client.get(address)
    assert response.status_code == 200, address
    page = response.get_data(as_text=True)
    assert_one_save_in_header(page, where=address)
    assert len(save_controls(page)) == (1 if has_save else 0), (address, save_controls(page))
    assert_save_is_the_only_primary(page, where=address)


def test_bm_modes_short_account_tab_saves_the_notifications_from_its_header(site, client):
    """BM mode's short settings show the notification settings on Account: its header saves them."""
    assert client.post("/settings/bm-mode", data={"bm_mode": "1"}).status_code == 302
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="notification-prefs-form"' in page
    assert_one_save_in_header(page, where="/settings (short)")
    assert 'form="notification-prefs-form"' in page


def test_no_stylesheet_hides_a_second_save():
    for path in ("bananawiki/core/static/css/bananawiki.css", "bananawiki/hosting/static/css/hosting.css"):
        css = (ROOT / path).read_text(encoding="utf-8")
        assert ".form-save" not in css, path
        assert "form-actions--sticky" not in css, path
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if "display: none" in body or "display:none" in body:
                assert not re.search(r"save|form-actions", selector, re.I), (path, selector.strip())
    templates = (ROOT / "bananawiki").rglob("*.html")
    assert not [str(path) for path in templates if "form-save" in path.read_text(encoding="utf-8")]
