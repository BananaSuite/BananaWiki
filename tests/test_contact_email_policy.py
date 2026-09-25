"""Contact-email policy regressions for the post-1.4 hosting UX."""

import os

import pytest
from werkzeug.security import generate_password_hash

from hosting import config as hosting_config
from hosting.app import create_hosting_app


@pytest.fixture
def hosting_client(tmp_path):
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db

    init_hosting_db()
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application.test_client()


def test_contact_email_defaults_to_optional_without_verification(hosting_client):
    from hosting.db import get_hosting_settings

    settings = get_hosting_settings()
    assert settings["ask_email_new_signup"] == 0
    assert settings["ask_email_existing_users"] == 0
    assert settings["email_required"] == 0
    assert settings["email_verification_required"] == 0


def test_missing_email_uses_focused_dialog_and_password_confirmation(
    hosting_client,
):
    from hosting.db import create_account, get_account_by_username, update_hosting_settings

    update_hosting_settings(ask_email_existing_users=1, email_required=1)

    create_account("contactless", generate_password_hash("safe-pass-123"))
    hosting_client.post("/login", data={"username": "contactless", "password": "safe-pass-123"})

    page = hosting_client.get("/account")
    assert b"Add a contact email" in page.data
    assert b"Email verification is not required by default" in page.data

    rejected = hosting_client.post(
        "/account/contact-email",
        data={"email": "person@example.com", "current_password": "wrong"},
        follow_redirects=True,
    )
    assert b"current password is incorrect" in rejected.data
    assert not get_account_by_username("contactless")["email"]

    saved = hosting_client.post(
        "/account/contact-email",
        data={"email": "person@example.com", "current_password": "safe-pass-123"},
        follow_redirects=True,
    )
    assert b"Verification is not required" in saved.data
    assert get_account_by_username("contactless")["email"] == "person@example.com"
