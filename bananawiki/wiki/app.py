"""Application factory for the wiki."""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import BadRequest, HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__
from ..core import web
from ..core.assets import SharedAssetsFlask
from ..core.i18n import Catalog
from ..core.ratelimit import MemoryLimiter
from ..core.sqlite import Database, DatabaseUnavailable, is_unavailable
from . import auth, i18n, migrations, registry, settings
from .config import PACKAGE_ROOT, Config, load_config
from .db import close_request_session, connection_scope

log = logging.getLogger("bananawiki")

GLOBAL_RATE_LIMIT = 300  # requests per minute per client, per worker
MAX_JSON_BYTES = 2 * 1024 * 1024


class _SessionInterface(SecureCookieSessionInterface):
    """Signed-cookie sessions (compatible with 1.4 cookies).

    ``Secure`` follows the actual request scheme unless ``BW_SECURE_COOKIES``
    forces it; "remember me" sessions last longer than ordinary ones.
    """

    def get_cookie_secure(self, app: Flask) -> bool:
        forced = app.config["BW"].secure_cookies
        if forced is not None:
            return forced
        try:
            return request.is_secure
        except RuntimeError:
            return False

    def get_expiration_time(self, app: Flask, session_obj: Any):
        if session_obj.permanent and session_obj.get("_remember_me"):
            from datetime import UTC, datetime

            return datetime.now(UTC) + timedelta(days=app.config["BW"].remember_me_days)
        return super().get_expiration_time(app, session_obj)


class _MaintenanceGate:
    """Answer 503 while the operator's maintenance file exists (used during updates)."""

    def __init__(self, wsgi_app: Any, path: str):
        self.wsgi_app = wsgi_app
        self.path = path

    def __call__(self, environ: dict, start_response: Any):
        if self.path and os.path.exists(self.path) and not environ.get("PATH_INFO", "").startswith("/health"):
            body = b"BananaWiki is being updated. Please try again in a minute.\n"
            start_response("503 Service Unavailable", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Retry-After", "30"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        return self.wsgi_app(environ, start_response)


def _apply_resource_limits(cfg: Config) -> None:
    if os.name != "posix" or not (cfg.memory_limit_mb or cfg.nofile_limit):
        return
    import resource

    try:
        if cfg.memory_limit_mb:
            ceiling = cfg.memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        if cfg.nofile_limit:
            _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = cfg.nofile_limit if hard == resource.RLIM_INFINITY else min(cfg.nofile_limit, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))
    except (OSError, ValueError) as error:
        log.warning("Could not apply resource limits: %s", error)


def _configure_logging(cfg: Config) -> None:
    root = logging.getLogger("bananawiki")
    if getattr(root, "_bw_configured", False):
        return
    level = {"off": logging.CRITICAL + 1, "minimal": logging.WARNING, "medium": logging.INFO,
             "verbose": logging.INFO, "debug": logging.DEBUG}[cfg.log_level]
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if cfg.log_level != "off" and not cfg.testing:
        from logging.handlers import RotatingFileHandler

        try:
            Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(cfg.log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError as error:
            root.warning("Cannot write the log file %s: %s", cfg.log_file, error)
    root.propagate = False
    root._bw_configured = True  # type: ignore[attr-defined]


def open_database(cfg: Config) -> Database:
    return Database(
        cfg.database_path,
        application_id=migrations.APPLICATION_ID,
        migrations=migrations.MIGRATIONS,
        baseline=migrations.BASELINE,
        bootstrap=migrations.bootstrap,
        busy_timeout_ms=cfg.db_busy_timeout_ms,
        cache_kib=cfg.db_cache_kib,
        synchronous=cfg.db_synchronous,
    )


def discover_features() -> list[registry.Feature]:
    from . import features as package

    found = []
    for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        mod = importlib.import_module(f"{package.__name__}.{module.name}")
        feature = getattr(mod, "FEATURE", None)
        if isinstance(feature, registry.Feature):
            feature.package_dir = Path(mod.__file__).parent  # type: ignore[arg-type]
            found.append(feature)
    return found


def create_app(config: Config | None = None, **overrides: Any) -> Flask:
    cfg = config or load_config(**overrides)
    _configure_logging(cfg)
    _apply_resource_limits(cfg)

    app = SharedAssetsFlask(
        "bananawiki.wiki",
        root_path=str(PACKAGE_ROOT),
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["BW"] = cfg
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_NAME=cfg.session_cookie_name,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=cfg.session_days),
        MAX_CONTENT_LENGTH=max(cfg.max_content_length, cfg.max_attachment_size, cfg.max_custom_page_video_size,
                               cfg.max_import_size if not cfg.managed_hosting else 0),
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=not cfg.is_production,
        JSON_SORT_KEYS=False,
        TESTING=cfg.testing,
        PREFERRED_URL_SCHEME=cfg.preferred_url_scheme or ("https" if cfg.proxy_mode else "http"),
    )
    app.session_interface = _SessionInterface()
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    if cfg.proxy_mode:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    if cfg.maintenance_file:
        app.wsgi_app = _MaintenanceGate(app.wsgi_app, cfg.maintenance_file)  # type: ignore[method-assign]

    # Storage and schema ------------------------------------------------
    from . import takeover

    takeover.relocate_legacy_folders(cfg)
    database = open_database(cfg)
    database.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, database, conn))
    app.extensions["bananawiki.database"] = database

    # Translations and features ----------------------------------------
    catalog = Catalog([PACKAGE_ROOT / "translations"], custom_dir=os.path.join(cfg.instance_dir, "translations"))
    app.extensions["bananawiki.i18n"] = catalog
    reg = registry.Registry()
    app.extensions["bananawiki.registry"] = reg
    for feature in discover_features():
        reg.add(feature)
        if feature.package_dir and (feature.package_dir / "translations").is_dir():
            catalog.add_directory(feature.package_dir / "translations")
        for blueprint in feature.blueprints:
            app.register_blueprint(blueprint)
        if feature.init_app:
            feature.init_app(app)
    from . import plugins_external

    plugins_external.load(app, reg, catalog)

    app.teardown_appcontext(close_request_session)
    _install_request_pipeline(app, cfg)
    _install_template_globals(app)
    _install_core_routes(app)
    _install_error_handlers(app)

    with app.app_context(), connection_scope(database):
        _seed_feature_rows(reg)

    app.extensions["bananawiki.scheduler"] = registry.Scheduler(app)
    app.extensions["bananawiki.limiter"] = MemoryLimiter()
    log.info("BananaWiki %s ready (database schema %s)", __version__, migrations.LATEST)
    return app


def _seed_feature_rows(reg: registry.Registry) -> None:
    """Make sure every switchable feature has its row in ``plugins``."""
    from ..core.timeutil import now_sql
    from .db import db

    existing = set(db.column("SELECT id FROM plugins"))
    for feature in reg.ordered():
        if feature.toggle == "plugin" and feature.id not in existing:
            db.execute(
                "INSERT OR IGNORE INTO plugins (id, name, version, author, description, builtin, enabled, "
                "installed_at, enabled_at) VALUES (?, ?, ?, 'BananaWiki', '', 1, ?, ?, ?)",
                (feature.id, feature.id, feature.version, 1 if feature.default_enabled else 0, now_sql(),
                 now_sql() if feature.default_enabled else None),
            )


# ── Request pipeline ─────────────────────────────────────────────────────────


def _view() -> Any:
    from flask import current_app

    return current_app.view_functions.get(request.endpoint or "")


def _install_request_pipeline(app: Flask, cfg: Config) -> None:
    @app.before_request
    def _limit_json_body():
        # CSRF may parse JSON before the authentication pipeline. Bound that
        # read as well as endpoint reads, including chunked request streams.
        if request.is_json:
            limit = min(app.config["MAX_CONTENT_LENGTH"], MAX_JSON_BYTES)
            request.max_content_length = limit
            if request.content_length is not None and request.content_length > limit:
                abort(413)
            if request.content_length is None or request.environ.get("wsgi.input_terminated"):
                # LimitedStream may return a truncated body at its boundary.
                # Read at most one extra byte, then cache only an allowed body,
                # so oversized JSON answers 413 before it reaches the decoder.
                request.max_content_length = limit + 1
                try:
                    if len(request.get_data()) > limit:
                        abort(413)
                finally:
                    request.max_content_length = limit

    def bearer_request() -> bool:
        # Only the token API authenticates bearer calls itself; every other
        # /api/ view relies on the session and the gates below.
        header = request.headers.get("Authorization", "")
        return header.lower().startswith("bearer ") and request.path.startswith("/api/v1/")

    web.install_csrf(app, exempt_when=bearer_request)
    app.extensions["bananawiki.bearer_request"] = bearer_request

    @app.before_request
    def _pipeline():
        g.request_id = uuid.uuid4().hex[:16]
        g.started = time.perf_counter()
        if cfg.run_background_jobs and not cfg.testing:
            app.extensions["bananawiki.scheduler"].start()
        if request.endpoint is None or request.endpoint == "static" or request.endpoint.endswith(".static"):
            return None  # static files (feature assets included) are public
        view = _view()
        if auth.view_stateless(view):
            return None
        access = auth.view_access(view)

        limiter: MemoryLimiter = app.extensions["bananawiki.limiter"]
        if not limiter.hit(f"g:{web.client_ip()}", GLOBAL_RATE_LIMIT, 60):
            return _too_many_requests()

        if bearer_request():
            return None  # the API authenticates and authorises bearer tokens itself

        auth.load_request_user()
        user = auth.current_user()

        if not settings.setup_done() and not auth.view_exempt(view, "setup"):
            return redirect(url_for("auth.setup"))

        if user is None:
            if access == "public" or (access == "public_read" and settings.public_mode_active()):
                return None
            if g.get("session_ended") and not auth.wants_json():
                auth.flash_t("flash.session_expired", "info")
            return auth.redirect_to_login()

        block = auth.account_block(user)
        if block and not auth.view_exempt(view, "approval") and access != "public":
            if auth.wants_json():
                return jsonify({"error": block}), 403
            return redirect(url_for("auth.account_status"))

        if settings.maintenance_active() and not auth.view_exempt(view, "maintenance"):
            if not auth.is_admin(auth.real_user()) and access != "public":
                if auth.wants_json():
                    return jsonify({"error": "maintenance"}), 503
                return redirect(url_for("auth.maintenance"))

        if not auth.view_exempt(view, "account_steps") and access != "public":
            step = _pending_account_step(user)
            if step:
                if auth.wants_json():
                    return jsonify({"error": "account_step_required", "step": step}), 403
                if request.method != "GET":
                    return auth.deny(403)
                return redirect(url_for(step))
        return None

    @app.after_request
    def _after(response: Response) -> Response:
        policy: web.SecurityPolicy = app.extensions.setdefault("bananawiki.csp", web.SecurityPolicy())
        web.apply_security_headers(response, policy, hsts=cfg.is_production)
        if g.get("user") and "Cache-Control" not in response.headers and request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        response.headers.setdefault("X-Request-ID", g.get("request_id", ""))
        return response


def _pending_account_step(user: dict[str, Any]) -> str | None:
    if auth.is_impersonating():
        return None
    if user.get("force_password_change"):
        return "auth.force_password_change"
    if user.get("onboarding_required"):
        return "auth.onboarding"
    return None


def _too_many_requests():
    if auth.wants_json():
        return jsonify({"error": "Too many requests. Slow down and try again shortly."}), 429
    return render_template("errors/error.html", code=429), 429


# ── Templates ─────────────────────────────────────────────────────────────────


def _install_template_globals(app: Flask) -> None:
    from . import templating

    templating.install(app)


# ── Core routes ───────────────────────────────────────────────────────────────


def _install_core_routes(app: Flask) -> None:
    cfg: Config = app.config["BW"]

    @app.get("/health")
    @app.get("/healthz")
    @auth.stateless
    def health():
        database: Database = app.extensions["bananawiki.database"]
        try:
            conn = database.connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as error:  # noqa: BLE001
            log.error("Health check failed: %s", error)
            return jsonify({"status": "unavailable"}), 503
        return jsonify({"status": "ok"})

    @app.get("/robots.txt")
    @auth.public
    @auth.exempt("setup", "maintenance", "account_steps", "approval")
    def robots():
        allowed = settings.public_mode_active()
        body = "User-agent: *\n" + ("Disallow: /admin\nDisallow: /api/\n" if allowed else "Disallow: /\n")
        return Response(body, mimetype="text/plain")

    @app.get("/static/uploads/<path:filename>")
    @auth.public_read
    def uploaded_file(filename: str):
        """Images embedded in pages (the URL shape is stored in page content)."""
        from . import storage

        return storage.send("uploads", filename, inline=True, max_age=86400)

    @app.get("/static/favicons/<path:filename>")
    @auth.public
    @auth.exempt("setup", "maintenance", "account_steps", "approval")
    def favicon_file(filename: str):
        from . import storage

        bundled = Path(app.static_folder or "") / "favicons"
        if (bundled / filename).is_file() and "/" not in filename:
            return send_from_directory(bundled, filename, max_age=86400)
        if filename.startswith("custom_"):
            return storage.send("favicons", filename, inline=True, max_age=86400)
        return Response(status=404)

    @app.get("/favicon.ico")
    @auth.public
    @auth.exempt("setup", "maintenance", "account_steps", "approval")
    def favicon_ico():
        from . import templating

        return redirect(templating.favicon_url())

    @app.get("/source")
    @auth.stateless
    def source_code():
        """AGPL section 13: where to get the corresponding source."""
        return redirect(cfg.source_url)


def _install_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException):
        code = error.code or 500
        if auth.wants_json():
            return jsonify({"error": error.description or error.name}), code
        try:
            return render_template("errors/error.html", code=code, description=error.description), code
        except Exception:  # noqa: BLE001 - the error page itself must never fail
            return Response(f"{code} {error.name}", status=code, mimetype="text/plain")

    @app.errorhandler(Exception)
    def _unexpected(error: Exception):
        if isinstance(error, HTTPException):
            return _http_error(error)
        if isinstance(error, OverflowError):
            # A number from the request (page, offset, id) too large for SQLite.
            return _http_error(BadRequest())
        if isinstance(error, DatabaseUnavailable) or is_unavailable(error):
            log.error("Storage unavailable: %s", error)
            code, message = 503, "The wiki's storage is temporarily unavailable. Try again shortly."
        else:
            log.exception("Unhandled error on %s %s (request %s)", request.method, request.path, g.get("request_id"))
            code, message = 500, "Something went wrong. The error has been logged."
        if auth.wants_json():
            return jsonify({"error": message, "request_id": g.get("request_id")}), code
        try:
            return render_template("errors/error.html", code=code, description=message), code
        except Exception:  # noqa: BLE001
            return Response(message, status=code, mimetype="text/plain")


def session_language() -> str | None:
    return session.get("interface_language")


__all__ = ["create_app", "open_database", "i18n"]
