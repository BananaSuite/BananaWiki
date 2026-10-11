"""Application factory for the hosting portal."""

from __future__ import annotations

import html
import json
import logging
import os
import time
import uuid
from datetime import timedelta
from typing import Any

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.datastructures import LanguageAccept
from werkzeug.exceptions import HTTPException
from werkzeug.http import parse_accept_header, parse_cookie
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__
from ..core import logformat, web
from ..core.assets import SharedAssetsFlask, asset_url
from ..core.i18n import FALLBACK, Catalog
from ..core.json import SafeJSONProvider
from ..core.ratelimit import MemoryLimiter
from ..core.sqlite import Database, DatabaseUnavailable, is_unavailable
from . import auth, i18n, settings, templating, wikihosts
from .config import DEFAULT_SERVICE_NAME, PACKAGE_ROOT, HostingConfig, load_config
from .db import EXTENSION as DB_EXTENSION
from .db import close_request_session, connection_scope, open_database
from .runtime import Runtime, load_runtime

log = logging.getLogger("bananawiki.hosting")

RUNTIME_EXTENSION = "bananawiki.hosting.runtime"
GLOBAL_RATE_LIMIT = 300
MAX_BODY = 32 * 1024 * 1024
SESSION_COOKIE = "bwh_session"
SECURE_SESSION_COOKIE = "__Host-" + SESSION_COOKIE


class _SessionInterface(SecureCookieSessionInterface):
    """Signed session cookie; ``Secure`` follows the request scheme.

    Over HTTPS the cookie is ``__Host-bwh_session``. The portal and the wikis
    are sibling subdomains, and a wiki controls its own responses (its admins
    may run plugins), so it could set a ``bwh_session`` cookie for the parent
    domain on a narrower path and swap a visitor's portal session on pages
    such as ``/oauth/authorize``. Browsers refuse a ``__Host-`` cookie that
    has a Domain or a path other than ``/``, so a wiki cannot plant one. The
    plain 1.4 name is kept only for plain-HTTP portals (port mode, local use).
    """

    def get_cookie_secure(self, app: Flask) -> bool:
        try:
            return request.is_secure
        except RuntimeError:
            return False

    def get_cookie_name(self, app: Flask) -> str:
        return SECURE_SESSION_COOKIE if self.get_cookie_secure(app) else SESSION_COOKIE


# Display preferences the maintenance page reads without a session: the portal keeps them in step (see _after).
THEME_COOKIE = "bw_theme"
LANGUAGE_COOKIE = "bw_lang"


class _MaintenanceGate:
    """503 for everything but ``/health`` while the updater's marker file exists."""

    def __init__(self, wsgi_app: Any, path: str, service_name: str = DEFAULT_SERVICE_NAME):
        self.wsgi_app = wsgi_app
        self.path = path
        self.pages = _maintenance_pages(service_name)

    def __call__(self, environ: dict, start_response: Any):
        if self.path and os.path.exists(self.path) and environ.get("PATH_INFO") not in ("/health", "/healthz"):
            # Browsers get a small page in their language and theme (the portal's display-preference cookies,
            # else the browser's own settings); API clients and scripts keep the plain text.
            if "text/html" in environ.get("HTTP_ACCEPT", ""):
                cookies = parse_cookie(environ.get("HTTP_COOKIE", ""))
                lang = cookies.get(LANGUAGE_COOKIE, "")
                if lang not in self.pages:
                    accepted = parse_accept_header(environ.get("HTTP_ACCEPT_LANGUAGE", ""), LanguageAccept)
                    # As i18n.current_language(): ties (a bare "*") go to English, offered first.
                    lang = accepted.best_match(sorted(self.pages, key=lambda code: code != FALLBACK)) or FALLBACK
                theme = cookies.get(THEME_COOKIE, "")
                body = self.pages[lang].replace(b"<html ", f'<html data-theme="{theme}" '.encode(), 1) \
                    if theme in ("dark", "light") else self.pages[lang]
                kind = "text/html; charset=utf-8"
            else:
                body, kind = b"Maintenance is in progress. Please retry shortly.\n", "text/plain; charset=utf-8"
            start_response("503 Service Unavailable", [
                ("Content-Type", kind), ("Content-Length", str(len(body))),
                ("Retry-After", "30"), ("Cache-Control", "no-store"),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                                            "form-action 'none'; frame-ancestors 'none'"),
                ("X-Content-Type-Options", "nosniff"),
            ])
            return [body]
        return self.wsgi_app(environ, start_response)


def _maintenance_pages(service_name: str = DEFAULT_SERVICE_NAME) -> dict[str, bytes]:
    """The maintenance page in each portal language, built once from the bundled catalogues.

    The gate runs while the updater replaces the application: it reads no database and renders no
    template, so the page is plain markup with its own small stylesheet.
    """
    catalogues: dict[str, dict[str, str]] = {}
    for lang in i18n.LANGUAGES:
        try:
            catalogues[lang] = json.loads((PACKAGE_ROOT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalogues[lang] = {}
    fallback = {"hosting.maintenance.title": "Maintenance in progress",
                "hosting.maintenance.body": "The platform is being updated. Try again in a minute."}
    service = html.escape(service_name)
    pages: dict[str, bytes] = {}
    light = "--bg:#f7f8fa;--card:#fff;--text:#252a33;--muted:#5c616b;--line:#d9dce1;color-scheme:light"
    dark = "--bg:#16161f;--card:#1e1e2c;--text:#c8ccd8;--muted:#a3a8b6;--line:#3e414e;color-scheme:dark"
    for lang, strings in catalogues.items():
        text = {key: html.escape(str(strings.get(key) or catalogues["en"].get(key) or default))
                for key, default in fallback.items()}
        # Dark like the portal; light when the visitor chose it there (data-theme, set by the gate from the
        # portal's cookie) or, without a choice, when the browser asks for it. Retry-After is 30 seconds:
        # the page looks again by itself.
        pages[lang] = (
            f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex"><meta name="color-scheme" content="dark light">'
            '<meta http-equiv="refresh" content="30">'
            f"<title>{text['hosting.maintenance.title']} · {service}</title><style>"
            f":root{{{dark}}}:root[data-theme=light]{{{light}}}"
            f"@media (prefers-color-scheme:light){{:root:not([data-theme]){{{light}}}}}"
            "*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:3rem 1rem;"
            "background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,"
            "sans-serif}main{max-width:34rem;margin:0 auto;background:var(--card);border:1px solid var(--line);"
            "border-radius:6px;padding:1.5rem 1.75rem}h1{margin:0 0 .5rem;font-size:1.5rem;line-height:1.25}"
            "p{margin:.5rem 0}main>:last-child{margin-bottom:0}.muted{color:var(--muted);font-size:.9rem}"
            "</style></head><body><main>"
            f"<h1>{text['hosting.maintenance.title']}</h1><p>{text['hosting.maintenance.body']}</p>"
            f'<p class="muted">{service}</p></main></body></html>\n'
        ).encode()
    return pages


def _configure_logging(cfg: HostingConfig) -> None:
    root = logging.getLogger("bananawiki.hosting")
    if getattr(root, "_bw_configured", False):
        return
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logformat.formatter(cfg.log_format))
        root.addHandler(handler)
    root._bw_configured = True  # type: ignore[attr-defined]


def create_app(config: HostingConfig | None = None, *, runtime: Runtime | None = None, **overrides: Any) -> Flask:
    cfg = config or load_config(**overrides)
    _configure_logging(cfg)
    app = SharedAssetsFlask("bananawiki.hosting", root_path=str(PACKAGE_ROOT), template_folder="templates",
                           static_folder="static", static_url_path="/static")
    app.json = SafeJSONProvider(app)
    app.config["HOSTING"] = cfg
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_NAME=SESSION_COOKIE,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=auth.SESSION_DAYS),
        MAX_CONTENT_LENGTH=max(MAX_BODY, cfg.archives.import_chunk_bytes + 1024 * 1024),
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        TESTING=cfg.testing,
        PREFERRED_URL_SCHEME=cfg.preferred_url_scheme or ("https" if cfg.proxy_mode else "http"),
    )
    app.session_interface = _SessionInterface()
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    # Inside ProxyFix: it needs the public Host and scheme.
    wikihosts.install(app)
    if cfg.proxy_mode:
        # X-Forwarded-Prefix is not trusted: Caddy does not strip it (audit).
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    if cfg.maintenance_file:
        app.wsgi_app = _MaintenanceGate(app.wsgi_app, cfg.maintenance_file,  # type: ignore[method-assign]
                                        cfg.service_name)

    database = open_database(cfg)
    database.initialize()
    app.extensions[DB_EXTENSION] = database
    app.extensions[i18n.EXTENSION] = Catalog([PACKAGE_ROOT / "translations"])
    app.extensions[RUNTIME_EXTENSION] = runtime if runtime is not None else load_runtime()
    app.extensions["bananawiki.hosting.limiter"] = MemoryLimiter()
    with app.app_context(), connection_scope(database):
        settings.encrypt_legacy_secrets(cfg.secret_key)

    from .routes import register

    register(app)
    app.teardown_appcontext(close_request_session)
    _install_pipeline(app)
    templating.install(app)
    _install_core_routes(app, database)
    _install_error_handlers(app)
    _startup_warnings(cfg)
    log.info("BananaWiki Hosting %s ready", __version__)
    return app


def _startup_warnings(cfg: HostingConfig) -> None:
    if cfg.testing:
        return
    from .config import CONTACT_EMAIL_PLACEHOLDER

    if cfg.contact_email == CONTACT_EMAIL_PLACEHOLDER:
        log.warning("HOSTING_CONTACT_EMAIL is not set; users are told to write to %s.", CONTACT_EMAIL_PLACEHOLDER)
    if cfg.hosting_mode == "port" and cfg.public_scheme == "http":
        log.warning("HOSTING_PUBLIC_SCHEME is 'http' in port mode: wiki URLs are unencrypted.")
    if cfg.tenant_network_outbound:
        log.warning("Tenant containers have outbound network access (HOSTING_TENANT_NETWORK=outbound).")


def is_api_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/") or path.startswith("/api/")


def _install_pipeline(app: Flask) -> None:
    def bearer_request() -> bool:
        return is_api_path(request.path)

    web.install_csrf(app, exempt_when=bearer_request)

    @app.before_request
    def _pipeline():
        g.request_id = uuid.uuid4().hex[:16]
        g.started = time.perf_counter()
        if request.endpoint in ("static", "health", "healthz", "favicon_ico") or (
                request.endpoint is None and not is_api_path(request.path)):
            return None
        limiter: MemoryLimiter = app.extensions["bananawiki.hosting.limiter"]
        if not limiter.hit(f"g:{web.client_network()}", GLOBAL_RATE_LIMIT, 60):
            if auth.wants_json():
                return jsonify({"error": "Too many requests."}), 429
            return render_template("hosting/error.html", code=429), 429
        if is_api_path(request.path):
            if not settings.flag("api_enabled"):
                return _api_not_found()
            return None
        view = app.view_functions.get(request.endpoint or "")
        auth.load()
        if g.get("suspended_redirect") and request.endpoint != "auth.account_suspended":
            return redirect(url_for("auth.account_suspended"))
        account = auth.current_account()
        if account is None:
            if auth.is_public(view):
                return None
            return auth.redirect_to_login()
        target = auth.pending_gate(account, view)
        if target and request.endpoint != target:
            if auth.wants_json():
                return jsonify({"error": "account_action_required"}), 403
            return redirect(url_for(target))
        return None

    @app.after_request
    def _after(response: Response) -> Response:
        policy: web.SecurityPolicy = app.extensions.setdefault(
            "bananawiki.hosting.csp",
            web.SecurityPolicy(frame_src=[], img_src=["'self'", "data:"], media_src=["'self'"],
                               frame_ancestors=["'none'"]),
        )
        web.apply_security_headers(response, policy, hsts=app.config["HOSTING"].is_production)
        if response.mimetype == "text/html" and request.endpoint not in (None, "static"):
            _sync_display_cookies(response)
        if (g.get("account") or is_api_path(request.path)) and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "private, no-store"
        response.headers.setdefault("X-Request-ID", g.get("request_id", ""))
        return response


def _sync_display_cookies(response: Response) -> None:
    """Keep the theme and language the visitor chose in plain cookies, for the maintenance page.

    They only steer how pages look (the session cookie stays the authority): the gate in front of the
    application reads them while the application itself is being replaced.
    """
    secure = request.is_secure
    theme = templating.theme()
    account = auth.current_account()
    chosen_theme = (account or {}).get("theme_mode") if account else request.cookies.get(THEME_COOKIE)
    if chosen_theme in ("dark", "light") and request.cookies.get(THEME_COOKIE) != theme:
        response.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400, samesite="Lax", secure=secure)
    language = session.get("interface_language")
    if language in i18n.LANGUAGES and request.cookies.get(LANGUAGE_COOKIE) != language:
        response.set_cookie(LANGUAGE_COOKIE, language, max_age=365 * 86400, samesite="Lax", secure=secure,
                            httponly=True)
    elif language not in i18n.LANGUAGES and request.cookies.get(LANGUAGE_COOKIE):
        response.delete_cookie(LANGUAGE_COOKIE, samesite="Lax", secure=secure, httponly=True)


def _api_not_found():
    response = jsonify(error="Not found.")
    response.status_code = 404
    response.headers["Cache-Control"] = "no-store"
    return response


def _install_core_routes(app: Flask, database: Database) -> None:
    @app.get("/health")
    @app.get("/healthz", endpoint="healthz")
    @auth.public
    def health():
        try:
            conn = database.connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as error:  # noqa: BLE001 - never reveal storage details to the public
            log.error("Health check failed: %s", error)
            return jsonify({"status": "unavailable"}), 503
        return jsonify({"status": "ok"})

    @app.get("/favicon.ico")
    @auth.public
    def favicon_ico():
        """Browsers ask for this where a page names no icon: the banana every page links."""
        return redirect(asset_url("favicon.png"))


def _install_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException):
        code = error.code or 500
        if auth.wants_json() or is_api_path(request.path):
            response = jsonify({"error": error.description if code != 404 else "Not found."})
            if code == 405 and getattr(error, "valid_methods", None):
                response.headers["Allow"] = ", ".join(sorted(error.valid_methods))  # type: ignore[union-attr]
            return response, code
        if code == 400 and request.method == "POST" and not web.csrf_valid():
            auth.flash_t("hosting.csrf_expired", "error")
            return redirect(request.referrer if web.is_safe_redirect(request.referrer or "") else url_for("public.index"))
        try:
            return render_template("hosting/error.html", code=code), code
        except Exception:  # noqa: BLE001 - the error page itself must never fail
            return Response(f"{code} {error.name}", status=code, mimetype="text/plain")

    @app.errorhandler(Exception)
    def _unexpected(error: Exception):
        if isinstance(error, HTTPException):
            return _http_error(error)
        if isinstance(error, DatabaseUnavailable) or is_unavailable(error):
            log.error("Storage unavailable: %s", error)
            code = 503
        else:
            log.exception("Unhandled error on %s %s (request %s)", request.method, request.path, g.get("request_id"))
            code = 500
        if auth.wants_json() or is_api_path(request.path):
            return jsonify({"error": "Internal error." if code == 500 else "Service unavailable.",
                            "request_id": g.get("request_id")}), code
        try:
            return render_template("hosting/error.html", code=code), code
        except Exception:  # noqa: BLE001
            return Response(f"{code}", status=code, mimetype="text/plain")


__all__ = ["create_app"]
