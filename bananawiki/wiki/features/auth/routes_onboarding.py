"""Routes: /onboarding (first-run wizard), /intro and the guided tour (/tour/*)."""

from __future__ import annotations

from flask import abort, redirect, render_template, request, url_for

from ....core.timeutil import now_sql
from ....core.web import safe_next
from ... import accounts, auth, i18n, settings
from ...db import db
from ...i18n import t
from . import docs, onboarding, signin, tour
from .blueprint import bp


def _replay_allowed() -> bool:
    return not settings.get("onboarding_replay_disabled")


@bp.route("/onboarding", methods=["GET", "POST"], endpoint="onboarding")
@auth.exempt("account_steps")
def onboarding_wizard():
    user = auth.current_user()
    if not auth.is_admin(user):
        if user.get("onboarding_required"):
            # Only administrators have a wizard; release anyone else a 1.4 database flagged.
            db.execute("UPDATE users SET onboarding_required = 0, onboarding_completed_at = ? WHERE id = ?",
                       (now_sql(), user["id"]))
            auth.refresh_current_user()
            return redirect(signin.landing_url(auth.current_user(), None))
        return redirect("/")
    first_run = bool(user.get("onboarding_required"))
    if not first_run and not _replay_allowed():
        return redirect("/")
    error = None
    if request.method == "POST":
        try:
            onboarding.apply(onboarding.parse(request.form), user)
        except accounts.AccountError as exc:
            error = t(exc.key, **exc.values)
        else:
            auth.flash_t("auth.onboarding.done", "success")
            return redirect(url_for("auth.intro"))
    return render_template(
        "auth/onboarding.html", first_run=first_run, error=error, form=request.form,
        features=onboarding.feature_options(), languages=i18n.enabled_languages(),
        rows=range(onboarding.INITIAL_USER_ROWS), roles=onboarding.INITIAL_USER_ROLES,
        docs_languages=docs.LANGUAGES, docs_tracked=bool(settings.get("docs_category_id")),
    ), 400 if error else 200


def _intro_allowed(user: dict) -> bool:
    return bool(user.get("intro_required")) or _replay_allowed()


def _complete_intro(user: dict) -> None:
    if user.get("intro_required"):
        db.execute("UPDATE users SET intro_required = 0, intro_completed_at = ? WHERE id = ?",
                   (now_sql(), user["id"]))


@bp.route("/intro", methods=["GET", "POST"])
def intro():
    user = auth.current_user()
    next_url = safe_next("/", request.values.get("next"))
    if request.method == "POST":
        _complete_intro(user)
        return redirect(next_url)
    preview = request.args.get("preview") == "1" and auth.is_admin(user)
    if not preview and not _intro_allowed(user):
        return redirect(next_url)
    options = tour.role_options(user)
    own = tour.real_role(user)
    return render_template("auth/intro.html", preview=preview, next_url=request.args.get("next", ""),
                           role_options=options, own_role=own,
                           steps=tour.steps_for(own))


def _tour_redirect(role: str, step: int = 0):
    return redirect(url_for("auth.tour_step", step=step, role=role))


@bp.post("/tour/start")
def tour_start():
    user = auth.current_user()
    if not _intro_allowed(user) and not auth.is_admin(user):
        return redirect("/")
    return _tour_redirect(tour.normalize_role(request.form.get("tour_role"), user))


@bp.post("/tour/role/<role>")
def tour_role(role: str):
    return _tour_redirect(tour.normalize_role(role, auth.current_user()))


@bp.get("/tour/presenter/<role>/<int:step>")
def tour_presenter(role: str, step: int):
    """1.4 URL for jumping to a step."""
    return _tour_redirect(tour.normalize_role(role, auth.current_user()), step)


@bp.route("/tour/step/<int:step>", methods=["GET", "POST"])
def tour_step(step: int):
    user = auth.current_user()
    role = tour.normalize_role(request.values.get("role"), user)
    if request.method == "POST":
        return _tour_redirect(role, step)
    if not _intro_allowed(user) and not auth.is_admin(user):
        return redirect("/")
    steps = tour.steps_for(role)
    if not 0 <= step < len(steps):
        abort(404)
    return render_template("auth/tour_step.html", step=steps[step], index=step, total=len(steps), role=role,
                           role_options=tour.role_options(user), own_role=tour.real_role(user),
                           apps=tour.enabled_apps())


@bp.post("/tour/finish")
def tour_finish():
    _complete_intro(auth.current_user())
    auth.flash_t("auth.tour.finished", "success")
    return redirect("/")
