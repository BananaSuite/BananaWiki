import pytest
from flask import session
from app import app
import db

def test_guest_session_language(client):
    """Test that a guest can set their language via session."""
    with client.session_transaction() as sess:
        sess['interface_language'] = 'it'

    # Home redirects to /setup if not set up
    response = client.get('/setup')
    assert b'lang="it"' in response.data

def test_site_default_wins_over_accept_language(client):
    """Browser's Accept-Language must NOT override the configured site default.

    A guest with an Italian browser visiting a wiki configured with
    English as the site default should see English, because the
    admin-configured site language is the source of truth when the
    visitor has not chosen a language themselves.
    """
    # Default site language is "en" (DB default).  Italian browser
    # should still see English.
    headers = {'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7'}
    response = client.get('/setup', headers=headers)
    assert b'lang="en"' in response.data

    # When the admin sets the site default to Italian, every visitor
    # should see Italian regardless of their browser language.
    db.update_site_settings(interface_language='it')
    headers = {'Accept-Language': 'en-US,en;q=0.9,it;q=0.8'}
    response = client.get('/setup', headers=headers)
    assert b'lang="it"' in response.data

def test_language_priority(client):
    """Test the priority: Session > Site default."""
    # Site default is Italian, but session says English.
    db.update_site_settings(interface_language='it')
    with client.session_transaction() as sess:
        sess['interface_language'] = 'en'

    response = client.get('/setup')
    assert b'lang="en"' in response.data
