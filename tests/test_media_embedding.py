"""
Tests for the unified media embedding system.

Covers: responsive CSS classes, extended video shortcode attributes
(margin, autoplay, loop, controls, preload), image margin support,
and editor modal UI elements.
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
def editor_user():
    """Create an editor user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editorpass"), role="editor")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, admin_user, editor_user):
    """Return a client logged in as editor (admin_user needed for setup_done)."""
    client.post("/login", data={"username": "editor1", "password": "editorpass"})
    return client


# ---------------------------------------------------------------------------
# Video shortcode with new attributes
# ---------------------------------------------------------------------------


class TestVideoShortcodeExtended:
    """Test the extended video shortcode with margin, autoplay, loop, controls, preload."""

    def test_shortcode_with_margin(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" margin="20"]]'
        result = render_markdown(md, embed_videos=True)
        assert 'data-bw-margin="20"' in result
        assert "margin-top:20px" in result
        assert "margin-bottom:20px" in result

    def test_shortcode_with_autoplay(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" autoplay="true"]]'
        result = render_markdown(md, embed_videos=True)
        assert 'data-bw-autoplay="true"' in result
        assert "autoplay=1" in result

    def test_shortcode_with_loop(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" loop="true"]]'
        result = render_markdown(md, embed_videos=True)
        assert 'data-bw-loop="true"' in result
        assert "loop=1" in result

    def test_shortcode_with_controls_disabled(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" controls="false"]]'
        result = render_markdown(md, embed_videos=True)
        assert 'data-bw-controls="false"' in result
        assert "controls=0" in result

    def test_shortcode_with_preload(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" preload="auto"]]'
        result = render_markdown(md, embed_videos=True)
        assert 'data-bw-preload="auto"' in result

    def test_shortcode_with_all_new_attributes(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" width="640" align="right" ratio="4:3" margin="20" autoplay="true" loop="true" controls="false" preload="auto"]]'
        result = render_markdown(md, embed_videos=True)
        assert "video-embed-right" in result
        assert 'data-bw-width="640"' in result
        assert 'data-bw-ratio="4:3"' in result
        assert 'data-bw-margin="20"' in result
        assert 'data-bw-autoplay="true"' in result
        assert 'data-bw-loop="true"' in result
        assert 'data-bw-controls="false"' in result
        assert 'data-bw-preload="auto"' in result
        assert "padding-bottom:75%" in result
        assert "autoplay=1" in result
        assert "loop=1" in result
        assert "controls=0" in result

    def test_shortcode_defaults_no_extra_data_attrs(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]'
        result = render_markdown(md, embed_videos=True)
        assert "video-embed" in result
        assert "data-bw-margin" not in result
        assert "data-bw-autoplay" not in result
        assert "data-bw-loop" not in result
        assert "data-bw-controls" not in result
        assert "data-bw-preload" not in result
        assert "autoplay=1" not in result
        assert "loop=1" not in result
        assert "controls=0" not in result

    def test_backward_compatible_old_shortcode(self):
        """Old shortcodes without new attributes should still work."""
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" width="640" align="right" ratio="4:3"]]'
        result = render_markdown(md, embed_videos=True)
        assert "video-embed-right" in result
        assert 'data-bw-width="640"' in result
        assert 'data-bw-ratio="4:3"' in result
        assert "padding-bottom:75%" in result

    def test_invalid_margin_ignored(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" margin="999"]]'
        result = render_markdown(md, embed_videos=True)
        # Margin value 999 > 200, should not match regex
        assert "data-bw-margin" not in result

    def test_vimeo_with_new_attributes(self):
        from app import render_markdown
        md = '[[video url="https://vimeo.com/123456789" margin="10" autoplay="true"]]'
        result = render_markdown(md, embed_videos=True)
        assert "player.vimeo.com/video/123456789" in result
        assert 'data-bw-margin="10"' in result
        assert 'data-bw-autoplay="true"' in result


# ---------------------------------------------------------------------------
# Responsive video classes
# ---------------------------------------------------------------------------


class TestVideoResponsiveClasses:
    """Test that video embeds include the new responsive CSS classes."""

    def test_video_has_media_video_container_class(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]'
        result = render_markdown(md, embed_videos=True)
        assert "media-video-container" in result

    def test_video_center_has_media_center_class(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]'
        result = render_markdown(md, embed_videos=True)
        assert "media-center" in result

    def test_video_left_has_media_left_class(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" align="left"]]'
        result = render_markdown(md, embed_videos=True)
        assert "media-left" in result

    def test_video_right_has_media_right_class(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" align="right"]]'
        result = render_markdown(md, embed_videos=True)
        assert "media-right" in result

    def test_video_iframe_has_responsive_class(self):
        from app import render_markdown
        md = '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]'
        result = render_markdown(md, embed_videos=True)
        assert "media-responsive-iframe" in result

    def test_bare_youtube_url_has_responsive_classes(self):
        from app import render_markdown
        md = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = render_markdown(md, embed_videos=True)
        assert "media-video-container" in result
        assert "media-responsive-iframe" in result


# ---------------------------------------------------------------------------
# Image sanitization with spacing
# ---------------------------------------------------------------------------


class TestImageSpacingSanitization:
    """Test that image spacing style attributes survive Bleach sanitization."""

    def test_figure_with_padding_style_survives_bleach(self):
        from app import render_markdown
        md = '<figure class="wiki-img-center media-figure media-center" style="--media-padding:20px;padding:20px"><img src="/static/uploads/test.jpg" alt="test" width="300" class="media-responsive"><figcaption>test</figcaption></figure>'
        result = render_markdown(md)
        assert "padding:20px" in result or "padding: 20px" in result
        assert "media-figure" in result
        assert "media-center" in result

    def test_img_with_padding_style_survives_bleach(self):
        from app import render_markdown
        md = '<img src="/static/uploads/test.jpg" alt="test" width="300" class="media-responsive" style="--media-padding:10px;padding:10px">'
        result = render_markdown(md)
        assert "padding" in result
        assert "media-responsive" in result

    def test_figure_with_margin_style_survives_bleach(self):
        from app import render_markdown
        md = '<figure class="wiki-img-center media-figure media-center" style="--media-margin-top:20px;margin-top:20px;--media-margin-bottom:20px;margin-bottom:20px"><img src="/static/uploads/test.jpg" alt="test" width="300" class="media-responsive"><figcaption>test</figcaption></figure>'
        result = render_markdown(md)
        assert "margin-top:20px" in result or "margin-top: 20px" in result
        assert "margin-bottom:20px" in result or "margin-bottom: 20px" in result
        assert "media-figure" in result
        assert "media-center" in result

    def test_img_with_margin_style_survives_bleach(self):
        from app import render_markdown
        md = '<img src="/static/uploads/test.jpg" alt="test" width="300" class="media-responsive" style="--media-margin-top:10px;margin-top:10px">'
        result = render_markdown(md)
        assert "margin-top" in result
        assert "media-responsive" in result

    def test_dangerous_style_stripped(self):
        """Ensure dangerous CSS properties are stripped by the sanitizer."""
        from app import render_markdown
        md = '<figure class="wiki-img-center" style="background:url(javascript:alert(1));margin-top:10px"><img src="/static/uploads/test.jpg" alt="test"></figure>'
        result = render_markdown(md)
        assert "javascript" not in result
        assert "url(" not in result
        assert "background" not in result.split("margin")[0]  # background is removed
        assert "margin-top" in result


# ---------------------------------------------------------------------------
# Preview API with new features
# ---------------------------------------------------------------------------


class TestPreviewApiMediaFeatures:
    def test_preview_api_video_with_margin(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" margin="15"]]'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'data-bw-margin="15"' in data["html"]
        assert "margin-top:15px" in data["html"]

    def test_preview_api_video_with_autoplay(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" autoplay="true"]]'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "autoplay=1" in data["html"]

    def test_preview_api_video_with_loop_and_controls(self, logged_in_admin):
        resp = logged_in_admin.post(
            "/api/preview",
            json={"content": '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" loop="true" controls="false"]]'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "loop=1" in data["html"]
        assert "controls=0" in data["html"]


# ---------------------------------------------------------------------------
# Page rendering with new media features
# ---------------------------------------------------------------------------


class TestPageMediaRendering:
    def test_page_with_video_margin_renders(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        db.update_page(
            home["id"],
            home["title"],
            '[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" margin="20" autoplay="true"]]',
            admin_user,
            "test",
        )
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200
        assert b'data-bw-margin="20"' in resp.data
        assert b'data-bw-autoplay="true"' in resp.data

    def test_page_with_image_margin_renders(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        content = '<figure class="wiki-img-center media-figure media-center" style="--media-margin-top:15px;margin-top:15px;--media-margin-bottom:15px;margin-bottom:15px"><img src="/static/uploads/test.jpg" alt="test" width="300" class="media-responsive"><figcaption>test</figcaption></figure>'
        db.update_page(home["id"], home["title"], content, admin_user, "test")
        resp = logged_in_admin.get("/")
        assert resp.status_code == 200
        assert b"margin-top" in resp.data
        assert b"media-figure" in resp.data


# ---------------------------------------------------------------------------
# Editor UI modal elements
# ---------------------------------------------------------------------------


class TestEditorMediaModals:
    def test_image_modal_has_padding_input(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'id="img-padding-input"' in resp.data
        assert b'id="img-margin-top-input"' not in resp.data
        assert b'id="img-margin-bottom-input"' not in resp.data

    def test_video_modal_has_margin_input(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'id="video-margin-input"' in resp.data

    def test_video_modal_has_autoplay_checkbox(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'id="video-autoplay-input"' in resp.data

    def test_video_modal_has_controls_checkbox(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'id="video-controls-input"' in resp.data

    def test_video_modal_has_loop_checkbox(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'id="video-loop-input"' in resp.data

    def test_video_modal_has_preload_buttons(self, logged_in_admin, admin_user):
        import db
        home = db.get_home_page()
        resp = logged_in_admin.get(f"/page/{home['slug']}/edit")
        assert resp.status_code == 200
        assert b'video-preload-btn' in resp.data
        assert b'data-preload="none"' in resp.data
        assert b'data-preload="metadata"' in resp.data
        assert b'data-preload="auto"' in resp.data


# ---------------------------------------------------------------------------
# Normalization edge cases
# ---------------------------------------------------------------------------


class TestVideoOptionNormalization:
    def test_normalize_invalid_margin(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, margin, _, _, _, _ = _normalize_video_options(margin="999")
        assert margin == ""

    def test_normalize_valid_margin(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, margin, _, _, _, _ = _normalize_video_options(margin="50")
        assert margin == "50"

    def test_normalize_zero_margin(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, margin, _, _, _, _ = _normalize_video_options(margin="0")
        assert margin == "0"

    def test_normalize_autoplay_true(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, autoplay, _, _, _ = _normalize_video_options(autoplay="true")
        assert autoplay is True

    def test_normalize_autoplay_false(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, autoplay, _, _, _ = _normalize_video_options(autoplay="false")
        assert autoplay is False

    def test_normalize_autoplay_none(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, autoplay, _, _, _ = _normalize_video_options(autoplay=None)
        assert autoplay is False

    def test_normalize_controls_true(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, controls, _ = _normalize_video_options(controls="true")
        assert controls is True

    def test_normalize_controls_false(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, controls, _ = _normalize_video_options(controls="false")
        assert controls is False

    def test_normalize_controls_default(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, controls, _ = _normalize_video_options(controls=None)
        assert controls is True

    def test_normalize_loop(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, loop, _, _ = _normalize_video_options(loop="true")
        assert loop is True

    def test_normalize_preload_valid(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, _, preload = _normalize_video_options(preload="auto")
        assert preload == "auto"

    def test_normalize_preload_invalid(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, _, preload = _normalize_video_options(preload="invalid")
        assert preload == ""

    def test_normalize_preload_none(self):
        from helpers._markdown import _normalize_video_options
        _, _, _, _, _, _, _, preload = _normalize_video_options(preload=None)
        assert preload == ""

    def test_backward_compatible_three_arg_call(self):
        """Old callers passing only width/align/ratio should still work."""
        from helpers._markdown import _normalize_video_options
        width, align, ratio, margin, autoplay, loop, controls, preload = (
            _normalize_video_options(width="640", align="left", ratio="4:3")
        )
        assert width == "640"
        assert align == "left"
        assert ratio == "4:3"
        assert margin == ""
        assert autoplay is False
        assert loop is False
        assert controls is True
        assert preload == ""
