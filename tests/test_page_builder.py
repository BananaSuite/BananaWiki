"""Core visual page builder behavior and security boundaries."""

import pytest
import json

import config
import db
from helpers import (
    BuilderValidationError,
    compile_builder_markdown,
    user_can_view_page,
    validate_builder_payload,
)


def _create_page(user_id, title="Builder test"):
    return db.create_page(title, "builder-test", "Original content", user_id=user_id)


def _document(*blocks):
    return {"version": 1, "blocks": list(blocks)}


def test_builder_is_disabled_by_default(logged_in_admin, admin_user):
    _create_page(admin_user)
    response = logged_in_admin.get("/page/builder-test/builder")
    assert response.status_code == 403
    assert db.get_site_settings()["page_builder_enabled"] == 0


def test_host_gate_overrides_local_enablement(logged_in_admin, admin_user, monkeypatch):
    _create_page(admin_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="admin")
    monkeypatch.setattr(config, "FORBID_PAGE_BUILDER", True)
    assert logged_in_admin.get("/page/builder-test/builder").status_code == 403


def test_admin_can_grant_builder_to_users(admin_user, logged_in_user, regular_user):
    _create_page(regular_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="user")
    response = logged_in_user.get("/page/builder-test/builder")
    assert response.status_code == 200
    assert b"Visual builder" in response.data


def test_editor_policy_rejects_regular_user(admin_user, logged_in_user, regular_user):
    _create_page(regular_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="editor")
    assert logged_in_user.get("/page/builder-test/builder").status_code == 403


def test_builder_validation_rejects_active_content_and_tracking_images():
    bad_text = _document({"type": "html", "text": "<script>alert(1)</script>"})
    try:
        validate_builder_payload(bad_text)
        pytest.fail("unknown active block should fail")
    except BuilderValidationError:
        pass

    external_image = _document({
        "type": "image",
        "url": "https://tracker.example/pixel.png",
        "alt": "",
        "caption": "",
    })
    try:
        validate_builder_payload(external_image)
        pytest.fail("external image should fail")
    except BuilderValidationError:
        pass


def test_compiler_escapes_text_and_only_emits_safe_blocks():
    markdown = compile_builder_markdown(_document(
        {"type": "heading", "level": 2, "text": "<img onerror=alert(1)>"},
        {"type": "button", "label": "Open", "url": "/page/home", "style": "primary"},
        {"type": "image", "url": "/static/uploads/0123456789abcdef0123456789abcdef.png", "alt": "A", "caption": "C"},
    ))
    assert "onerror=" in markdown
    assert "&lt;img onerror=alert(1)&gt;" in markdown
    assert '<a class="builder-button builder-button-primary"' in markdown
    assert "javascript:" not in markdown


def test_publish_persists_builder_revision_and_clears_draft(logged_in_admin, admin_user):
    page_id = _create_page(admin_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="admin")
    page = db.get_page(page_id)
    document = _document(
        {"type": "heading", "level": 1, "text": "Built safely"},
        {"type": "text", "text": "No scripts here."},
        {"type": "youtube", "url": "https://youtu.be/dQw4w9WgXcQ", "caption": "Demo"},
    )
    draft = logged_in_admin.post(
        "/api/page/builder-test/builder/draft",
        json={"document": document, "base_edited_at": page["last_edited_at"]},
    )
    assert draft.status_code == 200
    assert db.get_page_builder_draft(page_id, admin_user)

    response = logged_in_admin.post(
        "/api/page/builder-test/builder/publish",
        json={
            "document": document,
            "title": "Built page",
            "base_edited_at": page["last_edited_at"],
            "edit_message": "Visual refresh",
            "public": False,
        },
    )
    assert response.status_code == 200
    updated = db.get_page(page_id)
    assert updated["title"] == "Built page"
    saved_document = json.loads(updated["builder_json"])
    assert saved_document["blocks"][:2] == document["blocks"][:2]
    assert saved_document["blocks"][2]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert updated["builder_public"] == 0
    assert "builder-page" in updated["content"]
    assert db.get_page_builder_draft(page_id, admin_user) is None
    history = db.get_page_history(page_id)
    assert history[0]["builder_json"] == updated["builder_json"]


def test_publish_detects_concurrent_page_change(logged_in_admin, admin_user):
    page_id = _create_page(admin_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="admin")
    stale_timestamp = db.get_page(page_id)["last_edited_at"]
    db.update_page(page_id, "Changed elsewhere", "new", admin_user)
    response = logged_in_admin.post(
        "/api/page/builder-test/builder/publish",
        json={
            "document": _document({"type": "text", "text": "Mine"}),
            "base_edited_at": stale_timestamp,
        },
    )
    assert response.status_code == 409


def test_builder_pages_are_private_to_anonymous_users_by_default(monkeypatch):
    """Direct policy test avoids coupling this boundary to login redirect rendering."""
    monkeypatch.setattr(config, "FORBID_PUBLIC_MODE", False)
    monkeypatch.setattr(config, "FORBID_PUBLIC_BUILDER_PAGES", True)
    db.update_site_settings(public_mode=1, setup_done=1)
    page_id = db.create_page(
        "Private build",
        "private-build",
        "content",
        builder_json=json.dumps(_document({"type": "text", "text": "Private"})),
        builder_public=True,
    )
    assert user_can_view_page(None, db.get_page(page_id)) is False


def test_public_builder_page_requires_explicit_page_and_host_grant(monkeypatch):
    monkeypatch.setattr(config, "FORBID_PUBLIC_MODE", False)
    monkeypatch.setattr(config, "FORBID_PUBLIC_BUILDER_PAGES", False)
    db.update_site_settings(public_mode=1)
    page_id = db.create_page(
        "Public build",
        "public-build",
        "content",
        builder_json=json.dumps(_document({"type": "text", "text": "Public"})),
        builder_public=True,
    )
    assert user_can_view_page(None, db.get_page(page_id)) is True


def test_non_builder_update_preserves_private_builder_metadata(monkeypatch):
    monkeypatch.setattr(config, "FORBID_PUBLIC_MODE", False)
    db.update_site_settings(public_mode=1)
    document = json.dumps(_document({"type": "text", "text": "Private"}))
    page_id = db.create_page(
        "Private build", "private-update", "content",
        builder_json=document, builder_public=False,
    )
    db.update_page(page_id, "Renamed build", "content", None, "Title-only update")
    updated = db.get_page(page_id)
    assert updated["builder_json"] == document
    assert updated["builder_public"] == 0
    assert user_can_view_page(None, updated) is False


def test_anonymous_search_does_not_leak_private_builder_page(client, monkeypatch):
    monkeypatch.setattr(config, "FORBID_PUBLIC_MODE", False)
    db.update_site_settings(public_mode=1, setup_done=1)
    db.create_page(
        "Secret builder landing", "secret-builder", "Unique private phrase",
        builder_json=json.dumps(_document({"type": "text", "text": "Private"})),
        builder_public=False,
    )
    title_search = client.get("/api/pages/search?q=Secret")
    sidebar_search = client.get("/api/sidebar/search?q=Unique&scope=content")
    advanced_search = client.get("/search?q=Unique")
    assert title_search.status_code == 200 and title_search.get_json() == []
    assert sidebar_search.status_code == 200 and sidebar_search.get_json()["pages"] == []
    assert b"Secret builder landing" not in advanced_search.data
    assert b"Unique private phrase" not in advanced_search.data


def test_stale_autosave_cannot_reappear_after_publish(logged_in_admin, admin_user):
    page_id = _create_page(admin_user)
    db.update_site_settings(page_builder_enabled=1, page_builder_access="admin")
    old_timestamp = db.get_page(page_id)["last_edited_at"]
    db.update_page(
        page_id, "Published", "new", admin_user,
        builder_json=json.dumps(_document({"type": "text", "text": "New"})),
    )
    response = logged_in_admin.post(
        "/api/page/builder-test/builder/draft",
        json={
            "document": _document({"type": "text", "text": "Stale"}),
            "base_edited_at": old_timestamp,
        },
    )
    assert response.status_code == 409
    assert db.get_page_builder_draft(page_id, admin_user) is None


def test_builder_draft_image_is_retained_by_orphan_cleanup(admin_user):
    page_id = _create_page(admin_user)
    filename = "0123456789abcdef0123456789abcdef.png"
    document = json.dumps(_document({
        "type": "image",
        "url": f"/static/uploads/{filename}",
        "alt": "Draft image",
        "caption": "",
    }))
    db.save_page_builder_draft(page_id, admin_user, document)
    assert filename in db.get_all_referenced_image_filenames()
