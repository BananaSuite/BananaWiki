"""The sunglasses easter egg, taking them off again and the administrator's actions and easter egg switches."""

from __future__ import annotations

from flask import abort, jsonify, redirect, request, url_for

from ... import auth
from ...registry import feature_blueprint
from ..audit import record
from . import service

bp = feature_blueprint("mascot", "mascot", __name__, template_folder="templates")


def _shades_egg_on() -> None:
    # Switched off, the sunglasses egg is gone: its address answers like one that never existed.
    if not service.eggs()["sunglasses"]:
        abort(404)


@bp.post("/mascot/shades")
def put_on_shades():
    """Called by the script after the eleventh click on the mascot (signed in, as every view here)."""
    _shades_egg_on()
    service.update(auth.current_user(), mascot_shades=1)
    return jsonify({"ok": True})


@bp.delete("/mascot/shades")
def take_off_shades():
    """Called by the script after as many clicks again while the sunglasses are on."""
    _shades_egg_on()
    service.update(auth.current_user(), mascot_shades=0)
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
    if auth.is_impersonating():
        abort(403)
    action = request.form.get("action", "")
    if action not in service.BULK_ACTIONS:
        abort(400)
    _shades_egg_on()
    changed = service.apply_to_everyone(action)
    # One action name per button, so the audit log names it in the reader's language. The catalogues
    # also keep audit.action.mascot.everyone.show and .hide, which older versions logged.
    record(f"mascot.everyone.{action}", details={"count": changed})
    auth.flash_t(f"mascot.admin.done.{action}", "success", count=changed)
    return redirect(url_for("site_admin.appearance"))


@bp.post("/admin/appearance/easter-eggs")
@auth.admin_required
def easter_eggs():
    """Switch each easter egg on or off for the whole wiki (a box left unticked switches it off)."""
    if auth.is_impersonating():
        abort(403)
    changed = service.save_eggs({egg: bool(request.form.get(f"egg_{egg}")) for egg in service.EGGS})
    if changed:
        record("mascot.easter_eggs_changed", target_type="site", details={
            "switched_on": sorted(egg for egg, on in changed.items() if on),
            "switched_off": sorted(egg for egg, on in changed.items() if not on)})
    auth.flash_t("mascot.eggs.saved", "success")
    return redirect(url_for("site_admin.appearance") + "#mascot-eggs-heading")
