"""The audit log page (administrators; the retention is for owners and superusers)."""

from __future__ import annotations

from flask import render_template, request

from bananawiki.core.numbers import ascii_int

from ... import auth, registry
from . import service

bp = registry.feature_blueprint("audit", "audit", __name__, template_folder="templates")


def _hide_protected_addresses(rows: list[dict]) -> None:
    """Addresses of accounts the viewer may not manage stay hidden, as on the sessions page.

    The log keeps the addresses of deleted accounts (deleting an account must
    not erase the record of what it did), but nobody can tell any more whether
    one was an owner or a superuser: only owners and superusers see those.
    """
    from ..admin import service as admin_service

    viewer = auth.real_user()
    for row in rows:
        if not row["ip"] or (row["actor_id"] is None and row["actor_username"] is None):
            continue
        if row["actor_username"] is None:
            hidden = not admin_service.is_top(viewer)
        else:
            actor = {"id": row["actor_id"], "role": row["actor_role"], "is_superuser": row["actor_superuser"]}
            hidden = admin_service.protection_error(viewer, actor) is not None
        if hidden:
            row["ip"], row["ip_hidden"] = None, True


@bp.get("/admin/audit")
@auth.admin_required
def index():
    action = (request.args.get("action") or "").strip()[:100]
    actor = (request.args.get("actor") or "").strip()[:100]
    page_arg = request.args.get("page", "1")
    rows, page, pages = service.entries(action=action, actor=actor, page=ascii_int(page_arg) or 1)
    _hide_protected_addresses(rows)
    return render_template(
        "audit/index.html", rows=rows, page=page, pages=pages, action=action, actor=actor,
        actions=service.actions(), retention_days=service.retention_days(),
    )
