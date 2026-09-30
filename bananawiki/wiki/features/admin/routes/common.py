"""Helpers shared by the administration views."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, request, url_for
from werkzeug.routing import BuildError

from .....core.web import safe_next
from .... import auth
from ....accounts import AccountError
from .. import service


def actor() -> dict[str, Any]:
    user = auth.current_user()
    assert user is not None
    return user


def target_or_404(user_id: str) -> dict[str, Any]:
    user = service.get_user(user_id)
    if user is None:
        abort(404)
    return user


def back(endpoint: str, **values: Any):
    """Redirect to the form's safe ``next`` URL, or to *endpoint*."""
    return redirect(safe_next(url_for(endpoint, **values), request.form.get("next")))


def refuse(error: AccountError) -> None:
    auth.flash_t(error.key, "error", **error.values)


def endpoint_url(endpoint: str, **values: Any) -> str | None:
    """URL of another feature's view, or None while that feature is not installed."""
    try:
        return url_for(endpoint, **values)
    except BuildError:
        return None


def page_number() -> int:
    page = request.args.get("page", "1")
    return max(int(page), 1) if page.isdigit() else 1
