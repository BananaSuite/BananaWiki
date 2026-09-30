"""Account settings, sessions, display preferences, the member list and profiles."""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ....core import passwords
from ....core.timeutil import utcnow
from ....core.web import safe_next
from ... import accounts, auth, settings, storage
from ...i18n import enabled_languages, t
from . import preferences, service, sessions

bp = Blueprint("users", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static/users")

_ALL_BUT_SETUP = ("maintenance", "account_steps", "approval")


def _fail(error: Exception, endpoint: str, **values: Any):
    key = getattr(error, "key", "error.generic.body")
    auth.flash_t(key, "error", **getattr(error, "values", {}))
    return redirect(url_for(endpoint, **values))


def _no_impersonation() -> None:
    if auth.is_impersonating():
        abort(403)


# ── Account ──────────────────────────────────────────────────────────────────


@bp.get("/settings", endpoint="settings")
def settings_page():
    user = auth.current_user()
    return render_template(
        "users/settings.html", user=user, tab="account", languages=enabled_languages(),
        display_prefs=preferences.current(user),
    )


@bp.route("/account")
@bp.route("/account/settings")
def legacy_account():
    return redirect(url_for("users.settings"), code=301)


@bp.post("/settings/username")
def change_username():
    user = auth.current_user()
    try:
        if not service.password_ok(user, request.form.get("password")):
            raise service.ProfileError("auth.error.current_password_wrong")
        accounts.rename(user, request.form.get("new_username", ""), changed_by=user["id"])
    except (service.ProfileError, accounts.AccountError) as error:
        return _fail(error, "users.settings")
    auth.flash_t("users.flash.username_changed", "success")
    return redirect(url_for("users.settings"))


@bp.post("/settings/password")
def change_password():
    user = auth.current_user()
    new = request.form.get("new_password", "")
    try:
        if not service.password_ok(user, request.form.get("current_password")):
            raise service.ProfileError("auth.error.current_password_wrong")
        if new != request.form.get("confirm_password", ""):
            raise service.ProfileError("auth.error.passwords_differ")
        if passwords.verify_password(user["password"], new):
            raise service.ProfileError("auth.error.password_unchanged")
        accounts.set_password(user["id"], new, keep_session_id=auth.current_session_id())
    except (service.ProfileError, accounts.AccountError) as error:
        return _fail(error, "users.settings")
    auth.flash_t("users.flash.password_changed", "success")
    return redirect(url_for("users.settings"))


@bp.post("/settings/owner")
@auth.admin_required
def toggle_owner():
    try:
        role = service.set_owner_status(auth.current_user(), request.form.get("password"))
    except service.ProfileError as error:
        return _fail(error, "users.settings")
    auth.flash_t("users.flash.owner_on" if role == "owner" else "users.flash.owner_off", "success")
    return redirect(url_for("users.settings"))


@bp.post("/settings/delete")
def delete_account():
    _no_impersonation()
    try:
        service.delete_own_account(auth.current_user(), request.form.get("password"))
    except service.ProfileError as error:
        return _fail(error, "users.settings")
    auth.end_session()
    auth.flash_t("users.flash.account_deleted", "info")
    return redirect(url_for("auth.login"))


@bp.post("/settings/reactivate")
@auth.exempt("approval", "account_steps")
def reactivate():
    try:
        service.reactivate_self(auth.current_user())
    except service.ProfileError:
        abort(403)
    auth.flash_t("users.flash.reactivated", "success")
    return redirect(url_for("users.settings"))


@bp.post("/settings/language")
@bp.post("/account/language")
@auth.public
@auth.exempt(*_ALL_BUT_SETUP)
def change_language():
    user = auth.current_user()
    language = (request.form.get("language") or "default").strip().lower()
    if language not in enabled_languages():
        language = "default"
    response = make_response(redirect(safe_next(url_for("users.settings") if user else "/",
                                                 request.form.get("next"))))
    if language == "default":
        session.pop("interface_language", None)
    else:
        session["interface_language"] = language
    if user is not None:
        preferences.save(user, preferences.clean({"interface_language": language}, preferences.stored(user)),
                         response)
    return response


# ── Profile editing ──────────────────────────────────────────────────────────


def _require_profile_edit() -> dict[str, Any]:
    user = auth.current_user()
    if not service.can_edit_own_profile(user):
        abort(403)
    return user


@bp.route("/settings/profile", methods=["GET", "POST"])
def edit_profile():
    user = _require_profile_edit()
    if request.method == "POST":
        try:
            service.save_basics(user["id"], real_name=request.form.get("real_name"), bio=request.form.get("bio"),
                                birth_date=request.form.get("birth_date"))
            upload = request.files.get("avatar")
            if upload is not None and upload.filename:
                service.save_avatar(user["id"], upload)
        except (service.ProfileError, storage.UploadError) as error:
            return _fail(error, "users.edit_profile")
        auth.flash_t("users.flash.profile_saved", "success")
        return redirect(url_for("users.edit_profile"))
    return render_template("users/edit_profile.html", user=user, tab="profile",
                           profile=service.get_profile(user["id"]) or {})


@bp.post("/settings/profile/avatar/remove")
def remove_avatar():
    user = _require_profile_edit()
    service.remove_avatar(user["id"])
    auth.flash_t("users.flash.avatar_removed", "success")
    return redirect(url_for("users.edit_profile"))


# ── Display preferences ──────────────────────────────────────────────────────


@bp.route("/settings/display", methods=["GET", "POST"])
@auth.public_read
def display():
    user = auth.current_user()
    if request.method == "GET":
        prefs = preferences.current(user)
        return render_template("users/display.html", user=user, tab="display", display_prefs=prefs,
                               languages=enabled_languages(),
                               background_url=service.upload_url(prefs.get("background_image")),
                               max_background_mb=_mib("background_image_max_upload"))
    form = request.form
    data: dict[str, Any] = {key: form.get(key, "") for key in (
        "theme_mode", "interface_language", "font_scale", "contrast", "line_height", "letter_spacing",
        "sidebar_width", "content_max_width", *preferences.SEMANTIC_KEYS)}
    data["reduce_motion"] = 1 if form.get("reduce_motion") else 0
    data["dyslexic_font"] = 1 if form.get("dyslexic_font") else 0
    for key in preferences.COLOR_KEYS:
        data[key] = form.get(key, "") if form.get(f"{key}_on") else ""
    response = make_response(redirect(url_for("users.display")))
    preferences.save(user, preferences.clean(data, preferences.stored(user)), response)
    auth.flash_t("users.flash.display_saved", "success")
    return response


def _mib(setting: str) -> int:
    return max(1, getattr(current_app.config["BW"], setting) // (1024 * 1024))


@bp.post("/settings/display/reset")
@auth.public_read
def display_reset():
    response = make_response(redirect(url_for("users.display")))
    preferences.reset(auth.current_user(), response)
    auth.flash_t("users.flash.display_reset", "success")
    return response


@bp.post("/settings/display/background")
def background_upload():
    try:
        preferences.save_background(auth.current_user(), request.files.get("background"))
    except (service.ProfileError, storage.UploadError) as error:
        return _fail(error, "users.display")
    auth.flash_t("users.flash.background_saved", "success")
    return redirect(url_for("users.display"))


@bp.post("/settings/display/background/remove")
def background_remove():
    preferences.remove_background(auth.current_user())
    auth.flash_t("users.flash.background_removed", "success")
    return redirect(url_for("users.display"))


# 1.4 JSON endpoints, still used by the theme switch.


@bp.get("/api/accessibility")
@auth.public_read
def api_preferences():
    return jsonify(preferences.current(auth.current_user()))


@bp.post("/api/accessibility")
@auth.public_read
def api_save_preferences():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": t("users.error.invalid_request")}), 400
    user = auth.current_user()
    allowed = {k: v for k, v in data.items() if k in preferences.DEFAULTS and k != "background_image"}
    prefs = preferences.clean(allowed, preferences.stored(user))
    response = jsonify({"ok": True, "preferences": prefs})
    preferences.save(user, prefs, response)
    return response


@bp.post("/api/accessibility/reset")
@auth.public_read
def api_reset_preferences():
    user = auth.current_user()
    response = jsonify({"ok": True, "defaults": preferences.DEFAULTS})
    preferences.reset(user, response)
    return response


@bp.route("/api/accessibility/background", methods=["POST", "DELETE"])
def api_background():
    user = auth.current_user()
    if request.method == "DELETE":
        preferences.remove_background(user)
        return jsonify({"ok": True, "background_image": "", "url": ""})
    try:
        name = preferences.save_background(user, request.files.get("file"))
    except (service.ProfileError, storage.UploadError) as error:
        return jsonify({"error": t(error.key, **error.values)}), 400
    return jsonify({"ok": True, "background_image": name, "url": service.upload_url(name)})


# ── Sessions ─────────────────────────────────────────────────────────────────


@bp.get("/settings/sessions")
def sessions_page():
    user = auth.current_user()
    impersonating = auth.is_impersonating()
    return render_template(
        "users/sessions.html", user=user, tab="sessions", impersonating_view=impersonating,
        active=[] if impersonating else sessions.active(user["id"], auth.current_session_id()),
        history=[] if impersonating else sessions.history(user["id"]),
    )


@bp.post("/settings/sessions/<session_id>/revoke")
def revoke_session(session_id: str):
    _no_impersonation()
    user = auth.current_user()
    current = auth.current_session_id()
    if not sessions.revoke(user["id"], session_id):
        abort(404)
    if session_id == current:
        auth.end_session()
        auth.flash_t("users.flash.session_revoked", "info")
        return redirect(url_for("auth.login"))
    auth.flash_t("users.flash.session_revoked", "success")
    return redirect(url_for("users.sessions_page"))


@bp.post("/settings/sessions/logout-all")
def logout_everywhere():
    _no_impersonation()
    auth.revoke_sessions(auth.current_user()["id"])
    auth.end_session()
    auth.flash_t("users.flash.signed_out_everywhere", "info")
    return redirect(url_for("auth.login"))


@bp.post("/settings/sessions/history/clear")
def clear_session_history():
    _no_impersonation()
    sessions.clear_history(auth.current_user()["id"])
    auth.flash_t("users.flash.history_cleared", "success")
    return redirect(url_for("users.sessions_page"))


# ── Members and profiles ─────────────────────────────────────────────────────


@bp.get("/users")
@auth.permission_required("search.users")
def members():
    viewer = auth.current_user()
    query = request.args.get("q", "")
    page = request.args.get("page", 1, type=int) or 1
    rows, pages_count = service.members(viewer, query, page)
    return render_template("users/members.html", members=rows, query=query, page=page, pages=pages_count,
                           can_open=auth.is_admin(viewer) or auth.has_permission("profile.view", viewer))


@bp.get("/users/me")
def my_profile():
    return redirect(url_for("users.profile", username=auth.current_user()["username"]))


@bp.get("/users/<username>")
def profile(username: str):
    target = accounts.by_username(username)
    if target is None:
        abort(404)
    viewer = auth.current_user()
    record = service.get_profile(target["id"])
    if not service.can_view_profile(target, record, viewer):
        abort(404)
    is_own = viewer["id"] == target["id"]
    privileged = is_own or auth.is_admin(viewer)
    calendar = None
    years: list[int] = []
    if settings.get("profile_contribution_chart_enabled", 1):
        years = service.contribution_years(target["id"], viewer)
        year = request.args.get("year", type=int)
        if year not in years:
            year = utcnow().year
            if year not in years:
                years.insert(0, year)
        calendar = service.contribution_calendar(target["id"], viewer, year)
    return render_template(
        "users/profile.html", target=target, profile=record or {}, is_own=is_own, privileged=privileged,
        avatar_url=service.upload_url((record or {}).get("avatar_filename")),
        birthday=service.is_birthday(record), calendar=calendar, years=years,
        contributions=service.recent_contributions(target["id"], viewer),
        role_history=service.role_history(target["id"]) if privileged else [],
        can_edit=is_own and service.can_edit_own_profile(viewer),
    )
