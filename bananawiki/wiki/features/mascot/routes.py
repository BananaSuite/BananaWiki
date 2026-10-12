"""The sunglasses easter egg, taking them off again and the administrator's actions."""

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
    """Called by the script after the eleventh click on the mascot (signed in, as every view here)."""
    service.update(auth.current_user(), mascot_shades=1)
    return jsonify({"ok": True})


@bp.post("/settings/mascot")
def own_choice():
    """Take the mascot's sunglasses off, from "Customise"; it cannot be hidden."""
    if request.form.get("action", "") != "shades_off":
        abort(400)
    service.update(auth.current_user(), mascot_shades=0)
    auth.flash_t("mascot.flash.shades_off", "success")
    return redirect(url_for("users.display"))


@bp.post("/admin/appearance/mascot")
@auth.admin_required
def everyone():
    action = request.form.get("action", "")
    if action not in service.BULK_ACTIONS:
        abort(400)
    changed = service.apply_to_everyone(action)
    # One action name per button, so the audit log names it in the reader's language. The catalogues keep
    # audit.action.mascot.everyone.show and .hide, actions of earlier builds of 1.7, for entries already logged.
    record(f"mascot.everyone.{action}", details={"count": changed})
    auth.flash_t(f"mascot.admin.done.{action}", "success", count=changed)
    return redirect(url_for("site_admin.appearance"))
