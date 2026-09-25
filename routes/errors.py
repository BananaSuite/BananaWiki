"""
BananaWiki: Error handler routes.
"""

from flask import render_template, flash, redirect, url_for, request, g, jsonify
from flask_wtf.csrf import CSRFError

import os
import html
import sqlite3
import sqlite_runtime
import traceback
import private_logs
from datetime import datetime, timezone
import config
import db
from ops_observability import record_http_error
from helpers import _safe_referrer, get_request_category_tree
from helpers import t  # noqa: F401  (i18n)
from wiki_logger import get_logger


def _dump_traceback(exc, method, path):
    """Write the traceback to ``instance/errors.log`` so that operators can
    inspect 500s even when the hosting platform's log viewer is unavailable."""
    try:
        location = f"{method} {path}"
        location = location.replace("\r", "").replace("\n", "")[:2048]
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=40))
        private_logs.append(os.path.join(config.INSTANCE_DIR, "errors.log"),
                            f"\n{datetime.now(timezone.utc).isoformat()} {location}\n{details}")
    except Exception:
        pass


# Hard-coded last-resort HTML for when even the styled error template cannot
# be rendered.  This happens when the DB itself is unreachable (transient
# Hetzner volume stall, permissions glitch on the WAL sidecar, etc.) -- the
# real exception is captured by ``before_request_hook`` -> SQLite, and then
# the standard error template *also* fails to render because
# :func:`app.inject_globals` issues its own DB reads for ``settings`` /
# ``current_user`` / ``enabled_plugins``.  Without this safety net the
# visitor sees gunicorn's bare ``Internal Server Error`` page (or worse,
# an unstyled stack trace).  This template intentionally has zero runtime
# dependencies: no template engine, no DB, no Flask context processors.
_MINIMAL_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{status} | {heading}</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f5f5f5;color:#222;margin:0;padding:0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;}}
    .card{{background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.08);
           padding:2.5rem 2rem;max-width:480px;width:90%;text-align:center;}}
    h1{{font-size:1.5rem;margin:0 0 .75rem;}}
    p{{color:#555;line-height:1.5;margin:0 0 1.25rem;}}
    a{{display:inline-block;padding:.6rem 1.1rem;background:#fadb14;color:#000;
       border-radius:8px;text-decoration:none;font-weight:600;}}
    a:hover{{background:#fff060;}}
  </style>
</head>
<body>
  <div class="card">
    <h1>{status}: {heading}</h1>
    <p>{message}</p>
    <a href="/">Return home</a>
  </div>
</body>
</html>
"""


def _minimal_error_response(status_code, heading, message):
    """Return a self-contained styled error page with no DB / template deps.

    Used as the very last fallback when ``render_template`` itself fails
    (e.g. because :func:`app.inject_globals` cannot reach the DB).  Keeps
    the visitor on a recognisable BananaWiki-looking page instead of
    seeing gunicorn's raw ``Internal Server Error`` body.
    """
    html = _MINIMAL_ERROR_HTML.format(
        status=int(status_code), heading=heading, message=message,
    )
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def _safe_render(template_name, status_code, fallback_heading, fallback_message, **context):
    """Render *template_name* with a hard-coded HTML fallback on failure.

    If any layer of the normal render chain raises -- the template engine,
    a context processor, an i18n string lookup, or a DB read inside
    :func:`app.inject_globals` -- we still return a styled, recognisable
    error page instead of letting gunicorn emit its bare ``Internal
    Server Error`` body.  The render failure is logged with the request
    ID so operators can still triage genuine template regressions.
    """
    try:
        return render_template(template_name, **context), status_code
    except Exception:  # noqa: BLE001 - intentional broad catch
        try:
            request_id = getattr(g, "_request_id", None)
            get_logger().exception(
                "_safe_render: failed to render %s (request_id=%s); "
                "falling back to minimal HTML response",
                template_name, request_id,
            )
        except Exception:
            pass
        return _minimal_error_response(status_code, fallback_heading, fallback_message)


def _wants_json():
    """Return True when the current request should receive a JSON error body.

    AJAX/fetch clients hitting the JSON API expect a structured JSON error
    instead of an HTML error page.  We treat any request whose path begins
    with ``/api/``, whose endpoint belongs to one of the JSON API
    blueprints, or whose ``Accept`` header explicitly prefers JSON over HTML
    as a JSON client.
    """
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    if accept:
        try:
            best = accept.best_match(["application/json", "text/html"])
            if best == "application/json" and accept[best] >= accept["text/html"]:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _json_error(status_code, message):
    """Return a JSON error response with the given HTTP *status_code*."""
    return jsonify({"error": message, "status": status_code}), status_code


def _safe_category_tree():
    """Return the request-scoped category tree, falling back to empty lists."""
    try:
        return get_request_category_tree()
    except Exception:
        return [], []


def register_error_handlers(app):
    """Register HTTP error handlers on the Flask app."""

    @app.errorhandler(sqlite3.DatabaseError)
    def database_error(error):
        """Return a bounded outage response without reading broken storage again."""
        if not sqlite_runtime.is_unavailable(error):
            return internal_error(error)
        get_logger().error("Database unavailable; preserving storage for operator recovery", exc_info=error)
        message = "The service is temporarily unavailable. Please try again later."
        if _wants_json():
            response = jsonify({"error": message, "status": 503})
        else:
            response = app.make_response(_minimal_error_response(503, "Service unavailable", message))
        response.status_code = 503
        response.headers.update({"Retry-After": "30", "Cache-Control": "no-store"})
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """Handle CSRF validation failures with a helpful message and redirect."""
        if _wants_json():
            return jsonify({"error": "Your session has expired. Please refresh the page and try again."}), 400
        flash(t("flash.your_session_has_expired_please_try_again"), "error")
        return redirect(_safe_referrer() or url_for("home"))

    @app.errorhandler(400)
    def bad_request(e):
        """Render a styled 400 Bad Request page."""
        if _wants_json():
            return _json_error(400, getattr(e, "description", "Bad request."))
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/400.html", 400,
            "Bad request",
            "The request could not be understood by the server.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.errorhandler(404)
    def not_found(e):
        """Serve a custom page if one matches, otherwise render 404."""
        # Check for custom pages plugin
        enabled = getattr(g, "enabled_plugins", {})
        if enabled.get("custom_pages"):
            try:
                from routes.custom_pages import try_serve_custom_page
                response = try_serve_custom_page(request)
                if response is not None:
                    return response
            except Exception:
                pass

        if _wants_json():
            return _json_error(404, "Not found.")
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/404.html", 404,
            "Page not found",
            "The page you were looking for could not be found.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.errorhandler(403)
    def forbidden(e):
        """Render the 403 Forbidden page."""
        if _wants_json():
            return _json_error(403, "Permission denied.")
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/403.html", 403,
            "Forbidden",
            "You do not have permission to access this page.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.errorhandler(413)
    def request_entity_too_large(e):
        """Redirect back with a flash message when the upload exceeds the size limit."""
        max_mb = (config.MAX_CONTENT_LENGTH + (1024 * 1024) - 1) // (1024 * 1024)
        if _wants_json():
            return _json_error(413, f"Upload exceeds the maximum allowed size of {max_mb} MB.")
        flash(t("flash.this_upload_is_too_large_the_maximum_allowed", max_mb=max_mb), "error")
        return redirect(_safe_referrer() or url_for("home"))

    @app.errorhandler(429)
    def too_many_requests(e):
        """Render the 429 Too Many Requests page."""
        if _wants_json():
            return _json_error(429, "Too many requests. Please try again later.")
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/429.html", 429,
            "Too many requests",
            "You have made too many requests. Please try again in a moment.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.errorhandler(500)
    def internal_error(e):
        """Render the 500 Internal Server Error page.

        Also logs the underlying exception alongside the per-request ID
        so operators can correlate the user's "I got a 500" report with
        the actual traceback even when Flask's default handler does not
        carry the request context.  Flask already calls
        ``app.log_exception`` for unhandled errors, but a) it does so at
        ``ERROR`` level without our request-ID context, and b) some 500s
        are raised intentionally with ``abort(500)`` and would otherwise
        log nothing at all.
        """
        original = getattr(e, "original_exception", None) or e
        _dump_traceback(original, request.method, request.path)
        try:
            request_id = getattr(g, "_request_id", None)
            get_logger().error(
                "500 Internal Server Error on %s %s (request_id=%s): %s",
                request.method, request.path, request_id, original,
                exc_info=original if isinstance(original, BaseException) else None,
            )
        except Exception:
            pass
        # Count the failure in the per-wiki analytics roll-up.
        try:
            db.record_request("error")
        except Exception:
            pass
        try:
            record_http_error("wiki", 500, route=request.path)
        except Exception:
            pass
        if _wants_json():
            return _json_error(500, "Internal server error.")
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/500.html", 500,
            "Internal server error",
            "Something went wrong on our end. Please try again in a moment.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.errorhandler(405)
    def method_not_allowed(e):
        """Render a styled 405 Method Not Allowed page."""
        if _wants_json():
            return _json_error(405, "Method not allowed.")
        categories, uncategorized = _safe_category_tree()
        return _safe_render(
            "wiki/405.html", 405,
            "Method not allowed",
            "The request method is not allowed for this URL.",
            categories=categories, uncategorized=uncategorized,
        )

    @app.route("/admin/error-log")
    def admin_error_log():
        """Admin-only endpoint to view recent 500 tracebacks."""
        from flask import session, abort
        user = db.get_user_by_id(session.get("user_id"))
        if not user or user["role"] not in ("admin", "owner"):
            abort(404)
        log_path = os.path.join(config.INSTANCE_DIR, "errors.log")
        try:
            content = private_logs.tail(log_path)
        except FileNotFoundError:
            content = "No errors logged yet."
        except OSError:
            abort(404)
        return f"<pre>{html.escape(content)}</pre>", 200, {"Cache-Control": "private, no-store"}
