"""The ``/admin`` entry point and the dashboard."""

from __future__ import annotations

from typing import Any

from flask import redirect, render_template, url_for

from .....core.timeutil import now_sql, sql_in
from .... import auth
from ....db import db
from .. import analytics, service
from ..blueprint import bp
from .common import endpoint_url

RECENT_EDITS = 10
RECENT_SIGNUPS = 5
PENDING_ON_DASHBOARD = 10


@bp.get("/admin")
@auth.public
@auth.exempt("maintenance")
def entry():
    """1.4 administrator sign-in URL: sends administrators to the dashboard."""
    user = auth.current_user()
    if user is None:
        return redirect(url_for("auth.login", next=url_for("admin.dashboard")))
    if auth.account_block(user):
        return redirect(url_for("auth.account_status"))
    if not auth.is_admin(user):
        return auth.deny()
    return redirect(url_for("admin.dashboard"))


def _stats() -> dict[str, int]:
    week_ago = sql_in(days=-7)
    return {
        "users": db.scalar("SELECT COUNT(*) FROM users", default=0),
        "pending": service.pending_count(),
        "suspended": db.scalar("SELECT COUNT(*) FROM users WHERE suspended = 1", default=0),
        "online": db.scalar(
            "SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE revoked_at IS NULL AND expires_at > ? "
            "AND last_seen_at > ?", (now_sql(), sql_in(minutes=-15)), default=0),
        "pages": db.scalar("SELECT COUNT(*) FROM pages", default=0),
        "edits_week": db.scalar("SELECT COUNT(*) FROM page_history WHERE created_at > ?", (week_ago,), default=0),
        "signups_week": db.scalar("SELECT COUNT(*) FROM users WHERE created_at > ?", (week_ago,), default=0),
    }


def _recent_edits() -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT ph.id, ph.created_at, ph.edit_message, p.title, p.slug, u.username FROM page_history ph "
        "JOIN pages p ON p.id = ph.page_id LEFT JOIN users u ON u.id = ph.edited_by "
        "ORDER BY ph.created_at DESC, ph.id DESC LIMIT ?",
        (RECENT_EDITS,),
    )
    for row in rows:
        row["url"] = endpoint_url("pages.view", slug=row["slug"])
    return rows


@bp.get("/admin/dashboard")
@auth.admin_required
def dashboard():
    analytics.flush()
    pending = db.all(
        "SELECT id, username, created_at, invite_code FROM users WHERE approval_status = 'pending' "
        "ORDER BY created_at LIMIT ?", (PENDING_ON_DASHBOARD,))
    signups = db.all("SELECT id, username, role, created_at FROM users ORDER BY created_at DESC LIMIT ?",
                     (RECENT_SIGNUPS,))
    return render_template(
        "admin/dashboard.html",
        stats=_stats(),
        traffic=analytics.summary(30),
        pending=pending,
        edits=_recent_edits(),
        signups=signups,
    )
