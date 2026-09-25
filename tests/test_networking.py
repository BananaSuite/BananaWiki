"""
Tests for BananaWiki networking configuration.
"""

import importlib
import importlib.util
import os
import sys

import pytest

# Ensure the project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

_GUNICORN_CONF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "gunicorn.conf.py"
)


def _load_gunicorn_conf():
    """Load gunicorn.conf.py as a module (dot in filename prevents normal import)."""
    spec = importlib.util.spec_from_file_location("gunicorn_conf", _GUNICORN_CONF_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


# -----------------------------------------------------------------------
# Config defaults
# -----------------------------------------------------------------------
def test_default_port():
    assert config.PORT == 5001


def test_default_host_public():
    """Default bind is localhost (for use behind a reverse proxy)."""
    assert config.HOST == "127.0.0.1"


def test_host_localhost_default():
    """HOST defaults to 127.0.0.1 for use behind nginx."""
    assert config.HOST == "127.0.0.1"


def test_gunicorn_binds_localhost_when_not_public(monkeypatch):
    """Gunicorn binds to 127.0.0.1 when HOST is set to localhost."""
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    mod = _load_gunicorn_conf()
    assert mod.bind == f"127.0.0.1:{config.PORT}"


def test_gunicorn_bind_ipv6_host(monkeypatch):
    """Gunicorn bind address brackets an IPv6 HOST (e.g. :: or ::1)."""
    monkeypatch.setattr(config, "HOST", "::")
    mod = _load_gunicorn_conf()
    assert mod.bind == f"[::]:{config.PORT}"


def test_gunicorn_bind_ipv6_loopback(monkeypatch):
    """Gunicorn bind address brackets IPv6 loopback (::1)."""
    monkeypatch.setattr(config, "HOST", "::1")
    mod = _load_gunicorn_conf()
    assert mod.bind == f"[::1]:{config.PORT}"


def test_gunicorn_bind_ipv6_already_bracketed(monkeypatch):
    """Gunicorn bind address does not double-bracket an already-bracketed IPv6 HOST."""
    monkeypatch.setattr(config, "HOST", "[::1]")
    mod = _load_gunicorn_conf()
    assert mod.bind == f"[::1]:{config.PORT}"


def test_default_proxy_mode():
    assert config.PROXY_MODE is False


# -----------------------------------------------------------------------
# Gunicorn config reads from config.py
# -----------------------------------------------------------------------
def test_gunicorn_conf_bind():
    """gunicorn.conf.py bind address matches config."""
    mod = _load_gunicorn_conf()
    assert mod.bind == f"{config.HOST}:{config.PORT}"


def test_gunicorn_conf_no_ssl_by_default():
    """SSL is not configured in gunicorn.conf.py when config has no certs."""
    mod = _load_gunicorn_conf()
    assert not hasattr(mod, "certfile")
    assert not hasattr(mod, "keyfile")


def test_static_uploads_served_from_configured_folder(client, tmp_path, monkeypatch):
    upload_dir = tmp_path / "runtime_uploads"
    upload_dir.mkdir()
    (upload_dir / "probe.txt").write_text("served from runtime dir", encoding="utf-8")
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_dir))
    import db as db_mod
    from helpers._passwords import generate_password_hash
    db_mod.update_site_settings(setup_done=1)
    db_mod.create_user("uploader", generate_password_hash("pass1234"), role="owner")

    # Anonymous visitors are bounced to login: uploads are private content.
    anon = client.get("/static/uploads/probe.txt")
    assert anon.status_code in (302, 303)
    assert anon.headers["Location"].endswith("/login")

    # An authenticated session can read the uploaded file.
    login = client.post("/login", data={"username": "uploader", "password": "pass1234"})
    assert login.status_code in (302, 303)
    rv = client.get("/static/uploads/probe.txt")

    assert rv.status_code == 200
    assert rv.data == b"served from runtime dir"

    # Public access ("open access") mode lets anonymous visitors read files.
    db_mod.update_site_settings(public_mode=1)
    from app import app
    anon_client = app.test_client()
    anon_get = anon_client.get("/static/uploads/probe.txt")
    assert anon_get.status_code == 200
    assert anon_get.data == b"served from runtime dir"



def test_gunicorn_conf_proxy_forwarded(monkeypatch):
    """forwarded_allow_ips trusts only loopback (IPv4 + IPv6) regardless of
    PROXY_MODE: defence in depth so nginx on the same host can forward
    headers but no remote address can."""
    monkeypatch.setattr(config, "PROXY_MODE", True)
    mod = _load_gunicorn_conf()
    assert mod.forwarded_allow_ips == "127.0.0.1,::1"


def test_gunicorn_conf_proxy_forwarded_false(monkeypatch):
    """forwarded_allow_ips still trusts loopback only when PROXY_MODE is False."""
    monkeypatch.setattr(config, "PROXY_MODE", False)
    mod = _load_gunicorn_conf()
    assert mod.forwarded_allow_ips == "127.0.0.1,::1"


# -----------------------------------------------------------------------
# ProxyFix applied when PROXY_MODE is True
# -----------------------------------------------------------------------
def test_proxy_fix_applied_when_enabled():
    """A deployment must explicitly opt into trusted proxy headers."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", "from app import app; from werkzeug.middleware.proxy_fix import ProxyFix; assert isinstance(app.wsgi_app, ProxyFix)"],
        env={**os.environ, "BW_PROXY_MODE": "1"}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_proxy_fix_disabled_by_default():
    from app import app
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert not isinstance(app.wsgi_app, ProxyFix)


# -----------------------------------------------------------------------
# WSGI entry point
# -----------------------------------------------------------------------
def test_wsgi_exports_app():
    """wsgi.py exposes the Flask app."""
    import wsgi
    from flask import Flask
    assert isinstance(wsgi.app, Flask)


# -----------------------------------------------------------------------
# App responds on configured host/port
# -----------------------------------------------------------------------
def test_app_serves_http(client):
    """App responds to HTTP requests via the test client."""
    from werkzeug.security import generate_password_hash
    import db
    db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.get("/")
    assert resp.status_code == 200
