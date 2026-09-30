"""Download your own data; administrators download anyone's."""

from __future__ import annotations

from typing import Any

from flask import Response, abort, current_app, redirect, stream_with_context, url_for

from ... import accounts, auth
from ...registry import feature_blueprint
from . import service

bp = feature_blueprint("user_data_export", "user_data_export", __name__, template_folder="templates")

EXPORTS_PER_HOUR = 5


def _download(user: dict[str, Any], requester: dict[str, Any]) -> Response:
    limiter = current_app.extensions["bananawiki.limiter"]
    if not limiter.hit(f"export:{requester['id']}", EXPORTS_PER_HOUR, 3600):
        abort(429)
    current_app.logger.info("Data export of %s requested by %s", user["username"], requester["username"])
    response = Response(stream_with_context(service.stream(user)), mimetype="application/zip")
    response.headers["Content-Disposition"] = f'attachment; filename="{service.filename(user)}"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/settings/export")
def own_export():
    user = auth.current_user()
    return _download(user, user)


@bp.get("/account/export")
def legacy_own_export():
    return redirect(url_for("user_data_export.own_export"), code=301)


@bp.get("/admin/users/<user_id>/export")
@auth.admin_required
def admin_export(user_id: str):
    target = accounts.by_id(user_id)
    if target is None:
        abort(404)
    admin = auth.current_user()
    if target["id"] != admin["id"] and (target.get("is_superuser") or target["role"] == "owner"):
        abort(403)
    return _download(target, admin)
