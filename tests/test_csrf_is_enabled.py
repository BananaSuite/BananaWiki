"""Cross-site request forgery protection must be on in the shipped application.

Almost every other test runs with CSRF switched off, because supplying a token
on every request would drown the assertions. That leaves a blind spot: if the
protection were disabled in the application itself, the whole suite would still
pass. These two tests look at the application as it ships, not as tests
configure it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_shipped_application_has_csrf_protection_switched_on():
    """Import the app in a clean process, where no fixture has touched it."""
    probe = (
        "import app as module; "
        "value = module.app.config.get('WTF_CSRF_ENABLED'); "
        "extension = 'csrf' in module.app.extensions; "
        "print(repr((value, extension)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    enabled, registered = eval(result.stdout.strip().splitlines()[-1])
    assert registered, "CSRFProtect is not registered on the application"
    assert enabled is not False, (
        "the application ships with WTF_CSRF_ENABLED set to False, so every "
        "state-changing form would accept a request from any other site"
    )


def test_a_form_post_without_a_token_does_not_take_effect(client, admin_user):
    """The registered extension has to actually reject an untokened POST.

    A rejection here is a redirect with a flash, not a 400: the application
    handles CSRFError that way on purpose. What matters is that the post did
    not do anything, so the correct credentials must not produce a session.
    """
    from app import app

    previous = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post("/login", data={"username": "admin", "password": "admin123"})
        with client.session_transaction() as stored:
            logged_in = stored.get("user_id")
    finally:
        app.config["WTF_CSRF_ENABLED"] = previous

    assert response.status_code != 200, "the form was accepted and re-rendered"
    assert logged_in is None, (
        "a login POST carrying no CSRF token created a session, so cross-site "
        "form protection is not being enforced"
    )
