"""The sunglasses easter egg, taking them off again, the key-sequence easter eggs (finds, the easter egg
page, the administrators' editor) and the administrator's actions and easter egg switches."""

from __future__ import annotations

from flask import abort, current_app, g, jsonify, redirect, render_template, request, url_for

from ....core import eggs as shared
from ... import auth
from ...db import db
from ...i18n import t
from ...registry import feature_blueprint
from ..audit import record
from . import easter, service

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


def _save_switches(form) -> None:
    """Switch each easter egg on or off for the whole wiki (a box left unticked switches it off)."""
    changed = service.save_eggs({egg: bool(form.get(f"egg_{egg}")) for egg in service.EGGS})
    if changed:
        record("mascot.easter_eggs_changed", target_type="site", details={
            "switched_on": sorted(egg for egg, on in changed.items() if on),
            "switched_off": sorted(egg for egg, on in changed.items() if not on)})


@bp.post("/admin/appearance/easter-eggs")
@auth.admin_required
def easter_eggs():
    """The switches alone. Appearance saves them with its one Save (:func:`save_with_appearance`); this address
    stays for scripts and pages opened before an upgrade."""
    if auth.is_impersonating():
        abort(403)
    _save_switches(request.form)
    auth.flash_t("mascot.eggs.saved", "success")
    return redirect(url_for("site_admin.appearance") + "#mascot-eggs-heading")


# Key-sequence easter eggs

FINDS_PER_MINUTE = 10


@bp.post("/easter-eggs/<string:egg_id>/found")
def egg_found(egg_id: str):
    """Called by banana-eggs.js when a signed-in person finishes an egg's sequence: answers {ok, new}."""
    user = auth.current_user()
    if auth.is_impersonating():
        abort(403)  # a find belongs to the person at the keyboard, not to the account being viewed as
    limiter = current_app.extensions["bananawiki.limiter"]
    if not limiter.hit(f"mascot:egg_found:{user['id']}", FINDS_PER_MINUTE, 60):
        return jsonify({"error": t("error.429.body")}), 429
    found = easter.egg(egg_id)
    if found is None:  # unknown, switched off, not recorded, or every key-sequence egg off
        abort(404)
    new = easter.record_find(user["id"], found)
    if new:
        record("mascot.egg_found", target_type="user", target_id=user["id"], details={"egg": found["id"]})
    return jsonify({"ok": True, "new": new})


@bp.get("/easter-egg")
@auth.public_read
def easter_egg_page():
    """Every egg there is to find (the 1.4 address): found ones with the date, the others as "?"."""
    if not easter.master_on():
        abort(404)
    user = auth.current_user()
    listed = easter.recorded()
    found = easter.finds(user["id"]) if user else {}
    return render_template("mascot/easter_egg.html", eggs=listed, found=found,
                           found_count=sum(1 for item in listed if item["id"] in found))


def _editor_errors(error: shared.EggError) -> list[str]:
    """One sentence per problem, naming the egg and the field as the editor labels them."""
    messages = []
    for index, field, code in error.errors:
        where = t("mascot.eggs.error.list") if index is None else t("mascot.eggs.editor.title", n=index + 1)
        label = t(f"mascot.eggs.field.{field}", field)
        messages.append(t("mascot.eggs.error.line", where=where, field=label,
                          problem=t(f"mascot.eggs.problem.{code}", code)))
    return messages


def _save_list(form):
    """Store the editor's list of key-sequence eggs: whether it changed, or, when anything is wrong, the
    Appearance page again with each problem and what was sent (400)."""
    raw = form.get("easter_egg_configs", "")
    try:
        changed = easter.save(raw)
    except shared.EggError as error:
        g.easter_egg_errors = _editor_errors(error)
        g.easter_egg_submitted = raw if len(raw.encode("utf-8")) <= shared.MAX_BYTES else None
        auth.flash_t("mascot.eggs.list_invalid", "error")
        return current_app.view_functions["site_admin.appearance"](), 400
    if changed:
        record("mascot.easter_egg_list_changed", target_type="site",
               details={"eggs": [egg["id"] for egg in easter.config()["eggs"]]})
    return None


@bp.post("/admin/appearance/easter-eggs/list")
@auth.admin_required
def easter_egg_list():
    """The list alone, kept like :func:`easter_eggs`."""
    if auth.is_impersonating():
        abort(403)
    refused = _save_list(request.form)
    if refused is not None:
        return refused
    auth.flash_t("mascot.eggs.list_saved", "success")
    return redirect(url_for("site_admin.appearance") + "#mascot-egg-list-heading")


def save_with_appearance(form) -> object | None:
    """``admin.appearance.save``: Admin -> Appearance's one Save stores the switches and the list with the
    colours. The list is checked first: when it is refused nothing is stored (the colours neither) and the
    page shows again with what was sent. ``None`` once saved, or for a form without these fields."""
    if not form.get("mascot_eggs"):
        return None
    switches = {egg: bool(form.get(f"egg_{egg}")) for egg in service.EGGS}
    if auth.is_impersonating():
        # The colours may be saved while viewing as another administrator; the easter eggs may not change.
        if switches != service.eggs() or not easter.unchanged(form.get("easter_egg_configs", "")):
            abort(403)
        return None
    g.easter_egg_switches = switches
    refused_page = _save_list(form)
    if refused_page is not None:
        return refused_page
    _save_switches(form)
    return None


def user_section(target: dict | None = None) -> str:
    """Admin -> Users -> one account: how many eggs it found, and removing them."""
    user = auth.current_user()
    if target is None or user is None or not auth.is_admin(user):
        return ""
    count = easter.find_count(target["id"])
    if not count:
        return ""
    return render_template("mascot/_user.html", target=target, count=count,
                           impersonating=auth.is_impersonating())


@bp.post("/admin/users/<string:user_id>/easter-eggs/revoke")
@auth.admin_required
def revoke_finds(user_id: str):
    if auth.is_impersonating():
        abort(403)
    target = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if target is None:
        abort(404)
    from ..admin import service as admin_service

    error = admin_service.protection_error(auth.current_user(), target)
    if error:
        auth.flash_t(error, "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    removed = easter.revoke(user_id)
    if removed:
        record("mascot.egg_finds_revoked", target_type="user", target_id=user_id, details={"count": removed})
    auth.flash_t("mascot.eggs.revoked", "success", username=target["username"])
    return redirect(url_for("admin.user_detail", user_id=user_id))
