"""The ``pages`` blueprint and helpers shared by its route modules."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from ....core.web import client_ip, safe_next
from ... import auth
from ...i18n import t
from . import service

bp = Blueprint("pages", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static/pages")


def rate_limited(bucket: str, limit: int, window: int = 60) -> Callable[[Callable], Callable]:
    """Refuse more than *limit* requests per *window* seconds per account (or address)."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            user = auth.current_user()
            who = f"u:{user['id']}" if user else f"ip:{client_ip()}"
            limiter = current_app.extensions["bananawiki.limiter"]
            if not limiter.hit(f"pages:{bucket}:{who}", limit, window):
                if auth.wants_json():
                    return jsonify({"error": t("error.429.body")}), 429
                return render_template("errors/error.html", code=429), 429
            return view(*args, **kwargs)

        return wrapper

    return decorate


def visible_page_or_404(slug: str) -> dict[str, Any]:
    """The page at *slug* if the current user may see it.

    Invisible pages answer 404 like missing ones, so restricted slugs cannot be probed.
    """
    page = service.get_by_slug(slug)
    if page is None or not service.can_view(page):
        abort(404)
    return page


def page_url(page: dict[str, Any]) -> str:
    return url_for("pages.home") if page.get("is_home") else url_for("pages.view", slug=page["slug"])


def back(default: str | None = None):
    """Redirect to the referring page of this site, or *default*."""
    return redirect(safe_next(default or url_for("pages.home"), request.form.get("next"), _referrer_path()))


def _referrer_path() -> str | None:
    referrer = request.referrer or ""
    root = request.host_url.rstrip("/")
    return referrer[len(root):] if referrer.startswith(root + "/") else None


def error_text(exc: Exception) -> str:
    """Translated message of a service error (``PageError``, ``CategoryError``, ``UploadError``)."""
    return t(getattr(exc, "key", "js.error"), **getattr(exc, "values", {}))
