"""
Tests for video embedding and session limit features.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate limit store before and after each test."""
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
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def regular_user():
    """Create a regular (non-admin) user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("user1", generate_password_hash("userpass"), role="user")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# ---------------------------------------------------------------------------
# Video embedding: _embed_videos_in_html helper
# ---------------------------------------------------------------------------

class TestEmbedVideosInHtml:
    def test_youtube_watch_url_is_embedded(self):
        from app import _embed_videos_in_html
        html = '<p><a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">https://www.youtube.com/watch?v=dQw4w9WgXcQ</a></p>'
        result = _embed_videos_in_html(html)
        assert "video-embed" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in result
        assert "<iframe" in result

    def test_youtube_short_url_is_embedded(self):
        from app import _embed_videos_in_html
        html = '<p><a href="https://youtu.be/dQw4w9WgXcQ">https://youtu.be/dQw4w9WgXcQ</a></p>'
        result = _embed_videos_in_html(html)
        assert "video-embed" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in result

    def test_vimeo_url_is_embedded(self):
        from app import _embed_videos_in_html
        html = '<p><a href="https://vimeo.com/123456789">https://vimeo.com/123456789</a></p>'
        result = _embed_videos_in_html(html)
        assert "video-embed" in result
        assert "https://player.vimeo.com/video/123456789" in result

    def test_non_video_link_not_affected(self):
        from app import _embed_videos_in_html
        html = '<p><a href="https://example.com">Example</a></p>'
        result = _embed_videos_in_html(html)
        assert "video-embed" not in result
        assert result == html

    def test_youtube_link_with_custom_text_not_embedded(self):
        """Links with custom text (href != text) should not be embedded."""
        from app import _embed_videos_in_html
        html = '<p><a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">Watch this video</a></p>'
        result = _embed_videos_in_html(html)
        assert "video-embed" not in result

    def test_render_markdown_embed_videos_false(self):
        from app import render_markdown
        md = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = render_markdown(md, embed_videos=False)
        assert "video-embed" not in result
        assert "<iframe" not in result

    def test_render_markdown_embed_videos_true_bare_url(self):
        from app import render_markdown
        # Bare URL on its own line → wrapped in <p> by markdown
        md = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = render_markdown(md, embed_videos=True)
        assert "video-embed" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in result

    def test_render_markdown_embed_videos_true_linked_url(self):
        from app import render_markdown
        # Angle-bracket syntax → markdown produces <a> tag
        md = "<https://www.youtube.com/watch?v=dQw4w9WgXcQ>"
        result = render_markdown(md, embed_videos=True)
        assert "video-embed" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in result

    def test_render_markdown_embed_videos_true_custom_shortcode(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" width="640" align="right" ratio="4:3"]]'
        result = render_markdown(md, embed_videos=True)
        assert "video-embed-right" in result
        assert 'data-bw-width="640"' in result
        assert 'data-bw-ratio="4:3"' in result
        assert "padding-bottom:75%" in result


# ---------------------------------------------------------------------------
# Video embedding: admin settings toggle
# ---------------------------------------------------------------------------

class TestVideoEmbedSetting:
    def test_page_always_embeds_video(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        db.update_page(
            home["id"],
            home["title"],
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            admin_user,
            "test",
        )
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200
        assert b"video-embed" in resp.data
        assert b"youtube.com/embed/dQw4w9WgXcQ" in resp.data

    def test_settings_page_has_no_video_embed_checkbox(self, logged_in_admin):
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200
        assert b"video_embed_enabled" not in resp.data


# ---------------------------------------------------------------------------
# Preview API: video embedding in edit mode
# ---------------------------------------------------------------------------

class TestPreviewApiVideoEmbed:
    def test_preview_api_embeds_youtube_video(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "video-embed" in data["html"]
        assert "youtube.com/embed/dQw4w9WgXcQ" in data["html"]

    def test_preview_api_embeds_vimeo_video(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": "https://vimeo.com/123456789"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "video-embed" in data["html"]
        assert "player.vimeo.com/video/123456789" in data["html"]

    def test_preview_api_embeds_persisted_video_shortcode(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={
                "content": '[[video url="https://youtu.be/dQw4w9WgXcQ" width="720" align="left" ratio="1:1"]]'
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "video-embed-left" in data["html"]
        assert 'data-bw-width="720"' in data["html"]
        assert 'data-bw-ratio="1:1"' in data["html"]
        assert "padding-bottom:100%" in data["html"]
        assert "youtube.com/embed/dQw4w9WgXcQ" in data["html"]


# ---------------------------------------------------------------------------
# Session limit feature
# ---------------------------------------------------------------------------

class TestSessionLimitSetting:
    def test_session_limit_disabled_by_default(self):
        import db
        db.init_db()
        settings = db.get_site_settings()
        assert settings["session_limit_enabled"] == 0

    def test_admin_can_enable_session_limit(self, logged_in_admin):
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "BananaWiki",
            "timezone": "UTC",
            "primary_color": "#7c8dc6",
            "secondary_color": "#151520",
            "accent_color": "#6e8aca",
            "text_color": "#b8bcc8",
            "sidebar_color": "#111118",
            "bg_color": "#0d0d14",
            "session_limit_enabled": "1",
        })
        assert resp.status_code in (200, 302)
        import db
        settings = db.get_site_settings()
        assert settings["session_limit_enabled"] == 1

    def test_settings_page_has_session_limit_checkbox(self, logged_in_admin):
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200
        assert b"session_limit_enabled" in resp.data

    def test_session_token_set_on_login_when_limit_enabled(self, client, admin_user):
        import db
        db.update_site_settings(session_limit_enabled=1)
        client.post("/login", data={"username": "admin", "password": "admin123"})
        user = db.get_user_by_id(admin_user)
        assert user["session_token"] is not None
        # uuid4().hex produces exactly 32 hex characters
        assert len(user["session_token"]) == 32

    def test_session_token_not_set_on_login_when_limit_disabled(self, client, admin_user):
        import db
        db.update_site_settings(session_limit_enabled=0)
        # Ensure no token from previous logins
        db.update_user(admin_user, session_token=None)
        client.post("/login", data={"username": "admin", "password": "admin123"})
        user = db.get_user_by_id(admin_user)
        assert user["session_token"] is None

    def test_second_login_invalidates_first_session(self, client, admin_user):
        """When session_limit is on, logging in from a second client ends the first session."""
        from app import app
        import db
        db.update_site_settings(session_limit_enabled=1)

        # First client logs in
        with app.test_client() as client1:
            client1.post("/login", data={"username": "admin", "password": "admin123"})
            # First client can access the home page
            resp1 = client1.get("/")
            assert resp1.status_code == 200

            # Second client logs in from a different "device"
            with app.test_client() as client2:
                client2.post("/login", data={"username": "admin", "password": "admin123"})

            # First client's next request should be redirected to session-conflict page
            resp1_after = client1.get("/")
            assert resp1_after.status_code == 302
            assert "/session-conflict" in resp1_after.headers["Location"]

    def test_session_limit_disabled_allows_multiple_sessions(self, client, admin_user):
        """When session_limit is off, multiple sessions are not invalidated."""
        from app import app
        import db
        db.update_site_settings(session_limit_enabled=0)

        with app.test_client() as client1:
            client1.post("/login", data={"username": "admin", "password": "admin123"})

            with app.test_client() as client2:
                client2.post("/login", data={"username": "admin", "password": "admin123"})

            # First client's session should still be valid
            resp1 = client1.get("/")
            assert resp1.status_code == 200

    def test_logout_clears_session_token_when_limit_enabled(self, client, admin_user):
        """Logging out should clear the stored session token used by the single-session guard."""
        import db

        db.update_site_settings(session_limit_enabled=1)
        client.post("/login", data={"username": "admin", "password": "admin123"})
        assert db.get_user_by_id(admin_user)["session_token"] is not None

        resp = client.post("/logout")
        assert resp.status_code == 302
        assert db.get_user_by_id(admin_user)["session_token"] is None

    def test_account_password_change_rotates_current_session_token(self, client, admin_user):
        """Self-service password changes should keep the current session aligned with the new token."""
        import db
        from werkzeug.security import generate_password_hash

        db.update_site_settings(session_limit_enabled=1)
        user_id = db.create_user("pwrotate", generate_password_hash("oldpass"), role="user")

        client.post("/login", data={"username": "pwrotate", "password": "oldpass"})
        original_token = db.get_user_by_id(user_id)["session_token"]

        resp = client.post("/settings", data={
            "action": "change_password",
            "current_password": "oldpass",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        })
        assert resp.status_code == 302

        user = db.get_user_by_id(user_id)
        assert user["session_token"] is not None
        assert user["session_token"] != original_token
        with client.session_transaction() as sess:
            assert sess["session_token"] == user["session_token"]

        home_resp = client.get("/")
        assert home_resp.status_code == 200

    def test_stale_cookie_token_not_treated_as_admin_invalidation_when_limit_off(self, admin_user):
        """Regression: when session_limit is OFF, a stale cookie token left over from when the
        limit was enabled must NOT trigger the 'invalidated by administrator' redirect.

        Scenario:
          1. Session-limit is ON  → login issues a token; DB and cookie both have it.
          2. Admin disables session-limit.
          3. Another device logs in  → login code clears the DB token to NULL.
          4. Original device's cookie still has the old token.
        Expected: original session continues to work (token silently removed from cookie).
        Broken before fix: user was redirected to /login with
          "Your session has been invalidated by an administrator."
        """
        from app import app
        import db

        # Step 1: enable session-limit, log in on Device 1
        db.update_site_settings(session_limit_enabled=1)
        with app.test_client() as device1:
            device1.post("/login", data={"username": "admin", "password": "admin123"})
            # Confirm Device 1 is active
            assert device1.get("/").status_code == 200

            # Step 2: disable session-limit
            db.update_site_settings(session_limit_enabled=0)

            # Step 3: another device logs in (clears DB token because limit is now off)
            with app.test_client() as device2:
                device2.post("/login", data={"username": "admin", "password": "admin123"})
                # Device 2 works fine
                assert device2.get("/").status_code == 200

            # Step 4: Device 1 still has the old token in its cookie but DB token is NULL.
            # With the fix this MUST return 200, not redirect to /login with "admin" message.
            resp = device1.get("/")
            assert resp.status_code == 200, (
                "Session was unexpectedly invalidated when session_limit is disabled."
            )

    def test_stale_db_token_does_not_kick_session_when_limit_off(self, admin_user):
        """When session_limit is OFF, a stored_token != session_token mismatch
        (leftover from a prior enabled period) must NOT kick the user out.
        The session should be silently re-synchronised instead, with the cookie
        adopting the DB token so future requests land in the matching branch.
        """
        from app import app
        import db

        # Artificially plant a mismatched token: DB has tokenA, session has tokenB.
        db.update_site_settings(session_limit_enabled=0)
        db.update_user(admin_user, session_token="db_token_abc")

        with app.test_client() as c:
            # Build a session that has a different token than what's in the DB
            with c.session_transaction() as sess:
                sess["user_id"] = admin_user
                sess["session_token"] = "cookie_token_xyz"

            # Should NOT be kicked out; should be served normally
            resp = c.get("/")
            assert resp.status_code == 200, (
                "Session was unexpectedly invalidated on token mismatch when limit is disabled."
            )
            # The cookie token should now match the DB token (adopted during re-sync)
            with c.session_transaction() as sess:
                assert sess.get("session_token") == "db_token_abc", (
                    "Cookie token was not re-synchronised to the DB token."
                )

    def test_mass_logout_still_works_when_limit_enabled(self, admin_user):
        """When session_limit IS enabled, mass-logout (DB token → NULL with stale cookie token)
        must still redirect to /login with the 'invalidated by administrator' message.
        """
        from app import app
        import db

        db.update_site_settings(session_limit_enabled=1)
        with app.test_client() as c:
            c.post("/login", data={"username": "admin", "password": "admin123"})
            # Simulate mass-logout (admin clears all DB tokens)
            db.update_user(admin_user, session_token=None)

            # Session cookie still has the old token → should be redirected
            resp = c.get("/")
            assert resp.status_code == 302
            location = resp.headers["Location"]
            assert "/login" in location

    def test_admin_password_change_invalidates_existing_target_session(self, admin_user):
        """Admin password resets should rotate tokens so the target's legacy session is rejected."""
        from app import app
        import db
        from werkzeug.security import generate_password_hash

        db.update_site_settings(session_limit_enabled=1)
        user_id = db.create_user("targetuser", generate_password_hash("oldpass"), role="user")
        db.update_user(user_id, session_token="legacytoken")

        with app.test_client() as admin_client, app.test_client() as target_client:
            admin_client.post("/login", data={"username": "admin", "password": "admin123"})
            with target_client.session_transaction() as sess:
                sess["user_id"] = user_id
                sess["session_token"] = "legacytoken"

            resp = admin_client.post(
                f"/admin/users/{user_id}/edit",
                data={
                    "action": "change_password",
                    "password": "newpass123",
                    "confirm_password": "newpass123",
                },
            )
            assert resp.status_code == 302
            assert db.get_user_by_id(user_id)["session_token"] != "legacytoken"

            blocked = target_client.get("/")
            assert blocked.status_code == 302
            assert "/session-conflict" in blocked.headers["Location"]


# ---------------------------------------------------------------------------
# Session conflict page
# ---------------------------------------------------------------------------

class TestSessionConflictPage:
    def test_session_conflict_page_loads(self, client, admin_user):
        resp = client.get("/session-conflict")
        assert resp.status_code == 200
        assert b"Sign in again to continue" in resp.data
        assert b"one active session at a time" in resp.data

    def test_session_conflict_page_has_form(self, client, admin_user):
        resp = client.get("/session-conflict")
        assert resp.status_code == 200
        assert b"session-conflict/force" in resp.data
        assert b"Continue on this device" in resp.data

    def test_session_conflict_force_with_wrong_password(self, client, admin_user):
        resp = client.post("/session-conflict/force", data={
            "username": "admin", "password": "wrongpassword"
        })
        assert resp.status_code in (200, 302)
        # Should not redirect to home
        if resp.status_code == 302:
            assert "/session-conflict" in resp.headers["Location"]

    def test_session_conflict_force_with_correct_credentials(self, client, admin_user):
        resp = client.post("/session-conflict/force", data={
            "username": "admin", "password": "admin123"
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_session_conflict_force_preserves_password_spaces(self, client):
        """Password with leading/trailing spaces must work on session-conflict/force."""
        from werkzeug.security import generate_password_hash
        import db
        spaced_pw = " spaced password "
        db.create_user("spacey", generate_password_hash(spaced_pw), role="editor")
        db.update_site_settings(setup_done=1)
        # Should succeed with the exact password (including spaces)
        resp = client.post("/session-conflict/force", data={
            "username": "spacey", "password": spaced_pw
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "/session-conflict" not in resp.headers["Location"]

    def test_session_conflict_redirected_on_token_mismatch(self, client, admin_user):
        import db
        db.update_site_settings(session_limit_enabled=1)
        from app import app
        with app.test_client() as c1:
            c1.post("/login", data={"username": "admin", "password": "admin123"})
            with app.test_client() as c2:
                c2.post("/login", data={"username": "admin", "password": "admin123"})
            resp = c1.get("/")
            assert resp.status_code == 302
            assert "session-conflict" in resp.headers["Location"]

    def test_session_conflict_force_does_not_store_token_when_feature_disabled(self, client, admin_user):
        """When session_limit_enabled is False, force-login must not write a session token."""
        import db
        db.update_site_settings(session_limit_enabled=0)
        token_before = db.get_user_by_id(admin_user)["session_token"]
        resp = client.post("/session-conflict/force", data={
            "username": "admin", "password": "admin123"
        }, follow_redirects=False)
        assert resp.status_code == 302
        token_after = db.get_user_by_id(admin_user)["session_token"]
        # Token must not have changed when the feature is disabled
        assert token_after == token_before

    def test_session_conflict_force_stores_token_when_feature_enabled(self, client, admin_user):
        """When session_limit_enabled is True, force-login must write a new session token."""
        import db
        db.update_site_settings(session_limit_enabled=1)
        token_before = db.get_user_by_id(admin_user)["session_token"]
        resp = client.post("/session-conflict/force", data={
            "username": "admin", "password": "admin123"
        }, follow_redirects=False)
        assert resp.status_code == 302
        token_after = db.get_user_by_id(admin_user)["session_token"]
        # Token must have been rotated when the feature is enabled
        assert token_after != token_before

    def test_stale_session_rejected_after_logout(self, admin_user):
        """A session that was valid before logout must be redirected after logout clears the token."""
        from app import app
        import db
        db.update_site_settings(session_limit_enabled=1)

        with app.test_client() as stale_client:
            stale_client.post("/login", data={"username": "admin", "password": "admin123"})
            # Confirm the stale client can access a protected page
            assert stale_client.get("/settings").status_code == 200

            # Simulate logout (clears session_token in DB to NULL)
            db.update_user(admin_user, session_token=None)

            # The stale client's next request must be rejected, not served
            resp = stale_client.get("/settings")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_stale_session_rejected_after_real_logout(self, admin_user):
        """A session that logged out via /logout must not be reusable."""
        from app import app
        import db
        db.update_site_settings(session_limit_enabled=1)

        # client1 logs in first, client2 logs in second (rotates the token)
        with app.test_client() as client1:
            client1.post("/login", data={"username": "admin", "password": "admin123"})
            # client1 has an older token; client2 logs in, rotating it
            with app.test_client() as client2:
                client2.post("/login", data={"username": "admin", "password": "admin123"})
                # client2 logs out via the real endpoint (sets DB token to NULL)
                client2.post("/logout")

            # client1's stale session must now be rejected
            resp = client1.get("/settings")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_stale_session_rejected_after_mass_logout(self, admin_user):
        """Sessions that were valid before mass-logout must be redirected after mass-logout."""
        from app import app
        import db
        from werkzeug.security import generate_password_hash
        db.update_site_settings(session_limit_enabled=1)
        db.create_user("muser", generate_password_hash("pass123"), role="user")

        with app.test_client() as user_client:
            user_client.post("/login", data={"username": "muser", "password": "pass123"})
            # Can access protected route before mass-logout
            assert user_client.get("/settings").status_code == 200

            # Mass-logout clears all session tokens
            db.mass_logout_all_users()

            # Stale session must now be rejected
            resp_user = user_client.get("/settings")
            assert resp_user.status_code == 302
            assert "/login" in resp_user.headers["Location"]

    def test_session_conflict_force_failed_attempt_recorded(self, client, admin_user):
        """Failed auth via /session-conflict/force must be recorded in login_attempts."""
        import db
        ip = "127.0.0.1"
        before = db.count_recent_login_attempts(ip, 60)
        client.post("/session-conflict/force", data={
            "username": "admin", "password": "wrongpassword"
        })
        after = db.count_recent_login_attempts(ip, 60)
        assert after == before + 1

    def test_session_conflict_force_success_clears_login_attempts(self, client, admin_user):
        """Successful auth via /session-conflict/force must clear login_attempts for that IP."""
        import db
        ip = "127.0.0.1"
        # Record a couple of failed attempts first
        db.record_login_attempt(ip)
        db.record_login_attempt(ip)
        assert db.count_recent_login_attempts(ip, 60) == 2
        # Successful login should clear them
        client.post("/session-conflict/force", data={
            "username": "admin", "password": "admin123"
        })
        assert db.count_recent_login_attempts(ip, 60) == 0

    def test_session_conflict_force_overlong_password_recorded(self, client, admin_user):
        """A password exceeding 1024 chars via /session-conflict/force must be recorded."""
        import db
        ip = "127.0.0.1"
        before = db.count_recent_login_attempts(ip, 60)
        client.post("/session-conflict/force", data={
            "username": "admin", "password": "x" * 1025
        })
        after = db.count_recent_login_attempts(ip, 60)
        assert after == before + 1

    def test_session_conflict_page_resists_reload(self, client, admin_user):
        """The page must persist typed input across reloads via sessionStorage
        and warn before unloading once credentials have been entered."""
        resp = client.get("/session-conflict")
        assert resp.status_code == 200
        # sessionStorage-based username restoration
        assert b"sessionStorage" in resp.data
        assert b"bw-session-conflict-username" in resp.data
        # beforeunload-based reload guard
        assert b"beforeunload" in resp.data

    def test_session_conflict_form_carries_next_in_hidden_field(self, client, admin_user):
        """The next URL must travel as a hidden form field so it survives
        accidental URL changes between GET and POST."""
        resp = client.get("/session-conflict?next=/some/page")
        assert resp.status_code == 200
        assert b'name="next"' in resp.data
        assert b'value="/some/page"' in resp.data

    def test_session_conflict_force_accepts_next_from_form_body(self, client, admin_user):
        """The /force endpoint must honour the next URL when supplied in the
        form body (the new template puts it there as a hidden input)."""
        import db
        db.update_site_settings(session_limit_enabled=1)
        resp = client.post("/session-conflict/force", data={
            "username": "admin",
            "password": "admin123",
            "next": "/wiki",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/wiki")


# ---------------------------------------------------------------------------
# Embed Video button and modal present in editor
# ---------------------------------------------------------------------------

class TestEmbedVideoEditorUI:
    def test_embed_video_button_in_editor(self, logged_in_admin):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b"embed-video-btn" in resp.data

    def test_embed_video_modal_in_editor(self, logged_in_admin):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b"video-embed-modal" in resp.data
        assert b"video-url-input" in resp.data
        assert b"video-insert-btn" in resp.data
        assert b"video-width-input" in resp.data
        assert b"video-reset-btn" in resp.data
        assert b'data-ratio="16:9"' in resp.data
        assert b'data-ratio="4:3"' in resp.data
        assert b'data-ratio="1:1"' in resp.data

    def test_video_url_inserted_bare_renders_as_embed(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        db.update_page(
            home["id"],
            home["title"],
            "https://www.youtube.com/watch?v=abc1234abcd",
            admin_user,
            "test",
        )
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200
        assert b"video-embed" in resp.data
        assert b"youtube.com/embed/abc1234abcd" in resp.data


# ---------------------------------------------------------------------------
# Edit image modal in editor (click-to-edit pre-population)
# ---------------------------------------------------------------------------

class TestEditImageModalUI:
    def test_image_options_modal_in_editor(self, logged_in_admin):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b"image-options-modal" in resp.data
        assert b"img-alt-input" in resp.data
        assert b"img-width-input" in resp.data
        assert b"img-reset-btn" in resp.data

    def test_image_options_modal_has_alignment_buttons(self, logged_in_admin):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'data-align="left"' in resp.data
        assert b'data-align="right"' in resp.data
        assert b'data-align="center"' in resp.data

    def test_edit_image_js_function_exists(self, logged_in_admin):
        from app import app
        with app.test_client() as c:
            media = c.get("/static/js/editor-media.js")
            editor = c.get("/static/js/editor-tools.js")
            assert media.status_code == editor.status_code == 200
            assert b"openEditImageModal" in media.data
            assert b"updateImageInEditor" in editor.data
            assert b"openVideoOptionsModal" in media.data
            assert b"updateVideoInEditor" in editor.data
