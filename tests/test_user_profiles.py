"""
Tests for user profile (user pages) feature.

Covers:
  - Viewing a user profile page
  - Editing profile (real name, bio)
  - Publishing and unpublishing a profile page
  - Deleting a profile page (contributions are retained)
  - Hiding a profile from non-owners/non-admins (404)
  - Avatar upload validation (size limit, bad file type)
  - Removing an avatar
  - People directory (/users) for admin vs regular user
  - People directory search
  - Admin moderation: edit profile, remove avatar, disable/enable, delete
  - Sidebar People widget (published profiles only)
  - Contribution heatmap data on profile
  - Role badges shown on profile page
"""

import io
import os
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

MAX_AVATAR_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB


class _ContributionLinkParser(HTMLParser):
    """Collect contribution-list page links from a rendered profile page."""

    def __init__(self):
        super().__init__()
        self._in_contribution_link = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        if attrs.get("class") == "contribution-page":
            self._in_contribution_link = True

    def handle_data(self, data):
        if self._in_contribution_link:
            self.links.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_contribution_link = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    # Point uploads at a temp directory so file-system operations work
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", upload_dir)
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_uid():
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def regular_uid(admin_uid):
    """A regular (non-admin) user."""
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("alice", generate_password_hash("alice123"), role="user")


@pytest.fixture
def admin_client(client, admin_uid):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def alice_client(client, regular_uid):
    client.post("/login", data={"username": "alice", "password": "alice123"})
    return client


# ---------------------------------------------------------------------------
# Profile page: basic view
# ---------------------------------------------------------------------------

def test_own_profile_visible_without_published(admin_client):
    """User can always view their own profile even if unpublished."""
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"admin" in resp.data


def test_profile_404_for_unknown_username(admin_client):
    resp = admin_client.get("/users/nobody_here")
    assert resp.status_code == 404


def test_unpublished_profile_hidden_from_others(alice_client, admin_uid):
    """Another user cannot view an unpublished profile: should get 404."""
    resp = alice_client.get("/users/admin")
    assert resp.status_code == 404


def test_admin_can_view_unpublished_profile(admin_client, regular_uid):
    """Admins can view profiles even if not published."""
    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Edit profile (real name & bio)
# ---------------------------------------------------------------------------

def test_update_profile_real_name_and_bio(admin_client):
    resp = admin_client.post(
        "/settings",
        data={
            "action": "update_profile",
            "real_name": "Admin User",
            "bio": "I run this wiki.",
            "next_url": "/users/admin",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Admin User" in resp.data
    assert b"I run this wiki." in resp.data


def test_profile_bio_max_length_enforced(admin_client):
    """Bio is stored truncated to 500 chars: page renders without error."""
    long_bio = "x" * 600
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "", "bio": long_bio,
              "next_url": "/users/admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    import db
    import config as cfg
    # Reload DB from the test's temp path (already patched via monkeypatch)
    profile = db.get_user_profile(
        db.get_user_by_username("admin")["id"]
    )
    assert len(profile["bio"]) <= 500


def _site_today_iso():
    from helpers import get_site_timezone
    return datetime.now(get_site_timezone()).date().isoformat()


def test_update_profile_birth_date_enables_owner_birthday_celebration(admin_client, admin_uid):
    """A user who sets today's birth date gets birthday celebration mode."""
    import db

    today = _site_today_iso()
    resp = admin_client.post(
        "/settings",
        data={
            "action": "update_profile",
            "real_name": "Admin User",
            "bio": "",
            "birth_date": today,
            "next_url": "/users/admin",
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    profile = db.get_user_profile(admin_uid)
    assert profile["birth_date"] == today
    assert b"Happy birthday, admin!" in resp.data
    assert b"birthday-mode-banner" in resp.data
    assert b"profile-birthday-card" in resp.data


def test_published_profile_shows_birthday_celebration_to_visitors(alice_client, admin_uid):
    """Visitors see the profile birthday celebration when the owner has a birthday today."""
    import db

    today = _site_today_iso()
    db.upsert_user_profile(admin_uid, birth_date=today, page_published=True)

    resp = alice_client.get("/users/admin")

    assert resp.status_code == 200
    assert b"Happy birthday, admin!" in resp.data
    assert b"profile-birthday-card" in resp.data


def test_update_profile_rejects_future_birth_date(admin_client, admin_uid):
    """Birth dates must be real past-or-present dates."""
    import db
    from helpers import get_site_timezone

    future = (datetime.now(get_site_timezone()).date() + timedelta(days=1)).isoformat()
    resp = admin_client.post(
        "/settings",
        data={
            "action": "update_profile",
            "real_name": "",
            "bio": "",
            "birth_date": future,
            "next_url": "/users/admin",
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"Birth date cannot be in the future." in resp.data
    profile = db.get_user_profile(admin_uid)
    assert profile is None or profile["birth_date"] == ""


# ---------------------------------------------------------------------------
# Publish / unpublish / delete profile
# ---------------------------------------------------------------------------

def test_publish_profile(admin_client):
    # First create the profile record
    admin_client.post("/settings", data={"action": "update_profile",
                                        "real_name": "", "bio": "",
                                        "next_url": "/"})
    resp = admin_client.post(
        "/settings",
        data={"action": "publish_profile", "next_url": "/users/admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"now public" in resp.data or b"public" in resp.data.lower()


def test_unpublish_profile(admin_client):
    # Create + publish first
    admin_client.post("/settings", data={"action": "update_profile",
                                        "real_name": "", "bio": "", "next_url": "/"})
    admin_client.post("/settings", data={"action": "publish_profile", "next_url": "/"})
    resp = admin_client.post(
        "/settings",
        data={"action": "unpublish_profile", "next_url": "/users/admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"hidden" in resp.data.lower() or b"Hidden" in resp.data


def test_delete_profile_retains_contributions(admin_client, admin_uid):
    """Deleting a profile page does NOT delete page_history contributions."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "New content", admin_uid, "test edit")
    # Set up and delete profile
    admin_client.post("/settings", data={"action": "update_profile",
                                        "real_name": "A", "bio": "B", "next_url": "/"})
    admin_client.post("/settings", data={"action": "delete_profile", "next_url": "/"})
    # Contributions still exist in DB
    _, contribs = db.get_contributions_by_day(admin_uid)
    total = sum(contribs.values())
    assert total >= 1


def test_deleted_profile_not_in_published_list(admin_client, admin_uid):
    import db
    admin_client.post("/settings", data={"action": "update_profile",
                                        "real_name": "", "bio": "", "next_url": "/"})
    admin_client.post("/settings", data={"action": "publish_profile", "next_url": "/"})
    admin_client.post("/settings", data={"action": "delete_profile", "next_url": "/"})
    profiles = db.list_published_profiles()
    assert not any(p["username"] == "admin" for p in profiles)


# ---------------------------------------------------------------------------
# Avatar upload validation
# ---------------------------------------------------------------------------

def test_avatar_upload_invalid_type_rejected(admin_client):
    data = {
        "action": "update_profile",
        "real_name": "",
        "bio": "",
        "next_url": "/",
        "avatar": (io.BytesIO(b"<svg/>"), "avatar.svg"),
    }
    resp = admin_client.post("/settings", data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid avatar" in resp.data or b"invalid" in resp.data.lower()


def test_avatar_upload_too_large_rejected(admin_client):
    big_bytes = b"\x00" * (MAX_AVATAR_SIZE_BYTES + 1)  # just over 1 MB
    data = {
        "action": "update_profile",
        "real_name": "",
        "bio": "",
        "next_url": "/",
        "avatar": (io.BytesIO(big_bytes), "photo.jpg"),
    }
    resp = admin_client.post("/settings", data=data,
                             content_type="multipart/form-data",
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b"1 MB" in resp.data or b"smaller" in resp.data.lower()


def test_remove_avatar(admin_client, tmp_path, monkeypatch):
    """remove_avatar action clears the avatar_filename in the DB."""
    import db
    uid = db.get_user_by_username("admin")["id"]
    # Directly inject a fake avatar record
    db.upsert_user_profile(uid, avatar_filename="avatars/fake.png")
    # Create the fake file so os.remove doesn't crash
    fake_path = os.path.join(config.UPLOAD_FOLDER, "avatars", "fake.png")
    os.makedirs(os.path.dirname(fake_path), exist_ok=True)
    open(fake_path, "w").close()

    resp = admin_client.post("/settings",
                             data={"action": "remove_avatar", "next_url": "/"},
                             follow_redirects=True)
    assert resp.status_code == 200
    profile = db.get_user_profile(uid)
    assert profile["avatar_filename"] == ""


# ---------------------------------------------------------------------------
# People directory (/users)
# ---------------------------------------------------------------------------

def test_users_list_admin_sees_all(admin_client, regular_uid):
    """Admin sees all users in /users regardless of publish status."""
    resp = admin_client.get("/users")
    assert resp.status_code == 200
    assert b"alice" in resp.data


def test_users_list_regular_sees_only_published(alice_client, admin_uid):
    """Regular user only sees published profiles; admin has no published profile so is absent."""
    resp = alice_client.get("/users")
    assert resp.status_code == 200
    # admin has no published profile yet → must not appear in the users list for alice
    assert b'<span class="user-list-username">admin</span>' not in resp.data


def test_users_list_search(admin_client, regular_uid):
    resp = admin_client.get("/users?q=ali")
    assert resp.status_code == 200
    assert b"alice" in resp.data


def test_users_list_search_no_results(admin_client):
    resp = admin_client.get("/users?q=zzz_no_match")
    assert resp.status_code == 200
    assert b"No members found" in resp.data


def test_users_list_search_null_real_name(admin_client, regular_uid):
    """Search must not crash when a user's real_name is NULL (no profile)."""
    resp = admin_client.get("/users?q=ali")
    assert resp.status_code == 200
    assert b"alice" in resp.data


# ---------------------------------------------------------------------------
# Published profile visible to others
# ---------------------------------------------------------------------------

def test_published_profile_visible_to_others(alice_client, admin_uid):
    import db
    db.upsert_user_profile(admin_uid, real_name="Admin", bio="Hello",
                           page_published=True)
    resp = alice_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"Admin" in resp.data


# ---------------------------------------------------------------------------
# Role badge on profile page
# ---------------------------------------------------------------------------

def test_admin_badge_shown_on_profile(admin_client):
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"Administrator" in resp.data or b"admin" in resp.data.lower()


def test_user_role_badge_shown(alice_client, regular_uid):
    import db
    db.upsert_user_profile(regular_uid, real_name="", bio="",
                           page_published=True)
    resp = alice_client.get("/users/alice")
    assert resp.status_code == 200
    # Role badge for "user" role should be present
    assert b"alice" in resp.data


# ---------------------------------------------------------------------------
# Sidebar People widget
# ---------------------------------------------------------------------------

def test_sidebar_people_appears_when_profiles_published(admin_client, admin_uid):
    import db
    db.upsert_user_profile(admin_uid, real_name="", bio="", page_published=True)
    resp = admin_client.get("/")
    assert resp.status_code == 200
    assert b"People" in resp.data


def test_sidebar_people_shows_all_users_when_none_published(admin_client):
    resp = admin_client.get("/")
    assert resp.status_code == 200
    # All non-suspended users shown in sidebar grid (not just published profiles)
    assert b"sidebar-people-grid" in resp.data
    assert b"sidebar-people-empty" not in resp.data


# ---------------------------------------------------------------------------
# Contribution heatmap data
# ---------------------------------------------------------------------------

def test_contribution_heatmap_present_on_profile(admin_client, admin_uid):
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"contributions in" in resp.data
    assert b"contrib-graph" in resp.data


def test_contribution_counts_after_edit(admin_client, admin_uid):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Edited content", admin_uid, "my edit")
    _, contribs = db.get_contributions_by_day(admin_uid)
    assert sum(contribs.values()) >= 1


def test_profile_contribution_chart_setting_default_enabled(admin_uid):
    import db
    settings = db.get_site_settings()
    assert settings["profile_contribution_chart_enabled"] == 1


def test_admin_can_disable_profile_contribution_chart(logged_in_admin):
    import db
    resp = logged_in_admin.post("/global-settings", data={
        "site_name": "Test",
        "primary_color": "#8fa0d4",
        "secondary_color": "#1e1e2c",
        "accent_color": "#7e9ada",
        "text_color": "#c8ccd8",
        "sidebar_color": "#1a1a24",
        "bg_color": "#16161f",
        "light_primary_color": "#4b63b6",
        "light_secondary_color": "#ffffff",
        "light_accent_color": "#3553c7",
        "light_text_color": "#202534",
        "light_sidebar_color": "#e9edf5",
        "light_bg_color": "#f6f7fb",
        "default_theme_mode": "dark",
        "timezone": "UTC",
        # profile_contribution_chart_enabled omitted -> disabled
    }, follow_redirects=True)
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert settings["profile_contribution_chart_enabled"] == 0


def test_profile_hides_contribution_chart_when_disabled(admin_client):
    import db
    db.update_site_settings(profile_contribution_chart_enabled=0)
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b'id="contrib-graph"' not in resp.data
    assert b"Recent Contributions" in resp.data


def test_admin_settings_shows_profile_contribution_chart_toggle(logged_in_admin):
    resp = logged_in_admin.get("/global-settings")
    assert resp.status_code == 200
    assert b"profile_contribution_chart_enabled" in resp.data


def test_profile_can_render_selected_past_contribution_year(admin_client, admin_uid):
    import db
    selected_year = datetime.now(timezone.utc).year - 1
    home = db.get_home_page()
    with db.get_db_context() as conn:
        conn.execute(
            "INSERT INTO page_history (page_id, title, content, edited_by, edit_message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                home["id"],
                home["title"],
                home["content"],
                admin_uid,
                "historical contribution",
                f"{selected_year}-06-15T10:00:00+00:00",
            ),
        )
        conn.commit()
    resp = admin_client.get(f"/users/admin?year={selected_year}")
    assert resp.status_code == 200
    assert f'data-year="{selected_year}"'.encode() in resp.data


def test_profile_day_details_links_use_page_route(admin_client, admin_uid):
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "Edited content", admin_uid, "my edit")
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"link.href = '/page/' + encodeURIComponent(item.page_slug);" in resp.data


def test_public_profile_hides_inaccessible_contributions(alice_client, admin_uid, regular_uid):
    """Published profiles only show contributions for pages the viewer can currently access."""
    import db
    from helpers._permissions import get_default_permissions

    visible_cat = db.create_category("Visible Cat")
    hidden_cat = db.create_category("Hidden Cat")
    db.create_page(
        "Visible Contribution Page",
        "visible-contribution-page",
        "Content",
        category_id=visible_cat,
        user_id=admin_uid,
    )
    db.create_page(
        "Hidden Contribution Page",
        "hidden-contribution-page",
        "Content",
        category_id=hidden_cat,
        user_id=admin_uid,
    )
    deindexed_page_id = db.create_page(
        "Deindexed Contribution Page",
        "deindexed-contribution-page",
        "Content",
        category_id=visible_cat,
        user_id=admin_uid,
    )
    db.set_page_deindexed(deindexed_page_id, True)
    db.upsert_user_profile(admin_uid, page_published=True)
    db.set_user_permissions(
        regular_uid,
        get_default_permissions("user"),
        read_restricted=True,
        read_category_ids=[visible_cat],
    )
    admin = db.get_user_by_id(admin_uid)

    resp = alice_client.get(f"/users/{admin['username']}")
    parser = _ContributionLinkParser()
    parser.feed(resp.get_data(as_text=True))
    assert resp.status_code == 200
    assert parser.links == ["Visible Contribution Page"]
    assert b"1 contribution in" in resp.data


# ---------------------------------------------------------------------------
# Admin moderation tools
# ---------------------------------------------------------------------------

def test_admin_can_edit_other_user_profile(admin_client, regular_uid):
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "edit_profile", "real_name": "Alice R.", "bio": "Edited by admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    import db
    profile = db.get_user_profile(regular_uid)
    assert profile["real_name"] == "Alice R."
    assert profile["bio"] == "Edited by admin"


def test_admin_can_disable_user_profile(admin_client, regular_uid):
    import db
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "disable_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["page_disabled_by_admin"] == 1
    assert profile["page_published"] == 0


def test_admin_can_reenable_user_profile(admin_client, regular_uid):
    import db
    db.upsert_user_profile(regular_uid, page_disabled_by_admin=True)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "enable_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["page_disabled_by_admin"] == 0


def test_admin_can_delete_user_profile(admin_client, regular_uid):
    import db
    db.upsert_user_profile(regular_uid, real_name="Alice", bio="Bio")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "delete_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile is None


def test_admin_remove_avatar_for_user(admin_client, regular_uid):
    import db
    db.upsert_user_profile(regular_uid, avatar_filename="avatars/alice.png")
    fake_path = os.path.join(config.UPLOAD_FOLDER, "avatars", "alice.png")
    os.makedirs(os.path.dirname(fake_path), exist_ok=True)
    open(fake_path, "w").close()

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "remove_avatar"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["avatar_filename"] == ""


def test_disabled_profile_cannot_be_published_by_user(alice_client, regular_uid):
    """A user whose profile was disabled by admin cannot re-publish it."""
    import db
    db.upsert_user_profile(regular_uid, page_disabled_by_admin=True)
    resp = alice_client.post(
        "/settings",
        data={"action": "publish_profile", "next_url": "/"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["page_published"] == 0


def test_admin_moderation_requires_admin(alice_client, admin_uid):
    """Non-admin cannot access admin moderation endpoint: redirected to home."""
    import db
    resp = alice_client.post(
        f"/admin/users/{admin_uid}/profile",
        data={"action": "edit_profile", "real_name": "Hacked", "bio": ""},
    )
    # admin_required redirects non-admins to home
    assert resp.status_code == 302
    # Verify the DB was not modified
    profile = db.get_user_profile(admin_uid)
    assert profile is None or profile["real_name"] != "Hacked"


def test_admin_can_publish_user_profile(admin_client, regular_uid):
    """Admin can publish another user's profile page."""
    import db
    db.upsert_user_profile(regular_uid, page_published=False)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "publish_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["page_published"] == 1


def test_admin_can_unpublish_user_profile(admin_client, regular_uid):
    """Admin can unpublish another user's profile page."""
    import db
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "unpublish_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["page_published"] == 0


def test_admin_moderate_profile_redirects_to_user_profile(admin_client, regular_uid):
    """Admin profile moderation actions redirect back to the user's profile page."""
    import db
    db.upsert_user_profile(regular_uid, real_name="Alice", bio="Bio")
    regular = db.get_user_by_id(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "edit_profile", "real_name": "Alice R.", "bio": "Updated"},
    )
    assert resp.status_code == 302
    assert f"/users/{regular['username']}" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Protected admin profile protection
# ---------------------------------------------------------------------------

@pytest.fixture
def second_admin_uid(admin_uid):
    """A second admin used to test cross-admin restrictions."""
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("admin2", generate_password_hash("admin2pass"), role="admin")


@pytest.fixture
def second_admin_client(client, second_admin_uid):
    client.post("/login", data={"username": "admin2", "password": "admin2pass"})
    return client


@pytest.fixture
def owner_uid(admin_uid):
    """An admin with the owner role."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("padmin", generate_password_hash("padmin123"), role="owner")
    db.upsert_user_profile(uid, real_name="Protected Admin", bio="Original bio")
    return uid


def test_admin_cannot_edit_owner_profile(second_admin_client, owner_uid):
    """An admin cannot edit a owner's profile page."""
    import db
    resp = second_admin_client.post(
        f"/admin/users/{owner_uid}/profile",
        data={"action": "edit_profile", "real_name": "Hacked", "bio": "Hacked bio"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(owner_uid)
    assert profile["real_name"] != "Hacked"


def test_admin_cannot_disable_owner_profile(second_admin_client, owner_uid):
    """An admin cannot disable a owner's profile page."""
    import db
    db.upsert_user_profile(owner_uid, page_published=True)
    resp = second_admin_client.post(
        f"/admin/users/{owner_uid}/profile",
        data={"action": "disable_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(owner_uid)
    assert profile["page_disabled_by_admin"] == 0


def test_admin_cannot_delete_owner_profile(second_admin_client, owner_uid):
    """An admin cannot delete a owner's profile page."""
    import db
    resp = second_admin_client.post(
        f"/admin/users/{owner_uid}/profile",
        data={"action": "delete_profile"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(owner_uid)
    assert profile is not None


def test_admin_cannot_remove_avatar_of_owner(second_admin_client, owner_uid):
    """An admin cannot remove the avatar of a owner."""
    import db
    db.upsert_user_profile(owner_uid, avatar_filename="avatars/padmin.png")
    fake_path = os.path.join(config.UPLOAD_FOLDER, "avatars", "padmin.png")
    os.makedirs(os.path.dirname(fake_path), exist_ok=True)
    open(fake_path, "w").close()

    resp = second_admin_client.post(
        f"/admin/users/{owner_uid}/profile",
        data={"action": "remove_avatar"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(owner_uid)
    assert profile["avatar_filename"] == "avatars/padmin.png"


def test_owner_can_edit_own_profile_via_admin(client, owner_uid):
    """A owner can moderate their own profile via the admin endpoint."""
    import db
    client.post("/login", data={"username": "padmin", "password": "padmin123"})
    resp = client.post(
        f"/admin/users/{owner_uid}/profile",
        data={"action": "edit_profile", "real_name": "Self Edit", "bio": "My bio"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(owner_uid)
    assert profile["real_name"] == "Self Edit"


# ---------------------------------------------------------------------------
# Member since badge on profile
# ---------------------------------------------------------------------------

def test_member_since_badge_on_profile(admin_client):
    """The 'Member since' purple badge should appear on the profile page."""
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"Member since" in resp.data
    assert b"badge-member-since" in resp.data


# ---------------------------------------------------------------------------
# Role history tracking
# ---------------------------------------------------------------------------

def test_role_change_recorded_on_admin_change(admin_client, regular_uid):
    """Changing a user's role via admin records it in role_history."""
    import db
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/edit",
        data={"action": "change_role", "role": "editor"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    history = db.get_role_history(regular_uid)
    assert len(history) >= 1
    assert history[0]["old_role"] == "user"
    assert history[0]["new_role"] == "editor"


def test_role_history_shown_to_admin(admin_client, regular_uid):
    """Admin can see role history on a user's profile page."""
    import db
    db.record_role_change(regular_uid, "user", "editor", changed_by=None)
    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"Role History" in resp.data


def test_role_history_shown_to_own_user(alice_client, regular_uid):
    """A user can see their own role history on their profile page."""
    import db
    db.record_role_change(regular_uid, "user", "editor", changed_by=None)
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = alice_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"xs-f1278614" in resp.data


def test_role_history_not_shown_when_empty(admin_client):
    """No role history section when there are no role changes."""
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200
    assert b"xs-f1278614" not in resp.data


# ---------------------------------------------------------------------------
# Custom user tags
# ---------------------------------------------------------------------------

def test_admin_add_custom_tag(admin_client, regular_uid):
    """Admin can add a custom tag to a user."""
    import db
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "add_tag", "tag_label": "Founder", "tag_color": "#9b59b6"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(regular_uid)
    assert len(tags) == 1
    assert tags[0]["label"] == "Founder"
    assert tags[0]["color"] == "#9b59b6"


def test_admin_update_custom_tag(admin_client, regular_uid):
    """Admin can update a custom tag."""
    import db
    tag_id = db.add_user_custom_tag(regular_uid, "Old Label", "#e74c3c")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "update_tag", "tag_id": tag_id,
              "tag_label": "New Label", "tag_color": "#3498db"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tag = db.get_user_custom_tag(tag_id)
    assert tag["label"] == "New Label"
    assert tag["color"] == "#3498db"


def test_admin_delete_custom_tag(admin_client, regular_uid):
    """Admin can delete a custom tag."""
    import db
    tag_id = db.add_user_custom_tag(regular_uid, "Temp", "#e74c3c")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "delete_tag", "tag_id": tag_id},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_custom_tag(tag_id) is None


def test_admin_reorder_custom_tags(admin_client, regular_uid):
    """Admin can reorder custom tags."""
    import db
    t1 = db.add_user_custom_tag(regular_uid, "Tag1", "#e74c3c")
    t2 = db.add_user_custom_tag(regular_uid, "Tag2", "#3498db")
    t3 = db.add_user_custom_tag(regular_uid, "Tag3", "#2ecc71")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "reorder_tags", "tag_order": f"{t3},{t1},{t2}"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(regular_uid)
    assert [t["id"] for t in tags] == [t3, t1, t2]


def test_custom_tags_shown_on_profile(admin_client, regular_uid):
    """Custom tags are visible on the user's profile page."""
    import db
    db.add_user_custom_tag(regular_uid, "Founder", "#9b59b6")
    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"Founder" in resp.data


def test_non_admin_cannot_manage_tags(alice_client, admin_uid):
    """Non-admin cannot add tags via the admin endpoint."""
    resp = alice_client.post(
        f"/admin/users/{admin_uid}/tags",
        data={"action": "add_tag", "tag_label": "Hacked", "tag_color": "#ff0000"},
    )
    # admin_required redirects non-admins
    assert resp.status_code == 302


def test_add_tag_empty_label_rejected(admin_client, regular_uid):
    """Empty tag label is rejected."""
    import db
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "add_tag", "tag_label": "", "tag_color": "#9b59b6"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(regular_uid)
    assert len(tags) == 0


def test_add_tag_invalid_color_rejected(admin_client, regular_uid):
    """Invalid hex color is rejected."""
    import db
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "add_tag", "tag_label": "Test", "tag_color": "not-a-color"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(regular_uid)
    assert len(tags) == 0


def test_cannot_manage_tags_on_owner(second_admin_client, owner_uid):
    """Admin cannot add tags to a owner's profile."""
    import db
    resp = second_admin_client.post(
        f"/admin/users/{owner_uid}/tags",
        data={"action": "add_tag", "tag_label": "Hacked", "tag_color": "#ff0000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(owner_uid)
    assert len(tags) == 0


def test_manage_custom_tags_section_visible_to_admin(admin_client, regular_uid):
    """Admin sees the 'Manage Custom Tags' section on user profile."""
    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"Manage Custom Tags" in resp.data


def test_manage_custom_tags_section_hidden_from_non_admin(alice_client, regular_uid):
    """Non-admin does not see the 'Manage Custom Tags' section."""
    import db
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = alice_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"custom-tag-fields" not in resp.data


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_profile_with_no_created_at_does_not_crash(admin_client, admin_uid):
    """Profile page renders even if created_at is missing or empty."""
    import db
    # Directly update to empty created_at to simulate edge case
    conn = db.get_db()
    conn.execute("UPDATE users SET created_at='' WHERE id=?", (admin_uid,))
    conn.commit()
    conn.close()
    resp = admin_client.get("/users/admin")
    assert resp.status_code == 200


def test_delete_tag_wrong_id(admin_client, regular_uid):
    """Deleting a nonexistent tag does not crash."""
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "delete_tag", "tag_id": "99999"},
        follow_redirects=True,
    )
    assert resp.status_code == 200


def test_update_tag_wrong_user(admin_client, regular_uid, admin_uid):
    """Cannot update a tag belonging to another user via wrong user_id param."""
    import db
    tag_id = db.add_user_custom_tag(admin_uid, "Admin Tag", "#e74c3c")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "update_tag", "tag_id": tag_id,
              "tag_label": "Hacked", "tag_color": "#ff0000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tag = db.get_user_custom_tag(tag_id)
    assert tag["label"] == "Admin Tag"  # Unchanged


def test_reorder_tags_invalid_ids_rejected(admin_client, regular_uid, admin_uid):
    """Reordering with tag IDs from another user is rejected."""
    import db
    t1 = db.add_user_custom_tag(regular_uid, "Tag1", "#e74c3c")
    foreign_tag = db.add_user_custom_tag(admin_uid, "Foreign", "#3498db")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "reorder_tags", "tag_order": f"{t1},{foreign_tag}"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid tag IDs" in resp.data


def test_reorder_tags_valid_ids(admin_client, regular_uid):
    """Reordering with valid tag IDs succeeds."""
    import db
    t1 = db.add_user_custom_tag(regular_uid, "A", "#e74c3c")
    t2 = db.add_user_custom_tag(regular_uid, "B", "#3498db")
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "reorder_tags", "tag_order": f"{t2},{t1}"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tags = db.get_user_custom_tags(regular_uid)
    assert [t["id"] for t in tags] == [t2, t1]


def test_tag_id_none_does_not_crash(admin_client, regular_uid):
    """Submitting update/delete without a tag_id doesn't crash."""
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "update_tag", "tag_label": "X", "tag_color": "#aabbcc"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/tags",
        data={"action": "delete_tag"},
        follow_redirects=True,
    )
    assert resp.status_code == 200


def test_role_change_recorded_on_owner_toggle(client, admin_uid):
    """Toggling protected admin records role change in history."""
    import db
    db.update_user(admin_uid, is_superuser=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(
        "/settings",
        data={"action": "toggle_owner", "password": "admin123"},
        follow_redirects=True,
    )
    history = db.get_role_history(admin_uid)
    assert len(history) >= 1
    assert history[0]["old_role"] == "admin"
    assert history[0]["new_role"] == "owner"


# ---------------------------------------------------------------------------
# Admin attribution management
# ---------------------------------------------------------------------------

@pytest.fixture
def editor_uid(admin_uid):
    """An editor user for attribution tests."""
    from werkzeug.security import generate_password_hash
    import db
    return db.create_user("editor1", generate_password_hash("editor123"), role="editor")


@pytest.fixture
def editor_client(client, editor_uid):
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    return client


def _make_contribution(user_id):
    """Helper: create a page edit attributed to user_id, return entry_id."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", f"edit by {user_id}", user_id, "test edit")
    history = db.get_page_history(home["id"])
    return history[0]["id"]


def test_admin_deattribute_single_contribution(admin_client, regular_uid):
    """Admin can deattribute a single contribution from a user profile."""
    import db
    entry_id = _make_contribution(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "deattribute_contribution", "entry_id": entry_id},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Contribution deattributed" in resp.data
    entry = db.get_history_entry(entry_id)
    assert entry["edited_by"] is None
    assert entry["username"] == "[removed]"


def test_admin_deattribute_all_contributions(admin_client, regular_uid):
    """Admin can deattribute all contributions for a user."""
    import db
    _make_contribution(regular_uid)
    _make_contribution(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "deattribute_all"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Deattributed 2 contribution(s)" in resp.data
    # Contributions should be empty for this user now
    contribs = db.get_user_contributions(regular_uid)
    assert len(contribs) == 0


def test_admin_mass_reattribute_contributions(admin_client, regular_uid, editor_uid):
    """Admin can mass reattribute all contributions from one user to another."""
    import db
    _make_contribution(regular_uid)
    _make_contribution(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "mass_reattribute", "to_user_id": editor_uid},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Reattributed 2 contribution(s)" in resp.data
    # Regular user has no contributions, editor has them
    assert len(db.get_user_contributions(regular_uid)) == 0
    assert len(db.get_user_contributions(editor_uid)) == 2


def test_mass_reattribute_to_same_user_rejected(admin_client, regular_uid):
    """Cannot reattribute contributions to the same user."""
    _make_contribution(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "mass_reattribute", "to_user_id": regular_uid},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Cannot reattribute to the same user" in resp.data


def test_mass_reattribute_invalid_target_rejected(admin_client, regular_uid):
    """Mass reattribute with invalid target user shows error."""
    _make_contribution(regular_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "mass_reattribute", "to_user_id": "nonexistent-id"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid target user" in resp.data


def test_admin_delete_role_history_entry(admin_client, regular_uid, admin_uid):
    """Admin can delete a single role history entry."""
    import db
    db.record_role_change(regular_uid, "user", "editor", changed_by=admin_uid)
    history = db.get_role_history(regular_uid)
    entry_id = history[0]["id"]
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "delete_role_history_entry", "entry_id": entry_id},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Role history entry deleted" in resp.data
    assert len(db.get_role_history(regular_uid)) == 0


def test_admin_delete_all_role_history(admin_client, regular_uid, admin_uid):
    """Admin can delete all role history entries for a user."""
    import db
    db.record_role_change(regular_uid, "user", "editor", changed_by=admin_uid)
    db.record_role_change(regular_uid, "editor", "admin", changed_by=admin_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "delete_all_role_history"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Deleted 2 role history entries" in resp.data
    assert len(db.get_role_history(regular_uid)) == 0


def test_delete_role_history_wrong_user_rejected(admin_client, regular_uid, admin_uid):
    """Cannot delete a role history entry belonging to another user."""
    import db
    db.record_role_change(admin_uid, "admin", "owner", changed_by=admin_uid)
    history = db.get_role_history(admin_uid)
    entry_id = history[0]["id"]
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "delete_role_history_entry", "entry_id": entry_id},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Role history entry not found" in resp.data


def test_non_admin_cannot_manage_attributions(alice_client, regular_uid):
    """Non-admin users cannot access the attributions endpoint."""
    resp = alice_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "deattribute_all"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Admin access required" in resp.data
    assert b'class="flash-message">Deattributed' not in resp.data


def test_attributions_owner_restriction(admin_client, admin_uid):
    """Cannot manage attributions on a protected admin (unless own account)."""
    import db
    # Make admin a owner
    db.update_user(admin_uid, role="owner")
    # Create another admin to try
    from werkzeug.security import generate_password_hash
    db.create_user("otheradmin", generate_password_hash("otheradmin123"), role="admin")
    # Login as other admin
    admin_client.get("/logout")
    admin_client.post("/login", data={"username": "otheradmin", "password": "otheradmin123"})
    resp = admin_client.post(
        f"/admin/users/{admin_uid}/attributions",
        data={"action": "deattribute_all"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Protected admin" in resp.data


def test_deattribute_invalid_entry_id(admin_client, regular_uid):
    """Deattributing with an invalid entry_id shows error."""
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "deattribute_contribution"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid entry" in resp.data


def test_deattribute_entry_wrong_user(admin_client, regular_uid, editor_uid):
    """Deattributing an entry belonging to another user is rejected."""
    entry_id = _make_contribution(editor_uid)
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/attributions",
        data={"action": "deattribute_contribution", "entry_id": entry_id},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"does not belong to this user" in resp.data


def test_page_history_deattribute_button(admin_client, regular_uid):
    """Page history shows deattribute button for admins."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "edit", regular_uid, "msg")
    resp = admin_client.get(f"/page/{home['slug']}/history")
    assert resp.status_code == 200
    assert b"Deattribute this entry" in resp.data


def test_page_history_deattribute_entry(admin_client, regular_uid):
    """Admin can deattribute an entry from the page history page."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "some edit", regular_uid, "test msg")
    history = db.get_page_history(home["id"])
    entry_id = history[0]["id"]
    resp = admin_client.post(
        f"/page/{home['slug']}/history/{entry_id}/deattribute",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Attribution removed" in resp.data
    entry = db.get_history_entry(entry_id)
    assert entry["edited_by"] is None


def test_deattributed_entry_shows_removed_in_history(admin_client, regular_uid):
    """A deattributed entry shows [removed] as editor in page history."""
    import db
    home = db.get_home_page()
    db.update_page(home["id"], "Home", "some content", regular_uid, "test")
    history = db.get_page_history(home["id"])
    entry_id = history[0]["id"]
    db.deattribute_contribution(entry_id)
    resp = admin_client.get(f"/page/{home['slug']}/history")
    assert resp.status_code == 200
    assert b"[removed]" in resp.data


def test_profile_shows_deattribute_buttons_to_admin(admin_client, regular_uid):
    """Admin sees deattribute buttons on another user's profile."""
    import db
    _make_contribution(regular_uid)
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"deattribute_contribution" in resp.data
    assert b"Deattribute All Contributions" in resp.data
    assert b"Mass Reattribute" in resp.data


def test_non_admin_no_deattribute_buttons(alice_client, regular_uid):
    """Non-admin users don't see deattribute buttons."""
    import db
    _make_contribution(regular_uid)
    db.upsert_user_profile(regular_uid, page_published=True)
    resp = alice_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"deattribute_contribution" not in resp.data
    assert b'value="deattribute_all"' not in resp.data


# ---------------------------------------------------------------------------
# Security: _profile_next open-redirect protection
# ---------------------------------------------------------------------------

def test_profile_next_accepts_valid_same_site_path(admin_client):
    """A safe same-site next_url is honoured after update_profile."""
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "Admin", "bio": "",
              "next_url": "/users/admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/users/admin")


def test_profile_next_rejects_external_url(admin_client):
    """An external URL in next_url falls back to account settings."""
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "Admin", "bio": "",
              "next_url": "http://evil.example.com/steal"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.example.com" not in location


def test_profile_next_rejects_protocol_relative_url(admin_client):
    """A protocol-relative URL (//...) in next_url falls back to account settings."""
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "Admin", "bio": "",
              "next_url": "//evil.example.com/"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.example.com" not in location


def test_profile_next_rejects_backslash_url(admin_client):
    """A path with a backslash in next_url falls back to account settings."""
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "Admin", "bio": "",
              "next_url": "/\\evil.example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.example.com" not in location


def test_profile_next_empty_uses_fallback(admin_client):
    """An empty next_url redirects to the default account settings page."""
    resp = admin_client.post(
        "/settings",
        data={"action": "update_profile", "real_name": "Admin", "bio": "",
              "next_url": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/settings" in resp.headers["Location"]


def test_settings_profile_management_moves_to_users_page(admin_client):
    """Profile editing controls should live on /users/<username>, not /settings."""
    resp = admin_client.get("/settings")
    assert resp.status_code == 200
    assert b"Manage Profile on /users" in resp.data
    assert b"Save Profile Fields" not in resp.data


def test_save_profile_fields_redirects_to_profile_and_persists(admin_client):
    """Saving profile fields redirects to the user profile and persists values."""
    import db
    from werkzeug.datastructures import MultiDict

    db.register_plugin(
        "user_profiles",
        name="User Profiles",
        version="1.0.0",
        author="BananaWiki",
        description="Profile pages and fields",
        builtin=True,
        enabled=True,
    )
    with db.get_db_context() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_profile_fields__definitions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, "
            "label TEXT NOT NULL, field_type TEXT NOT NULL DEFAULT 'text', "
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_profile_fields__values ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
            "field_id INTEGER NOT NULL, value TEXT NOT NULL DEFAULT '', "
            "visible INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "FOREIGN KEY (field_id) REFERENCES user_profile_fields__definitions(id) ON DELETE CASCADE, "
            "UNIQUE(user_id, field_id))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_profile_fields__definitions "
            "(key, label, field_type, sort_order) VALUES ('location', 'Location', 'text', 0)"
        )
        conn.commit()

    resp = admin_client.post(
        "/settings",
        data=MultiDict([
            ("action", "save_profile_fields"),
            ("profile_field_key", "location"),
            ("profile_field_visible", "location"),
            ("profile_value_location", "Rome"),
        ]),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/users/admin#profile-fields")
    values = db.get_user_field_values(db.get_user_by_username("admin")["id"])
    assert values["location"]["value"] == "Rome"
    assert values["location"]["visible"] is True


# ---------------------------------------------------------------------------
# Admin can upload avatar for a user via admin_moderate_profile
# ---------------------------------------------------------------------------

def _create_profile_fields_tables():
    """Create profile fields tables if they don't exist."""
    import db
    with db.get_db_context() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_profile_fields__definitions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, "
            "label TEXT NOT NULL, field_type TEXT NOT NULL DEFAULT 'text', "
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_profile_fields__values ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
            "field_id INTEGER NOT NULL, value TEXT NOT NULL DEFAULT '', "
            "visible INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "FOREIGN KEY (field_id) REFERENCES user_profile_fields__definitions(id) ON DELETE CASCADE, "
            "UNIQUE(user_id, field_id))"
        )
        conn.commit()


def test_admin_can_upload_avatar_for_user(admin_client, regular_uid):
    """Admin can upload an avatar for another user via admin_moderate_profile."""
    import db
    # Create a valid 1x1 PNG image
    from PIL import Image
    img_buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color="red").save(img_buffer, format="PNG")
    img_buffer.seek(0)

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={
            "action": "edit_profile",
            "real_name": "Alice",
            "bio": "Bio",
            "avatar": (img_buffer, "avatar.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile is not None
    assert profile["avatar_filename"].startswith("avatars/")
    assert profile["avatar_filename"].endswith(".png")
    assert profile["real_name"] == "Alice"
    assert profile["bio"] == "Bio"


def test_admin_avatar_upload_invalid_type_rejected(admin_client, regular_uid):
    """Admin avatar upload rejects invalid file types (e.g. SVG)."""
    data = {
        "action": "edit_profile",
        "real_name": "",
        "bio": "",
        "avatar": (io.BytesIO(b"<svg/>"), "avatar.svg"),
    }
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid avatar" in resp.data or b"invalid" in resp.data.lower()


def test_admin_avatar_upload_too_large_rejected(admin_client, regular_uid):
    """Admin avatar upload rejects files over 1 MB."""
    big_bytes = b"\x00" * (MAX_AVATAR_SIZE_BYTES + 1)
    data = {
        "action": "edit_profile",
        "real_name": "",
        "bio": "",
        "avatar": (io.BytesIO(big_bytes), "photo.jpg"),
    }
    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"1 MB" in resp.data or b"smaller" in resp.data.lower()


def test_admin_edit_profile_replaces_old_avatar(admin_client, regular_uid, tmp_path):
    """Admin uploading a new avatar removes the old one."""
    import db
    # Set up initial avatar
    db.upsert_user_profile(regular_uid, avatar_filename="avatars/old.png")
    old_path = os.path.join(config.UPLOAD_FOLDER, "avatars", "old.png")
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    open(old_path, "w").close()

    from PIL import Image
    img_buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color="blue").save(img_buffer, format="PNG")
    img_buffer.seek(0)

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={
            "action": "edit_profile",
            "real_name": "",
            "bio": "",
            "avatar": (img_buffer, "new.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["avatar_filename"] != "avatars/old.png"
    assert profile["avatar_filename"].startswith("avatars/")
    assert not os.path.exists(old_path)


def test_admin_can_save_profile_fields_for_user(admin_client, regular_uid):
    """Admin can save profile fields for another user."""
    import db
    from werkzeug.datastructures import MultiDict

    db.register_plugin(
        "user_profiles",
        name="User Profiles",
        version="1.0.0",
        author="BananaWiki",
        description="Profile pages and fields",
        builtin=True,
        enabled=True,
    )
    _create_profile_fields_tables()
    with db.get_db_context() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_profile_fields__definitions "
            "(key, label, field_type, sort_order) VALUES ('location', 'Location', 'text', 0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_profile_fields__definitions "
            "(key, label, field_type, sort_order) VALUES ('hobbies', 'Hobbies', 'longtext', 1)"
        )
        conn.commit()

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data=MultiDict([
            ("action", "edit_profile"),
            ("real_name", "Alice"),
            ("bio", "Bio"),
            ("profile_field_key", "location"),
            ("profile_field_visible", "location"),
            ("profile_value_location", "New York"),
            ("profile_field_key", "hobbies"),
            ("profile_value_hobbies", "Reading, Coding"),
        ]),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    values = db.get_user_field_values(regular_uid)
    assert values["location"]["value"] == "New York"
    assert values["location"]["visible"] is True
    assert values["hobbies"]["value"] == "Reading, Coding"
    assert values["hobbies"]["visible"] is False


def test_admin_profile_fields_visible_on_user_page(admin_client, regular_uid):
    """Admin can see profile field data on another user's profile page."""
    import db
    from werkzeug.datastructures import MultiDict

    db.register_plugin(
        "user_profiles",
        name="User Profiles",
        version="1.0.0",
        author="BananaWiki",
        description="Profile pages and fields",
        builtin=True,
        enabled=True,
    )
    _create_profile_fields_tables()
    with db.get_db_context() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_profile_fields__definitions "
            "(key, label, field_type, sort_order) VALUES ('location', 'Location', 'text', 0)"
        )
        conn.commit()

    # Save a field value
    db.save_user_field_value(regular_uid, "location", "Paris", True)
    db.upsert_user_profile(regular_uid, page_published=True)

    resp = admin_client.get("/users/alice")
    assert resp.status_code == 200
    assert b"Paris" in resp.data


def test_admin_edit_profile_without_avatar_keeps_existing(admin_client, regular_uid):
    """Admin editing profile without uploading avatar preserves existing avatar."""
    import db
    db.upsert_user_profile(regular_uid, avatar_filename="avatars/existing.png",
                           real_name="Old Name", bio="Old Bio")

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={
            "action": "edit_profile",
            "real_name": "New Name",
            "bio": "New Bio",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["avatar_filename"] == "avatars/existing.png"
    assert profile["real_name"] == "New Name"
    assert profile["bio"] == "New Bio"


def test_admin_remove_avatar_still_works(admin_client, regular_uid, tmp_path):
    """Admin can still remove a user's avatar via remove_avatar action."""
    import db
    db.upsert_user_profile(regular_uid, avatar_filename="avatars/toremove.png")
    fake_path = os.path.join(config.UPLOAD_FOLDER, "avatars", "toremove.png")
    os.makedirs(os.path.dirname(fake_path), exist_ok=True)
    open(fake_path, "w").close()

    resp = admin_client.post(
        f"/admin/users/{regular_uid}/profile",
        data={"action": "remove_avatar"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    profile = db.get_user_profile(regular_uid)
    assert profile["avatar_filename"] == ""
