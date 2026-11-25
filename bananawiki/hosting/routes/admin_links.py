"""Portal admin → a wiki → **Account links** (:mod:`..wiki_links`): the wiki's accounts and their links to
hosting accounts, linking one or many (preview first), unlinking, and switching the wiki to hosting accounts
only. Every view runs tasks in the wiki's container, so GET counts against the per-network limit too."""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from .. import auth, instances, oauth, urls, wiki_links
from ..errors import ServiceError
from ..i18n import t, tn
from ..limits import rate_limit
from ..runtime import RuntimeFailure
from .admin import bp
from .common import account, flash_error


def _wiki(instance_id: str) -> dict[str, Any]:
    inst = instances.get(instance_id)
    if inst is None:
        abort(404)
    if inst["status"] == "terminated" or not instances.provisioning_ready(inst):
        abort(409)
    return inst


def _page(inst: dict[str, Any], *, status: int = 200, rows=None, token: str = "",
          link_request: wiki_links.LinkRequest | None = None, stale: bool = False):
    items, counts, failure = [], {"total": 0, "linked": 0, "admins_unlinked": 0}, ""
    try:
        items, counts = wiki_links.overview(inst)
    except RuntimeFailure as error:
        failure = t(f"hosting.runtime.{error.code}")
    except ServiceError as error:
        failure = t(error.key, **error.values)
    return render_template(
        "hosting/admin/account_links.html", instance=instances.describe(inst), items=items, counts=counts,
        failure=failure, rows=rows, token=token, request_fields=(link_request or wiki_links.LinkRequest()).fields(),
        row_counts=wiki_links.counts(rows) if rows is not None else {}, stale=stale,
        oauth_ready=oauth.enabled() and bool(inst.get("oauth_client_id")),
        hosting_only=(inst.get("platform_signin_mode") or "") == "platform", slug=urls.original_slug(inst["subdomain"])
        or inst["subdomain"],
    ), status


def _back(instance_id: str):
    return redirect(url_for("admin.account_links", instance_id=instance_id))


@bp.get("/admin/instances/<instance_id>/account-links")
@auth.admin_required
@rate_limit(20, count_get=True)
def account_links(instance_id: str):
    return _page(_wiki(instance_id))


@bp.post("/admin/instances/<instance_id>/account-links/preview")
@auth.admin_required
@rate_limit(20)
def account_links_preview(instance_id: str):
    inst = _wiki(instance_id)
    try:
        link_request = wiki_links.LinkRequest.from_form(request.form)
        rows, token = wiki_links.preview(inst, link_request)
    except ServiceError as error:
        flash_error(error)
        return _page(inst, status=400, link_request=locals().get("link_request"))
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return _page(inst, status=502)
    return _page(inst, rows=rows, token=token, link_request=link_request)


@bp.post("/admin/instances/<instance_id>/account-links/apply")
@auth.admin_required
@rate_limit(10)
def account_links_apply(instance_id: str):
    inst = _wiki(instance_id)
    try:
        link_request = wiki_links.LinkRequest.from_form(request.form)
        done = wiki_links.apply(inst, link_request, request.form.get("token") or "", actor=account(),
                                notify=request.form.get("notify") == "1")
    except wiki_links.StalePreview as stale:
        flash(t("hosting.links.stale"), "warning")
        return _page(inst, status=409, rows=stale.rows, token=stale.token, link_request=link_request, stale=True)
    except ServiceError as error:
        flash_error(error)
        return _back(instance_id)
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return _back(instance_id)
    flash(tn("hosting.links.linked", done["linked"], count=done["linked"]), "success")
    if done["refused"]:
        flash(tn("hosting.links.refused_by_wiki", done["refused"], count=done["refused"]), "warning")
    if done["told"]:
        flash(tn("hosting.links.told", done["told"], count=done["told"]), "info")
    return _back(instance_id)


@bp.post("/admin/instances/<instance_id>/account-links/unlink")
@auth.admin_required
@rate_limit(20)
def account_links_unlink(instance_id: str):
    inst = _wiki(instance_id)
    user_id = (request.form.get("wiki_user_id") or "").strip()[:128]
    if not user_id:
        abort(400)
    try:
        changed = wiki_links.unlink(inst, user_id, actor=account())
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return _back(instance_id)
    except ServiceError as error:
        flash_error(error)
        return _back(instance_id)
    flash(t("hosting.links.unlinked" if changed else "hosting.links.not_linked"), "success" if changed else "info")
    return _back(instance_id)


@bp.post("/admin/instances/<instance_id>/account-links/hosting-only")
@auth.admin_required
@rate_limit(10)
def account_links_hosting_only(instance_id: str):
    inst = _wiki(instance_id)
    try:
        applied = wiki_links.switch_to_hosting_only(inst, actor=account())
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return _back(instance_id)
    except ServiceError as error:
        flash_error(error)
        return _back(instance_id)
    flash(t("hosting.links.hosting_only_on" if applied else "hosting.admin.signin_mode_saved_restart_failed"),
          "success" if applied else "error")
    return _back(instance_id)


__all__: list[str] = []
