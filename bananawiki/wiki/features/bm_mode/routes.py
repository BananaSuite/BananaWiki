"""BM mode pages: the administrators' settings, BM mode per account and the getting-started card."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from ... import auth, bm, i18n, settings
from ...db import db
from ...migrations import DEFAULT_HOME
from ...registry import registry
from ..audit import record

bp = Blueprint("bm_mode", __name__, template_folder="templates")


def _features() -> list[dict[str, Any]]:
    """Every feature an administrator may show or hide in BM mode, by name."""
    denied = current_app.config["BW"].managed_plugin_denylist
    items = [{"id": feature.id, "name": i18n.t(feature.name), "description": i18n.t(feature.description, ""),
              "shown": bm.choices().get(f"feature:{feature.id}", bool(feature.easy_wiki)),
              "default": bool(feature.easy_wiki)}
             for feature in registry().ordered()
             if feature.toggle != "always" and feature.id not in denied and not bm.third_party(feature.id)]
    return sorted(items, key=lambda item: item["name"].casefold())


def _simplifications() -> list[dict[str, Any]]:
    items = []
    for feature in registry().ordered():
        for item in feature.simplifications:
            items.append({"id": item.id, "feature": i18n.t(feature.name), "label": i18n.t(item.label),
                          "description": i18n.t(item.description, ""), "on": bm.chosen(item)})
    return items


@bp.route("/admin/bm-mode", methods=["GET", "POST"], endpoint="settings")
@auth.admin_required
def settings_page():
    if request.method == "POST":
        if auth.is_impersonating():
            abort(403)
        features = {item["id"]: bool(request.form.get(f"feature_{item['id']}")) for item in _features()}
        items = {item["id"]: bool(request.form.get(f"simplify_{item['id']}")) for item in _simplifications()}
        mode = bool(request.form.get("bm_mode"))
        self_service = bool(request.form.get("bm_self_service"))
        new_accounts = bool(request.form.get("bm_new_accounts"))
        kept = bm.save(mode=mode, features=features, simplifications_on=items, self_service_on=self_service,
                       new_accounts_on=new_accounts)
        for feature_id, reason in kept.items():
            features[feature_id] = True
            auth.flash_t(reason, "warning")
        # Recorded even when the change hid the audit log itself (an owner may), like switching it off.
        record("bm_mode.changed", target_type="site", force=True, details={
            "bm_mode": mode, "hidden_features": sorted(k for k, v in features.items() if not v),
            "simplifications_off": sorted(k for k, v in items.items() if not v),
            "self_service": self_service, "new_accounts": new_accounts})
        auth.flash_t("bm_mode.flash.saved", "success")
        return redirect(url_for("bm_mode.settings"))
    return render_template("bm_mode/admin.html", active=bm.active(), features=_features(),
                           simplifications=_simplifications(), operator_default=bool(bm._operator_default()),
                           self_service=bm.self_service(), new_accounts=bm.new_accounts(),
                           in_bm_mode=bm.accounts_in_bm_mode(), top=_admin_service().is_top(auth.current_user()))


@bp.post("/settings/bm-mode", endpoint="own_switch")
def own_switch():
    """Someone switches BM mode for their own account, when the wiki and their account allow it."""
    user = auth.current_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if auth.is_impersonating() or not bm.may_switch_self(user):
        abort(403)
    on = bool(request.form.get("bm_mode"))
    bm.set_personal(user["id"], on)
    auth.flash_t("bm_mode.flash.own_on" if on else "bm_mode.flash.own_off", "success")
    return redirect(url_for("users.display"))


def display_section() -> str:
    """Customise: BM mode for one's own account, or who decides it."""
    user = auth.current_user()
    if user is None:
        return ""
    return render_template("bm_mode/_display.html", wiki_on=bm.active(), on=bm.personal(user),
                           may_switch=bm.may_switch_self(user) and not auth.is_impersonating())


# One account, or everyone, chosen by administrators


def _admin_service():
    from ..admin import service

    return service


@bp.post("/admin/users/<string:user_id>/bm-mode", endpoint="account_switch")
@auth.admin_required
def account_switch(user_id: str):
    """An administrator puts one account in BM mode or takes it out, and says whether it may switch itself."""
    if auth.is_impersonating():
        abort(403)
    target = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if target is None:
        abort(404)
    admin = _admin_service()
    error = admin.protection_error(auth.current_user(), target)
    if error:
        auth.flash_t(error, "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    rule = request.form.get("bm_self", "")
    if rule not in bm.SELF_RULES:
        abort(400)
    on = bool(request.form.get("bm_mode"))
    with db.transaction():
        changed_mode = bm.set_personal(user_id, on)
        changed_rule = bm.set_rule(user_id, rule)
    if changed_mode or changed_rule:
        record("bm_mode.account_changed", target_type="user", target_id=user_id,
               details={"bm_mode": on, "may_switch": rule or "wiki"})
    auth.flash_t("bm_mode.flash.account_saved", "success", username=target["username"])
    return redirect(url_for("admin.user_detail", user_id=user_id) + "#bm-mode")


# The base roles the switch for many accounts offers; a custom role counts under its base role.
EVERYONE_ROLES = ("user", "editor", "admin")


def _everyone_sql(actor: dict[str, Any], roles: list[str]) -> tuple[str, list[Any]]:
    """SQL condition on ``users``: the accounts of *roles* that *actor* may manage.

    The administrator's own account counts under administrators, an owner's too.
    """
    condition, params = _admin_service()._manageable_sql(actor)
    marks = ", ".join("?" for _ in roles)
    if "admin" in roles:
        return f"({condition} AND (role IN ({marks}) OR id = ?))", [*params, *roles, actor["id"]]
    return f"({condition} AND role IN ({marks}))", [*params, *roles]


@bp.post("/admin/bm-mode/everyone", endpoint="everyone")
@auth.admin_required
def everyone():
    """Put the accounts of the chosen roles the administrator may manage in BM mode, or take them all out."""
    if auth.is_impersonating():
        abort(403)
    action = request.form.get("action", "")
    if action not in ("on", "off"):
        abort(400)
    chosen = request.form.getlist("role")
    roles = [role for role in EVERYONE_ROLES if role in chosen]
    if not roles:
        auth.flash_t("bm_mode.flash.everyone_no_roles", "error")
        return redirect(url_for("bm_mode.settings") + "#bm-everyone")
    condition, params = _everyone_sql(auth.current_user(), roles)
    count = bm.set_everyone(action == "on", condition=condition, params=params)
    record("bm_mode.everyone_changed", target_type="site",
           details={"bm_mode": action == "on", "roles": roles, "accounts": count})
    auth.flash_t("bm_mode.flash.everyone_on" if action == "on" else "bm_mode.flash.everyone_off", "success",
                 count=count)
    return redirect(url_for("bm_mode.settings") + "#bm-everyone")


def user_section(target: dict[str, Any] | None = None) -> str:
    """Admin → Users → one account: its BM mode and whether it may switch it itself."""
    user = auth.current_user()
    if target is None or user is None or not auth.is_admin(user):
        return ""
    locked = _admin_service().protection_error(user, target)
    return render_template("bm_mode/_user.html", target=target, wiki_on=bm.active(), on=bm.personal(target),
                           rule=target.get("bm_self") or "", wiki_rule=bm.self_service(), locked=locked,
                           impersonating=auth.is_impersonating())


# Getting started


def _steps() -> list[dict[str, Any]]:
    home = db.one("SELECT content FROM pages WHERE is_home = 1 LIMIT 1")
    wrote = bool(db.scalar("SELECT 1 FROM pages WHERE is_home = 0 LIMIT 1")) or bool(
        home and (home["content"] or "").strip() != DEFAULT_HOME.strip())
    invited = (db.scalar("SELECT COUNT(*) FROM users", default=0) > 1
               or bool(db.scalar("SELECT 1 FROM invite_codes LIMIT 1")) or settings.open_signup_active())
    named = settings.site_name() != "BananaWiki"
    return [
        {"key": "write", "done": wrote, "url": url_for("pages.create")},
        {"key": "invite", "done": invited, "url": url_for("admin.codes")},
        {"key": "name", "done": named, "url": url_for("site_admin.general")},
    ]


def dashboard_card() -> str:
    user = auth.current_user()
    if not user or not auth.is_admin(user) or not bm.simplified("bm_mode.getting_started"):
        return ""
    steps = _steps()
    if all(step["done"] for step in steps):
        return ""
    return render_template("bm_mode/_dashboard.html", steps=steps)
