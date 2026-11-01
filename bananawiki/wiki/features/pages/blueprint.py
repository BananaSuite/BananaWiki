"""The ``pages`` blueprint and helpers shared by its route modules."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from ....core.web import client_network, safe_next
from ... import auth
from ...i18n import t
from ..audit.service import record
from . import service, summaries, uploads

bp = Blueprint("pages", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static/pages")


def rate_limited(bucket: str, limit: int, window: int = 60) -> Callable[[Callable], Callable]:
    """Refuse more than *limit* requests per *window* seconds per account (or address)."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            user = auth.current_user()
            who = f"u:{user['id']}" if user else f"ip:{client_network()}"
            limiter = current_app.extensions["bananawiki.limiter"]
            if not limiter.hit(f"pages:{bucket}:{who}", limit, window):
                if auth.wants_json():
                    return jsonify({"error": t("error.429.body")}), 429
                return render_template("errors/error.html", code=429), 429
            return view(*args, **kwargs)

        return wrapper

    return decorate


@bp.app_template_global("image_upload_rules")
def image_upload_rules() -> dict[str, object]:
    """The editor's checks before it sends a picture (see :func:`.uploads.client_rules`)."""
    return uploads.client_rules()


@bp.app_template_global("page_history_message")
def history_message(message: str | None) -> str:
    """An edit summary in the reader's language (see :mod:`.summaries`)."""
    return summaries.render(message)


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


# Page changes made while impersonating are recorded under the administrator's name.
_IMPERSONATION_AUDITED = frozenset({"pages", "page_builder", "page_governance", "attachments", "difficulty_tags",
                                    "assessments"})
_IMPERSONATION_AUDITED_ENDPOINTS = frozenset({  # page changes made through other features
    "contributions.approve", "contributions.deny", "contributions.propose", "contributions.edit",
    "contributions.withdraw", "contributions.quota_request", "contributions.cancel_quota_request",
    "federation.sharing", "page_export.bulk_import",
    "deletion_slowdown.restore", "deletion_slowdown.purge", "bulk_manage.delete_pages",
    "bulk_manage.delete_categories", "federation.fork", "site_admin.spawn_docs",
    "temporary_accounts.schedule_page", "temporary_accounts.remove_page",
    "temporary_accounts.schedule_visibility", "temporary_accounts.remove_visibility",
    "drafts.api_transfer", "drafts.transfer",
})
_IMPERSONATION_UNAUDITED = frozenset({  # previews, presence and private drafts change nothing others see
    "pages.api_preview", "pages.api_highlight", "pages.api_editing_heartbeat", "pages.api_editing_stop",
    "page_builder.preview", "page_builder.custom_preview", "page_builder.clipboard",
    "page_builder.save_draft", "page_builder.discard_draft",
})


@bp.after_app_request
def _audit_impersonated_page_action(response):
    """Record each page change an administrator makes in a member's name while impersonating them.

    History and the page show only the member's name, so edits, renames,
    deletions, reverts, attachments, reservations, protections, reviews of
    proposed edits, difficulty tags, assessments, bulk Markdown imports, bulk
    deletions, restores or purges of pending deletions, proposed edits and
    contribution quota requests, pages shared with or withdrawn from federated
    wikis, copies of federated pages, the built-in guide, scheduled page
    deletions or visibility and take-overs of drafts made
    this way get an audit entry with the administrator as actor (refused
    attempts too, with their status), as chat actions do.
    """
    if request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint in _IMPERSONATION_UNAUDITED \
            or (request.blueprint not in _IMPERSONATION_AUDITED
                and request.endpoint not in _IMPERSONATION_AUDITED_ENDPOINTS) or not auth.is_impersonating():
        return response
    values = request.view_args or {}
    if "remote_id" in values:  # a federated copy: its page_id is the remote wiki's, not one of ours
        target = ("federation_copy", f"{values['remote_id']}/{values.get('page_id')}")
    else:
        target = next(((kind, values[key]) for kind, key in (("page", "slug"), ("page", "page_id"),
                                                              ("category", "category_id"),
                                                              ("contribution", "contribution_id"),
                                                              ("attachment", "attachment_id")) if key in values),
                      (None, None))
    record("page.impersonated_action", target_type=target[0], target_id=target[1],
           details={"as_user": auth.current_user()["username"], "endpoint": request.endpoint,
                    "status": response.status_code})
    return response
