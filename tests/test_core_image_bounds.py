"""Image uploads decode within the limits of their use, one picture at a time per process."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin
from werkzeug.datastructures import FileStorage

from bananawiki.wiki import storage
from bananawiki.wiki.features.users import routes as user_routes
from bananawiki.wiki.features.users import service as user_service
from tests.features.pages_support import in_app

MIB = 1024 * 1024


def _png(size: tuple[int, int], mode: str = "1") -> bytes:
    """A picture whose header announces *size* in a few kilobytes."""
    buffer = io.BytesIO()
    Image.new(mode, size).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(size: tuple[int, int], *, orientation: int | None = None) -> bytes:
    buffer = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _save(app, data: bytes, filename: str, **options) -> storage.StoredFile:
    def upload() -> storage.StoredFile:
        return storage.save(FileStorage(io.BytesIO(data), filename=filename), "uploads", allowed=None,
                            max_bytes=10 * MIB, images_only=True, **options)

    return in_app(app, upload)


@pytest.fixture
def decoded(monkeypatch) -> list[int]:
    """Count the pictures that reached decoding (the per-process image slot)."""
    entered: list[int] = []
    original = storage.image_slot

    @contextmanager
    def counting():
        entered.append(1)
        with original():
            yield

    monkeypatch.setattr(storage, "image_slot", counting)
    return entered


@pytest.fixture
def bob(client, make_user, login):
    user = make_user("bob")
    login(client, user)
    return user


def test_avatar_pixel_limit_is_checked_before_decoding(client, db, bob, decoded):
    """A 4.2-megapixel avatar of a few KB used to be decoded (and copied) at full size."""
    data = _png((2100, 2000))
    assert len(data) < user_service.AVATAR_MAX_BYTES
    response = client.post("/settings/profile", data={"avatar": (io.BytesIO(data), "me.png")},
                           content_type="multipart/form-data", follow_redirects=True)
    assert "4 megapixels" in response.get_data(as_text=True)
    assert decoded == []
    assert db.scalar("SELECT avatar_filename FROM user_profiles WHERE user_id = ?", (bob["id"],)) in (None, "")


def test_other_images_keep_the_general_pixel_limit(app):
    stored = _save(app, _png((2100, 2000)), "wide.png")
    assert stored.filename.endswith(".png")


def test_background_pixel_limit_is_checked_before_sanitising(app, client, db, bob, decoded):
    app.config["BW"] = replace(app.config["BW"], background_image_max_pixels=2_000_000)
    response = client.post("/settings/display/background",
                           data={"background": (io.BytesIO(_png((1500, 1500))), "bg.png")},
                           content_type="multipart/form-data", follow_redirects=True)
    assert "2 megapixels" in response.get_data(as_text=True)
    assert decoded == []
    assert db.scalar("SELECT accessibility FROM users WHERE id = ?", (bob["id"],)) is None
    assert [path for path in Path(app.config["BW"].folders.uploads).rglob("*") if path.is_file()] == []


def test_sanitising_turns_pictures_without_keeping_a_copy(app, monkeypatch):
    copies: list[int] = []
    original = Image.Image.copy

    def counting_copy(self):
        copies.append(1)
        return original(self)

    monkeypatch.setattr(Image.Image, "copy", counting_copy)
    upright = _save(app, _jpeg((60, 40)), "upright.jpg")
    turned = _save(app, _jpeg((60, 40), orientation=6), "turned.jpg")
    assert copies == []
    folder = Path(app.config["BW"].folders.uploads)
    with Image.open(folder / upright.filename) as image:
        assert image.size == (60, 40)
    with Image.open(folder / turned.filename) as image:
        assert image.size == (40, 60)
        assert 0x0112 not in image.getexif()


def test_large_jpeg_backgrounds_are_decoded_at_a_reduced_scale(app, client, db, bob, monkeypatch):
    drafts: list[tuple[int, int]] = []
    original = JpegImagePlugin.JpegImageFile.draft

    def recording_draft(self, mode, size):
        result = original(self, mode, size)
        drafts.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", recording_draft)
    edge = app.config["BW"].background_image_max_dimension
    client.post("/settings/display/background",
                data={"background": (io.BytesIO(_jpeg((edge * 2 + 80, 400))), "bg.jpg")},
                content_type="multipart/form-data")
    assert drafts and drafts[-1][0] < edge * 2  # decoded at half size, not in full
    name = json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (bob["id"],)))["background_image"]
    with Image.open(Path(app.config["BW"].folders.uploads) / name) as image:
        assert image.format == "JPEG" and image.width == edge


def test_profile_pictures_are_rate_limited_per_account(app, client, db, bob, monkeypatch):
    monkeypatch.setattr(user_routes, "IMAGE_UPLOADS", 2)

    def avatar():
        return client.post("/settings/profile", data={"avatar": (io.BytesIO(_jpeg((40, 30))), "me.jpg")},
                           content_type="multipart/form-data", follow_redirects=True)

    assert "Too many picture uploads" not in avatar().get_data(as_text=True)
    first = db.scalar("SELECT avatar_filename FROM user_profiles WHERE user_id = ?", (bob["id"],))
    assert first
    client.post("/settings/display/background", data={"background": (io.BytesIO(_jpeg((40, 30))), "bg.jpg")},
                content_type="multipart/form-data")
    assert "Too many picture uploads" in avatar().get_data(as_text=True)
    assert db.scalar("SELECT avatar_filename FROM user_profiles WHERE user_id = ?", (bob["id"],)) == first
    response = client.post("/api/accessibility/background", data={"file": (io.BytesIO(_jpeg((40, 30))), "bg.jpg")},
                           content_type="multipart/form-data")
    assert response.status_code == 429
    # Saving the rest of the profile, without a picture, is not limited.
    client.post("/settings/profile", data={"real_name": "Bob"})
    assert db.scalar("SELECT real_name FROM user_profiles WHERE user_id = ?", (bob["id"],)) == "Bob"


def test_busy_image_slot_refuses_with_a_retryable_error(app, monkeypatch):
    monkeypatch.setattr(storage, "IMAGE_SLOT_TIMEOUT", 0.01)
    assert storage._image_slot.acquire(timeout=1)
    try:
        with pytest.raises(storage.UploadError) as error:
            _save(app, _jpeg((40, 30)), "photo.jpg")
    finally:
        storage._image_slot.release()
    assert error.value.key == "upload.error.busy"
    assert in_app(app, lambda: list(storage.folder_path("uploads").iterdir())) == []
    assert _save(app, _jpeg((40, 30)), "photo.jpg").filename.endswith(".jpg")
