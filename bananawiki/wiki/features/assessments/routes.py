"""Routes: take, manage and review page assessments (1.4 URLs)."""

from __future__ import annotations

import re
from typing import Any

from flask import abort, current_app, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from ... import auth, settings
from ...i18n import t
from ...permissions import ROLES
from ...registry import feature_blueprint
from ..pages import service as pages
from . import service
from .service import AssessmentError, QuestionInput

bp = feature_blueprint("assessments", "assessments", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/assessments")

SUBMIT_LIMIT = 10  # submissions per user per minute
POINTS_SETTING = "assessment_points_badge_enabled"
_QUESTION_FIELD = re.compile(
    r"^questions-(\d{1,4})-(id|prompt|type|options|correct|points_correct|points_incorrect|required|remove)$"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _page(slug: str) -> dict[str, Any]:
    """The page, or 404 when it does not exist or the user may not read it."""
    page = pages.get_by_slug(slug, with_content=False)
    if page is None or not pages.can_view(page):
        abort(404)
    return page


def _assessment(page: dict[str, Any]) -> dict[str, Any]:
    assessment = service.for_page(page["id"])
    if assessment is None:
        abort(404)
    return assessment


def _require_manager(page: dict[str, Any]) -> None:
    if not service.can_manage(page):
        abort(403)


def _translate_errors(errors: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [t(key, **values) for key, values in errors]


def _editor_row(question: QuestionInput | dict[str, Any]) -> dict[str, Any]:
    """One question in the shape the editor template expects."""
    if isinstance(question, QuestionInput):
        return {"id": question.id, "prompt": question.prompt, "question_type": question.question_type,
                "options": question.options, "correct_answers": question.correct_answers,
                "points_correct": question.points_correct, "points_incorrect": question.points_incorrect,
                "is_required": question.is_required, "errors": _translate_errors(question.errors)}
    return {**question, "is_required": bool(question["is_required"]), "errors": []}


def _blank_row() -> dict[str, Any]:
    return {"id": None, "prompt": "", "question_type": "single_choice", "options": [], "correct_answers": [],
            "points_correct": 1, "points_incorrect": 0, "is_required": True, "errors": []}


def _submitted_questions(form: MultiDict) -> list[QuestionInput]:
    """Questions of the editor form, in the order they appear on the page."""
    groups: dict[str, dict[str, str]] = {}
    for key in form:
        match = _QUESTION_FIELD.match(key)
        if match:
            groups.setdefault(match.group(1), {})[match.group(2)] = form.get(key, "")
    parsed = (service.question_from_form(group) for group in groups.values() if not group.get("remove"))
    return [question for question in parsed if question is not None]


def _submitted_settings(form: MultiDict) -> service.Settings:
    return service.Settings(
        title=(form.get("title") or "").strip(),
        description=(form.get("description") or "").strip(),
        allow_multiple_attempts=bool(form.get("allow_multiple_attempts")),
        banned_roles=form.getlist("banned_roles"),
        banned_usernames=re.split(r"[,\n]", form.get("banned_users") or ""),
    )


def _render_manage(page, assessment, form_settings: dict[str, Any], rows: list[dict[str, Any]],
                   error: str | None = None, status: int = 200):
    return render_template(
        "assessments/manage.html", page=page, assessment=assessment, form=form_settings, rows=rows,
        roles=ROLES, error=error, blank=_blank_row(),
        attempt_count=len(service.all_attempts(assessment["id"])) if assessment else 0,
    ), status


def _render_take(page, assessment, user, *, answers=None, error=None, missing=(), status=200):
    attempts = service.attempts_of(assessment["id"], user["id"])
    reason = service.block_reason(assessment, user, attempts=len(attempts))
    return render_template(
        "assessments/take.html", page=page, assessment=assessment, attempts=attempts,
        questions=service.questions_for_taker(assessment["id"]) if reason is None else [],
        block_reason=reason, answers=answers or {}, error=error, missing=set(missing),
        can_manage=service.can_manage(page),
    ), status


# ── Taking an assessment ─────────────────────────────────────────────────────


@bp.get("/page/<slug>/assessment")
def take(slug: str):
    page = _page(slug)
    if not auth.has_permission("assessment.view"):
        abort(403)
    return _render_take(page, _assessment(page), auth.current_user())


@bp.post("/page/<slug>/assessment/submit")
def submit(slug: str):
    user = auth.current_user()
    page = _page(slug)
    if not auth.has_permission("assessment.view"):
        abort(403)
    assessment = _assessment(page)
    if not current_app.extensions["bananawiki.limiter"].hit(f"assessment-submit:{user['id']}", SUBMIT_LIMIT, 60):
        abort(429)
    question_rows = service.questions(assessment["id"])
    answers = {q["id"]: service.parse_answer(q, request.form.getlist(f"q_{q['id']}")) for q in question_rows}
    missing = service.missing_required(question_rows, answers)
    if missing:
        return _render_take(page, assessment, user, answers=answers, missing=missing,
                            error=t("assessments.error.answer_required"), status=422)
    try:
        result = service.submit(assessment, page, user, answers)
    except AssessmentError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return redirect(url_for("assessments.take", slug=page["slug"]))
    auth.flash_t("assessments.submitted", "success", score=result["total_points"], maximum=result["max_points"])
    return redirect(url_for("assessments.attempt", slug=page["slug"], attempt_id=result["id"]))


@bp.get("/page/<slug>/assessment/attempts/<int:attempt_id>")
def attempt(slug: str, attempt_id: int):
    """One submitted attempt: for its author and for the assessment's managers."""
    user = auth.current_user()
    page = _page(slug)
    assessment = _assessment(page)
    row = service.attempt(attempt_id)
    if row is None or row["assessment_id"] != assessment["id"]:
        abort(404)
    manager = service.can_manage(page)
    if row["user_id"] != user["id"] and not manager:
        abort(404)
    return render_template(
        "assessments/attempt.html", page=page, assessment=assessment, attempt=row,
        answers=service.attempt_answers(attempt_id), can_manage=manager,
        reveal=service.reveals_answers(assessment, page, user),
    )


# ── Managing ─────────────────────────────────────────────────────────────────


@bp.route("/page/<slug>/assessment/manage", methods=["GET", "POST"])
def manage(slug: str):
    page = _page(slug)
    _require_manager(page)
    assessment = service.for_page(page["id"])
    if request.method == "GET":
        if assessment:
            form_settings = {"title": assessment["title"], "description": assessment["description"],
                             "allow_multiple_attempts": bool(assessment["allow_multiple_attempts"]),
                             "banned_roles": service.banned_roles(assessment),
                             "banned_users": ", ".join(service.banned_usernames(assessment))}
            rows = [_editor_row(q) for q in service.questions(assessment["id"])]
        else:
            form_settings = {"title": page["title"], "description": "", "allow_multiple_attempts": False,
                             "banned_roles": [], "banned_users": ""}
            rows = []
        return _render_manage(page, assessment, form_settings, rows or [_blank_row()])

    submitted = _submitted_settings(request.form)
    inputs = _submitted_questions(request.form)
    form_settings = {"title": submitted.title, "description": submitted.description,
                     "allow_multiple_attempts": submitted.allow_multiple_attempts,
                     "banned_roles": submitted.banned_roles, "banned_users": request.form.get("banned_users", "")}
    rows = [_editor_row(q) for q in inputs]
    if request.form.get("add_question"):
        return _render_manage(page, assessment, form_settings, [*rows, _blank_row()])
    try:
        service.save(page, submitted, inputs, actor_id=auth.current_user()["id"])
    except AssessmentError as exc:
        return _render_manage(page, assessment, form_settings, rows or [_blank_row()],
                              error=t(exc.key, **exc.values), status=400)
    auth.flash_t("assessments.saved", "success")
    return redirect(url_for("assessments.manage", slug=page["slug"]))


@bp.post("/page/<slug>/assessment/delete")
def delete(slug: str):
    page = _page(slug)
    _require_manager(page)
    _assessment(page)
    service.delete(page)
    auth.flash_t("assessments.deleted", "success")
    return redirect(url_for("pages.view", slug=page["slug"]))


@bp.get("/page/<slug>/assessment/results")
def results(slug: str):
    page = _page(slug)
    _require_manager(page)
    assessment = _assessment(page)
    return render_template("assessments/results.html", page=page, assessment=assessment,
                           attempts=service.all_attempts(assessment["id"]))


@bp.post("/page/<slug>/assessment/reset/<user_id>")
def reset(slug: str, user_id: str):
    """Delete one user's attempts so they can take the assessment again."""
    page = _page(slug)
    _require_manager(page)
    assessment = _assessment(page)
    if not service.reset_attempts(assessment["id"], user_id):
        abort(404)
    auth.flash_t("assessments.reset_done", "success")
    return redirect(url_for("assessments.results", slug=page["slug"]))


# ── Points ───────────────────────────────────────────────────────────────────


def points_enabled() -> bool:
    return bool(settings.get(POINTS_SETTING))


@bp.get("/settings/assessment-points")
def points():
    if not points_enabled():
        abort(404)
    user = auth.current_user()
    return render_template("assessments/points.html", total=service.total_points(user["id"]),
                           sources=service.point_sources(user["id"]))


@bp.route("/admin/assessments", methods=["GET", "POST"])
@auth.admin_required
def admin():
    if request.method == "POST":
        settings.update({POINTS_SETTING: 1 if request.form.get(POINTS_SETTING) else 0})
        auth.flash_t("common.saved", "success")
        return redirect(url_for("assessments.admin"))
    return render_template("assessments/admin.html", assessments=service.overview(),
                           points_enabled=points_enabled())
