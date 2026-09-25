import io
import os

import pytest
from PIL import Image

import config
import db


@pytest.fixture
def background_uploads(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_root))
    monkeypatch.setattr(config, "BACKGROUND_IMAGE_MAX_UPLOAD_SIZE", 4 * 1024 * 1024)
    monkeypatch.setattr(config, "BACKGROUND_IMAGE_MAX_PIXELS", 16_000_000)
    monkeypatch.setattr(config, "BACKGROUND_IMAGE_MAX_DIMENSION", 2560)
    return upload_root


def _image_file(width=32, height=32, color="red", fmt="PNG", filename="background.png"):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format=fmt)
    buf.seek(0)
    return buf, filename


def test_custom_background_upload_is_resized_and_persisted(logged_in_admin, admin_user, background_uploads):
    image, filename = _image_file(width=4000, height=1000)
    resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (image, filename)},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    data = resp.get_json()
    stored_name = data["background_image"]
    assert stored_name.startswith("backgrounds/")
    assert stored_name.endswith(".jpg")
    assert data["url"].endswith(f"/static/uploads/{stored_name}")

    saved_path = os.path.join(config.UPLOAD_FOLDER, stored_name)
    assert os.path.exists(saved_path)
    with Image.open(saved_path) as saved:
        assert saved.format == "JPEG"
        assert max(saved.size) <= config.BACKGROUND_IMAGE_MAX_DIMENSION

    prefs = db.get_user_accessibility(admin_user)
    assert prefs["background_image"] == stored_name


def test_custom_background_upload_rejects_large_request(logged_in_admin, background_uploads, monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND_IMAGE_MAX_UPLOAD_SIZE", 16)
    image, filename = _image_file()

    resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (image, filename)},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 413
    assert not os.path.exists(os.path.join(config.UPLOAD_FOLDER, "backgrounds"))


def test_custom_background_upload_rejects_large_pixel_count(logged_in_admin, background_uploads, monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND_IMAGE_MAX_PIXELS", 50)
    image, filename = _image_file(width=10, height=10)

    resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (image, filename)},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 413
    assert not os.path.exists(os.path.join(config.UPLOAD_FOLDER, "backgrounds"))


def test_custom_background_upload_replaces_old_file(logged_in_admin, admin_user, background_uploads):
    first_image, first_filename = _image_file(color="red", filename="first.png")
    first_resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (first_image, first_filename)},
        content_type="multipart/form-data",
    )
    first_name = first_resp.get_json()["background_image"]
    first_path = os.path.join(config.UPLOAD_FOLDER, first_name)
    assert os.path.exists(first_path)

    second_image, second_filename = _image_file(color="blue", filename="second.png")
    second_resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (second_image, second_filename)},
        content_type="multipart/form-data",
    )
    second_name = second_resp.get_json()["background_image"]

    assert second_resp.status_code == 200
    assert first_name != second_name
    assert not os.path.exists(first_path)
    assert os.path.exists(os.path.join(config.UPLOAD_FOLDER, second_name))
    assert db.get_user_accessibility(admin_user)["background_image"] == second_name


def test_custom_background_delete_clears_pref_and_file(logged_in_admin, admin_user, background_uploads):
    image, filename = _image_file()
    upload_resp = logged_in_admin.post(
        "/api/accessibility/background",
        data={"file": (image, filename)},
        content_type="multipart/form-data",
    )
    stored_name = upload_resp.get_json()["background_image"]
    stored_path = os.path.join(config.UPLOAD_FOLDER, stored_name)
    assert os.path.exists(stored_path)

    resp = logged_in_admin.delete("/api/accessibility/background")

    assert resp.status_code == 200
    assert resp.get_json()["background_image"] == ""
    assert db.get_user_accessibility(admin_user)["background_image"] == ""
    assert not os.path.exists(stored_path)
