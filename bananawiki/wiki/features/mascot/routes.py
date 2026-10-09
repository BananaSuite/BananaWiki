"""The sunglasses easter egg, the viewer's own switch and the administrator's actions."""

from __future__ import annotations

from flask import abort, jsonify, redirect, request, url_for

from ... import auth
from ...registry import feature_blueprint
from ..audit import record
from . import service

bp = feature_blueprint("mascot", "mascot", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/mascot")


@bp.post("/mascot/shades")
def put_on_shades():
    """Called by the script after the eleventh click on the mascot."""
    user = auth.current_user()
    if not service.state(user)["enabled"]:
        abort(409)
    service.update(user, mascot_shades=1)
    return jsonify({"ok": True})


@bp.post("/settings/mascot")
def own_choice():
    """Switch the mascot on or off, or take its sunglasses off, from "Customize"."""
    user = auth.current_user()
    action = request.form.get("action", "")
    if action == "show":
        service.update(user, mascot_enabled=1)
    elif action == "hide":
        service.update(user, mascot_enabled=0)
    elif action == "shades_off":
        service.update(user, mascot_shades=0)
    else:
        abort(400)
    auth.flash_t(f"mascot.flash.{action}", "success")
    return redirect(url_for("users.display"))


@bp.post("/admin/appearance/mascot")
@auth.admin_required
def everyone():
    action = request.form.get("action", "")
    if action not in service.BULK_ACTIONS:
        abort(400)
    changed = service.apply_to_everyone(action)
    record("mascot.bulk_updated", details={"action": action, "accounts": changed})
    auth.flash_t(f"mascot.admin.done.{action}", "success", count=changed)
    return redirect(url_for("site_admin.appearance"))
