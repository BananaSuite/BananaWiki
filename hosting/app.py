"""Flask application for the BananaWiki managed hosting portal."""

import logging
import os
import secrets
import sqlite3
import sqlite_runtime
import sys
import threading
import traceback
from datetime import timedelta

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config
from ._subdomain_proxy import SubdomainProxyMiddleware
from .db import get_hosting_api_enabled, init_hosting_db
from .routes import register_hosting_routes
from .routes.api import api_disabled_error, api_routing_error, is_api_path
from ops_observability import record_http_error

# The hosting portal lives in a subpackage (``hosting/``) but reuses the
# BananaWiki-wide translation files at ``<repo>/translations/*.json``.  Make
# sure the repo root is importable so ``helpers._translations`` can be
# loaded regardless of how the portal is launched (Gunicorn, dev script,
# pytest).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from helpers._translations import t as _t  # noqa: E402  (import after sys.path tweak)
from helpers._interface_languages import (  # noqa: E402
    BUILTIN_INTERFACE_LANGUAGES,
    match_best_interface_language,
)

logger = logging.getLogger("hosting")

# Cleanup interval: check for expired instances every 5 minutes.
_CLEANUP_INTERVAL = 5 * 60  # seconds
_cleanup_timer = None
_background_services_started = False
_background_services_lock = None


class _AutoSecureSessionInterface(SecureCookieSessionInterface):
    """Session interface that sets the ``Secure`` cookie flag dynamically.

    Instead of relying on the static ``SESSION_COOKIE_SECURE`` config,
    this checks the actual request scheme so that the session cookie works
    over both HTTP (development / LAN / port mode) and HTTPS (production
    behind a reverse proxy).
    """

    def get_cookie_secure(self, app):
        """Return ``True`` only when the current request is over HTTPS."""
        try:
            return request.is_secure
        except RuntimeError:
            return app.config.get("SESSION_COOKIE_SECURE", False)


def run_maintenance_once():
    """Run one complete hosting maintenance pass.

    Kept separate from the timer wrapper so production can invoke the work
    from a dedicated systemd timer instead of relying on a web worker.
    """
    try:
        from .instance_manager import (
            terminate_expired, purge_expired_grace_periods,
            check_instance_health,
            cleanup_expired_instance_suspensions,
            enforce_storage_quotas,
            terminate_instance,
        )
        from .db import cleanup_expired_account_suspensions, cleanup_denied_hosting_accounts, get_hosting_activation_denied_timeout_seconds, get_hosting_activation_denied_deletion_enabled, get_pending_deletion_accounts
        count = terminate_expired()
        if count:
            logger.info("Cleaned up %d expired instance(s)", count)
        purged = purge_expired_grace_periods()
        if purged:
            logger.info(
                "Hard-deleted retained data for %d instance(s) past their grace window",
                purged,
            )
        quota_suspended = enforce_storage_quotas()
        if quota_suspended:
            logger.warning(
                "Storage protection suspended %d over-quota instance(s)",
                quota_suspended,
            )
        auto_unsuspended = cleanup_expired_instance_suspensions()
        if auto_unsuspended:
            logger.info(
                "Auto-unsuspended %d instance(s) with expired timed suspensions",
                auto_unsuspended,
            )
        auto_unsuspended_accounts = cleanup_expired_account_suspensions()
        if auto_unsuspended_accounts:
            logger.info(
                "Auto-unsuspended %d account(s) with expired timed suspensions",
                auto_unsuspended_accounts,
            )
        from .domains import refresh_domains
        refresh_domains()
        auto_deleted_denied = 0
        if get_hosting_activation_denied_deletion_enabled():
            denied_timeout = get_hosting_activation_denied_timeout_seconds()
            auto_deleted_denied = cleanup_denied_hosting_accounts(timeout_seconds=denied_timeout)
        if auto_deleted_denied:
            logger.info(
                "Auto-deleted %d denied hosting account(s) past their timeout",
                auto_deleted_denied,
            )

        # Pending-approval digest: one email per interval listing signups
        # still waiting, so admins learn about the queue without one email
        # per user.  Immediate mode (if selected) already fired at signup.
        try:
            from .notifications import maybe_send_pending_approval_digest
            if maybe_send_pending_approval_digest():
                logger.info("Sent pending-approval digest to the admin address")
        except Exception:
            logger.exception("Error during pending-approval digest sweep")

        # Pending-deletion cleanup: delete accounts whose countdown has expired.
        pd_ids = get_pending_deletion_accounts()
        for pd_id in pd_ids:
            try:
                from .db import get_instances_for_account, delete_account
                for inst in get_instances_for_account(pd_id):
                    if inst["status"] != "terminated":
                        terminate_instance(inst["id"])
                delete_account(pd_id)
            except Exception:
                logger.warning("Failed to auto-delete pending-deletion account %s", pd_id, exc_info=True)
        if pd_ids:
            logger.info("Auto-deleted %d account(s) past their pending-deletion countdown", len(pd_ids))

        # Tombstone sweep: erase soft-deleted account rows once no instance
        # data is retained, so grayed-out "deleted" accounts do not linger.
        try:
            from .db import purge_deleted_account_tombstones
            removed = purge_deleted_account_tombstones()
            if removed:
                logger.info("Purged %d deleted-account tombstone(s) with no retained instance data", removed)
        except Exception:
            logger.exception("Error during deleted-account tombstone sweep")

        # Health monitor: probe all running instances and log any that are
        # unhealthy so admins have visibility without waiting for a user report.
        try:
            from .db import get_all_active_instances
            unhealthy = []
            for inst in get_all_active_instances():
                if inst["status"] != "running":
                    continue
                if not check_instance_health(inst):
                    unhealthy.append(inst["subdomain"])
            if unhealthy:
                logger.warning(
                    "Health monitor: %d instance(s) are unhealthy: %s",
                    len(unhealthy), ", ".join(unhealthy),
                )
        except Exception:
            logger.exception("Error during health monitor sweep")
    except Exception:
        logger.exception("Error during periodic cleanup")


def _periodic_cleanup():
    """Run maintenance and reschedule the in-process fallback timer."""
    global _cleanup_timer
    run_maintenance_once()
    _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _periodic_cleanup)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()


def stop_cleanup_timer():
    """Cancel the periodic cleanup timer (used during shutdown)."""
    global _cleanup_timer
    if _cleanup_timer is not None:
        _cleanup_timer.cancel()
        _cleanup_timer = None


def _running_under_pytest():
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def _acquire_background_services_lock():
    """Hold a process-wide lock so one Gunicorn worker runs schedulers."""
    global _background_services_lock
    if _background_services_lock is not None:
        return True
    try:
        from filelock import FileLock, Timeout as FileLockTimeout

        os.makedirs(os.path.dirname(config.HOSTING_DATABASE_PATH), exist_ok=True)
        lock_path = os.path.join(
            os.path.dirname(config.HOSTING_DATABASE_PATH), ".background-services.lock"
        )
        lock = FileLock(lock_path)
        lock.acquire(timeout=0)
        # Retain the FileLock object itself for the lifetime of the process.
        # Keeping only an implementation-private descriptor allowed the local
        # object to be garbage-collected and its lock to be released, causing
        # every Gunicorn worker to start duplicate cleanup/backup schedulers.
        _background_services_lock = lock
        return True
    except ImportError:
        return False
    except (OSError, FileLockTimeout):
        return False


def start_background_services(*, force=False):
    """Start hosting recovery and cleanup services exactly once per server."""
    global _background_services_started
    if _background_services_started:
        return False
    if not force:
        if os.environ.get("BANANAWIKI_HOSTING_SKIP_BACKGROUND_SERVICES") == "1":
            return False
        if _running_under_pytest():
            return False
    if not _acquire_background_services_lock():
        return False

    _background_services_started = True
    _maybe_start_bytecode_precompile()
    _start_background_recovery()
    _periodic_cleanup()
    try:
        from .gdrive_backup import start_backup_scheduler
        start_backup_scheduler()
    except Exception:
        import logging
        logging.getLogger("hosting.app").warning(
            "Could not start Google Drive backup scheduler", exc_info=True
        )
    import atexit
    atexit.register(stop_cleanup_timer)
    return True


def _start_background_recovery():
    """Recover instances marked as ``running`` in a daemon thread.

    Called from :func:`create_hosting_app` so the portal Gunicorn worker
    can start serving requests immediately rather than blocking on
    ``recover_running_instances`` inside ``--preload``.  Recovery still
    runs in parallel internally; this just unblocks the WSGI worker
    boot.
    """
    def _worker():
        try:
            from .instance_manager import recover_running_instances
            recovered = recover_running_instances()
            if recovered:
                logger.info("Recovered %d instance(s) on startup", recovered)
        except Exception:
            logger.exception("Error recovering instances on startup")

    t = threading.Thread(target=_worker, daemon=True, name="hosting-recover")
    t.start()


def _maybe_start_bytecode_precompile():
    """Optionally pre-compile BananaWiki sources in a background thread.

    This avoids blocking portal startup and deployment health checks on slower
    disks or larger trees while still warming ``.pyc`` files for future
    instance launches.
    """
    enabled = os.environ.get("HOSTING_PRECOMPILE_ON_START", "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return

    def _worker():
        try:
            import compileall

            compileall.compile_dir(config.BW_ROOT, quiet=2, force=False)
        except Exception:
            logger.warning(
                "Bytecode pre-compilation failed (non-fatal)",
                exc_info=True,
            )

    t = threading.Thread(target=_worker, daemon=True, name="hosting-precompile")
    t.start()


def create_hosting_app():
    """Create and configure the hosting portal Flask application."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )

    app.secret_key = config.HOSTING_SECRET_KEY
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_NAME"] = "bwh_session"
    app.permanent_session_lifetime = timedelta(days=7)

    # Route tenants after a trusted front proxy has normalized the request.
    if config.HOSTING_MODE == "subdomain" and config.BASE_DOMAIN:
        app.wsgi_app = SubdomainProxyMiddleware(app.wsgi_app)

    # Reverse-proxy support ---
    if config.HOSTING_PROXY_MODE:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
        )
        app.config["PREFERRED_URL_SCHEME"] = (
            config.HOSTING_PREFERRED_URL_SCHEME or "https"
        )

    # Use the auto-secure session interface so session cookies work on both
    # HTTP (dev / port mode) and HTTPS (production) without manual config.
    app.session_interface = _AutoSecureSessionInterface()

    @app.before_request
    def hide_disabled_api():
        """Answer every path under the API prefix with the JSON 404 while the API is off.

        Registered before CSRF protection, and app hooks run before routing
        errors are raised, so a path such as ``/api/v1//me`` gets the same
        answer instead of a slash-merging redirect or an HTML page.
        """
        if is_api_path(request.path) and not get_hosting_api_enabled():
            return api_disabled_error()
        return None

    CSRFProtect(app)

    @app.before_request
    def before_request_hook():
        """Clear per-request caches on g and resolve interface language.

        The hosting portal supports the same built-in en/it translations
        as the main wiki.  Resolution order matches the main app's:
          1. ``session["interface_language"]`` (manual user choice)
          2. ``Accept-Language`` request header
          3. ``"en"`` (default)

        Setting ``g._current_language`` here (a ``before_request`` hook
        rather than a context processor) ensures route handlers that
        call ``flash(t(key))`` resolve translations against the correct
        language.
        """
        if hasattr(g, "_csp_nonce"):
            del g._csp_nonce

        # Allocate a short, opaque request ID so 500 pages can show the
        # operator a value to grep server logs for.  Stored on ``g`` so the
        # before/after hooks and the error handler agree on the same value.
        g._request_id = secrets.token_hex(8)

        # Skip language resolution for static assets to avoid extra work
        # on the hot path.
        if request.endpoint == "static":
            return

        try:
            session_lang = session.get("interface_language")
            chosen = None
            if session_lang and session_lang in BUILTIN_INTERFACE_LANGUAGES:
                chosen = session_lang
            else:
                accept = request.headers.get("Accept-Language")
                if accept:
                    chosen = match_best_interface_language(
                        accept, BUILTIN_INTERFACE_LANGUAGES
                    )
            g._current_language = chosen or "en"
        except Exception:
            try:
                g._current_language = "en"
            except RuntimeError:
                pass

    @app.get("/health")
    def health_check():
        """Lightweight liveness probe: no auth required."""
        try:
            from .db import get_hosting_db_context
            with get_hosting_db_context() as conn:
                conn.execute("SELECT 1")
            return {"status": "ok"}, 200
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}, 503

    def _get_csp_nonce():
        """Return the per-request CSP nonce, generating one on first call."""
        if not hasattr(g, "_csp_nonce"):
            g._csp_nonce = secrets.token_hex(16)
        return g._csp_nonce

    @app.context_processor
    def inject_csp_nonce():
        """Make the CSP nonce available in every template as ``csp_nonce``.

        Also exposes the JSON translation helper as ``t`` so hosting
        templates can resolve translation keys with ``{{ t('hosting.x') }}``.
        """
        return {"csp_nonce": _get_csp_nonce(), "t": _t, "source_code_url": config.SOURCE_CODE_URL,
                "contact_email": config.HOSTING_CONTACT_EMAIL}

    @app.errorhandler(sqlite3.DatabaseError)
    def handle_database_error(error):
        if not sqlite_runtime.is_unavailable(error):
            return handle_500(error)
        logger.error("Platform database unavailable; preserving storage for operator recovery", exc_info=error)
        message = "The service is temporarily unavailable. Please try again later."
        if request.is_json or request.accept_mimetypes.best == "application/json":
            response = app.json.response({"error": message, "status": 503})
        else:
            response = app.response_class(message + "\n", mimetype="text/plain")
        response.status_code = 503
        response.headers.update({"Retry-After": "30", "Cache-Control": "no-store"})
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """Handle CSRF validation failures with a helpful redirect."""
        flash("Your session has expired. Please try again.", "error")
        return redirect(url_for("hosting_login"))

    @app.errorhandler(404)
    def handle_404(e):
        """Return a styled 404 page, or JSON under the REST API prefix."""
        if is_api_path(request.path):
            return api_routing_error(e)
        return render_template(
            "error.html",
            code=404,
            message="Page not found.",
            request_id=getattr(g, "_request_id", None),
        ), 404

    @app.errorhandler(405)
    def handle_405(e):
        """Answer a wrong method on the REST API in JSON; keep Werkzeug's page elsewhere."""
        if is_api_path(request.path):
            return api_routing_error(e)
        return e

    @app.errorhandler(500)
    def handle_500(e):
        """Return a styled 500 page with a request ID and admin diagnostics.

        Behaviour:

        * A request ID is generated (or reused from :attr:`g._request_id`) so
          operators can correlate user reports with server logs.
        * The full exception (with traceback) is logged at ``ERROR`` level so
          the underlying cause is never silently swallowed.
        * Logged-in admins additionally see the exception type and message
          rendered in the page itself, which dramatically speeds up triage
          without ever leaking the traceback to anonymous visitors.
        """
        request_id = getattr(g, "_request_id", None) or secrets.token_hex(8)
        try:
            g._request_id = request_id
        except RuntimeError:
            pass

        logger.exception(
            "Hosting portal 500 [request_id=%s, path=%s, method=%s]",
            request_id,
            request.path,
            request.method,
        )
        try:
            record_http_error("hosting_portal", 500, route=request.path)
        except Exception:
            pass

        # Admin-aware diagnostic details, only shown to currently-logged-in
        # admin accounts.  Anonymous visitors and regular users see only the
        # generic message + request ID.
        admin_detail = None
        try:
            aid = session.get("hosting_account_id")
            if aid:
                from .db import get_account_by_id
                acct = get_account_by_id(aid)
                if acct and acct["is_admin"]:
                    admin_detail = {
                        "type": type(e).__name__,
                        "message": str(e),
                        "traceback": traceback.format_exc(limit=8),
                    }
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to resolve current account during 500 handler"
            )

        return render_template(
            "error.html",
            code=500,
            message="An unexpected error occurred. The site administrators have been notified.",
            request_id=request_id,
            admin_detail=admin_detail,
        ), 500

    @app.after_request
    def set_security_headers(response):
        """Set security headers on every response (mirrors BananaWiki conventions)."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        nonce = _get_csp_nonce()
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}'; "
            f"style-src-attr 'unsafe-inline'; "
            f"img-src 'self' data:; "
            f"font-src 'self'; "
            f"connect-src 'self'; "
            f"media-src 'self'; "
            f"object-src 'none'; "
            f"base-uri 'self'; "
            f"form-action 'self'; "
            f"frame-ancestors 'none';"
        )
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    init_hosting_db()

    # Override INSTANCE_URL_SUFFIX from the DB setting if an admin has
    # configured it via the platform settings page.  This lets admins
    # change the suffix (e.g. from "hosting" to "wiki") without editing
    # the .env file. An unset database value preserves the environment value.
    try:
        from .db import get_hosting_settings as _get_hs
        _hs = _get_hs()
        if _hs:
            suffix_disabled = bool(_hs.get("instance_suffix_disabled"))
            if suffix_disabled:
                config.INSTANCE_URL_SUFFIX = ""
            elif _hs.get("instance_url_suffix"):
                config.INSTANCE_URL_SUFFIX = _hs["instance_url_suffix"].strip().lower()
                config.RESERVED_SUBDOMAINS.add(config.INSTANCE_URL_SUFFIX)
    except Exception:
        pass

    register_hosting_routes(app)

    # Warn when the transactional email sender addresses are still at their
    # vendor-domain defaults.  Self-hosted operators who haven't configured
    # HOSTING_EMAIL_FROM / HOSTING_EMAIL_REPLY_TO will send verification and
    # suspension emails that appear to come from bananawiki.com: confusing
    # for users and potentially violating the vendor's domain policies.
    _vendor_email_defaults = (
        "noreply@bananawiki.com",
        "contact@bananawiki.com",
    )
    if not app.config.get("TESTING"):
        _from_addr = (config.HOSTING_EMAIL_FROM or "").lower()
        _reply_addr = (config.HOSTING_EMAIL_REPLY_TO or "").lower()
        if any(v in _from_addr for v in _vendor_email_defaults):
            logger.warning(
                "HOSTING_EMAIL_FROM is '%s' which contains the vendor default "
                "domain. Set HOSTING_EMAIL_FROM to an address on your own domain "
                "(e.g. 'BananaWiki <noreply@yourdomain.com>').",
                config.HOSTING_EMAIL_FROM,
            )
        if any(v in _reply_addr for v in _vendor_email_defaults):
            logger.warning(
                "HOSTING_EMAIL_REPLY_TO is '%s' which contains the vendor default "
                "domain. Set HOSTING_EMAIL_REPLY_TO to your own support address.",
                config.HOSTING_EMAIL_REPLY_TO,
            )

    # Without a domain there is no sensible contact address to guess, and
    # suspended or denied users are told to write to it.
    if (
        not app.config.get("TESTING")
        and config.HOSTING_CONTACT_EMAIL == config.CONTACT_EMAIL_PLACEHOLDER
    ):
        logger.warning(
            "HOSTING_CONTACT_EMAIL is not set, so the portal and its emails tell "
            "users to write to %s. Set HOSTING_CONTACT_EMAIL to an address "
            "people can reach.",
            config.CONTACT_EMAIL_PLACEHOLDER,
        )

    # Warn when port mode is active but HOSTING_PUBLIC_HOST resolves to a
    # non-public address (localhost, loopback, or an RFC 1918 private range).
    # Instance URLs will be non-functional from any machine other than the
    # server itself.  Set HOSTING_PUBLIC_HOST to your server's public IP or
    # hostname before going to production.
    if (
        not app.config.get("TESTING")
        and config.HOSTING_MODE == "port"
        and config._is_private_host(config.HOSTING_PUBLIC_HOST)
    ):
        logger.warning(
            "HOSTING_PUBLIC_HOST is '%s' which appears to be a non-public "
            "address. Instance URLs shown to users will not be reachable from "
            "other machines. Set HOSTING_PUBLIC_HOST to your server's public "
            "IP or hostname.",
            config.HOSTING_PUBLIC_HOST,
        )

    # Warn in port mode when HOSTING_PUBLIC_SCHEME is "http".  In subdomain
    # mode HTTPS is handled at the nginx/proxy layer so this doesn't apply.
    # In port mode, tenant URLs are built directly from the scheme, so plain
    # "http" means users receive unencrypted traffic with no TLS at all.
    if (
        not app.config.get("TESTING")
        and config.HOSTING_MODE == "port"
        and config.HOSTING_PUBLIC_SCHEME.lower() == "http"
    ):
        logger.warning(
            "HOSTING_PUBLIC_SCHEME is 'http' in port mode. Instance URLs will "
            "be unencrypted. Set HOSTING_PUBLIC_SCHEME=https and configure TLS "
            "at a reverse proxy, or set it explicitly to 'http' to suppress "
            "this warning if you are running in a trusted internal network."
        )

    # Recover instances and start the cleanup timer when not testing.  Gunicorn
    # preload sets BANANAWIKI_HOSTING_SKIP_BACKGROUND_SERVICES and starts these
    # from post_worker_init so no threads exist in the master before fork.
    # An error here must never prevent the Flask app from starting. The portal
    # worker still serves health checks and admin pages while recovery happens
    # in the background.
    if not app.config.get("TESTING"):
        try:
            start_background_services()
        except Exception:
            logger.exception("Failed to start background services: continuing without them")

    return app
