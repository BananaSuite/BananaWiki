"""Administration routes for hosting-wide banners."""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from helpers import t

from ..db import (
    create_hosting_banner,
    delete_hosting_banner,
    get_all_accounts,
    get_hosting_banner,
    get_hosting_banner_audience_ids,
    list_hosting_banners,
    update_hosting_banner,
)
from .auth import get_current_account, hosting_admin_required, hosting_rate_limit


_COLORS = {"red", "orange", "yellow", "blue", "green"}
_VISIBILITIES = {"both", "logged_in", "logged_out"}
_AUDIENCES = {"all", "allowlist", "denylist"}


def _valid_hex_color(value):
    value = value or ""
    return len(value) == 7 and value.startswith("#") and all(
        char in "0123456789abcdefABCDEF" for char in value[1:]
    )


def _parse_expiry(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def register_hosting_banner_routes(app):
    """Register hosting banner CRUD routes."""

    def form_data():
        content = request.form.get("content", "").strip()
        color = request.form.get("color", "orange")
        visibility = request.form.get("visibility", "both")
        audience_mode = request.form.get("audience_mode", "all")
        audience_ids = list(dict.fromkeys(request.form.getlist("audience_account_ids")))

        if not content or len(content) > 2000:
            flash(t("hosting.banners.flash.invalid_content"), "error")
            return None
        if color not in _COLORS or visibility not in _VISIBILITIES or audience_mode not in _AUDIENCES:
            flash(t("hosting.banners.flash.invalid_options"), "error")
            return None

        accounts = get_all_accounts()
        valid_ids = {account["id"] for account in accounts if not account.get("deleted_at")}
        if any(account_id not in valid_ids for account_id in audience_ids):
            flash(t("hosting.banners.flash.invalid_audience"), "error")
            return None
        if audience_mode != "all" and not audience_ids:
            flash(t("hosting.banners.flash.audience_required"), "error")
            return None
        if audience_mode == "all":
            audience_ids = []

        try:
            expires_at = _parse_expiry(request.form.get("expires_at", "").strip())
        except ValueError:
            flash(t("hosting.banners.flash.invalid_expiry"), "error")
            return None

        custom_background = None
        custom_text_color = None
        if request.form.get("custom_colors"):
            custom_background = request.form.get("custom_background", "").strip()
            custom_text_color = request.form.get("custom_text_color", "").strip()
            if not (_valid_hex_color(custom_background) and _valid_hex_color(custom_text_color)):
                flash(t("hosting.banners.flash.invalid_colors"), "error")
                return None

        return {
            "content": content,
            "color": color,
            "visibility": visibility,
            "audience_mode": audience_mode,
            "audience_account_ids": audience_ids,
            "expires_at": expires_at,
            "not_removable": 1 if request.form.get("not_removable") else 0,
            "show_countdown": 1 if request.form.get("show_countdown") else 0,
            "custom_background": custom_background,
            "custom_text_color": custom_text_color,
        }

    @app.route("/admin/banners")
    @hosting_admin_required
    def hosting_admin_banners():
        banners = list_hosting_banners()
        accounts = [account for account in get_all_accounts() if not account.get("deleted_at")]
        target_ids = {
            banner["id"]: set(get_hosting_banner_audience_ids(banner["id"]))
            for banner in banners
        }
        return render_template(
            "admin_banners.html", banners=banners, accounts=accounts,
            banner_target_ids=target_ids,
        )

    @app.route("/admin/banners/create", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_create_banner():
        values = form_data()
        if values is None:
            return redirect(url_for("hosting_admin_banners"))
        account = get_current_account()
        create_hosting_banner(created_by=account["id"], **values)
        flash(t("hosting.banners.flash.created"), "success")
        return redirect(url_for("hosting_admin_banners"))

    @app.route("/admin/banners/<int:banner_id>/edit", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_edit_banner(banner_id):
        if not get_hosting_banner(banner_id):
            abort(404)
        values = form_data()
        if values is None:
            return redirect(url_for("hosting_admin_banners"))
        values["is_active"] = 1 if request.form.get("is_active") else 0
        update_hosting_banner(banner_id, **values)
        flash(t("hosting.banners.flash.updated"), "success")
        return redirect(url_for("hosting_admin_banners"))

    @app.route("/admin/banners/<int:banner_id>/delete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_delete_banner(banner_id):
        if not get_hosting_banner(banner_id):
            abort(404)
        delete_hosting_banner(banner_id)
        flash(t("hosting.banners.flash.deleted"), "success")
        return redirect(url_for("hosting_admin_banners"))
