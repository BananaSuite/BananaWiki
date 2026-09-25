"""Page assessment (poll/test) routes."""

import json

from flask import render_template, request, redirect, url_for, flash, abort, g

import db
from helpers import (
    login_required, editor_required, admin_required, rate_limit,
    get_current_user, user_can_view_page, editor_has_category_access,
    t,
)
from wiki_logger import log_action


def _assessment_plugin_enabled():
    """Return True when the assessments plugin is enabled for this request."""
    enabled_plugins = getattr(g, "enabled_plugins", {})
    return bool(enabled_plugins.get("assessments"))


def _load_page_and_assessment(slug):
    """Load page/user/assessment with standard visibility checks."""
    page = db.get_page_by_slug(slug)
    if not page:
        abort(404)
    user = get_current_user()
    if not user_can_view_page(user, page):
        abort(403)
    assessment = db.get_assessment_for_page(page["id"])
    return page, user, assessment


def _parse_questions_json(raw_json):
    """Validate and parse submitted questions JSON payload."""
    if raw_json is None or str(raw_json).strip() == "":
        raise ValueError("Questions JSON is required to continue.")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Questions JSON contains invalid syntax. Please verify the JSON formatting.") from exc
    if not isinstance(data, list):
        raise ValueError("Questions JSON must be an array.")
    if not data:
        raise ValueError("At least one question is required to continue.")
    return data


def _serialize_questions_for_template(question_rows):
    """Convert DB question rows into template-friendly dicts."""
    out = [dict(q) for q in question_rows]
    for q in out:
        q["options"] = json.loads(q.get("options_json") or "[]")
        q["correct_answers"] = json.loads(q.get("correct_answers_json") or "[]")
        q.pop("options_json", None)
        q.pop("correct_answers_json", None)
    return out


def _hydrate_answer_rows(answer_rows):
    """Attach parsed answer/correct values for display."""
    rows = [dict(r) for r in answer_rows]
    for row in rows:
        try:
            row["answer_value"] = json.loads(row["answer_json"] or "null")
        except json.JSONDecodeError:
            row["answer_value"] = row["answer_json"]
        try:
            row["correct_values"] = json.loads(row["correct_answers_json"] or "[]")
        except json.JSONDecodeError:
            row["correct_values"] = []
    return rows


def register_assessment_routes(app):
    """Register assessment routes."""

    @app.route("/page/<slug>/assessment")
    @login_required
    def take_assessment(slug):
        """Render assessment form and previous attempt summary."""
        if not _assessment_plugin_enabled():
            abort(404)
        user = get_current_user()
        if not db.has_permission(user, "assessment.view"):
            flash(t("flash.you_do_not_have_permission_to_take_assessments"), "error")
            return redirect(url_for("home"))
        page, user, assessment = _load_page_and_assessment(slug)
        if not assessment:
            flash(t("flash.no_assessment_is_configured_for_this_page"), "error")
            return redirect(url_for("view_page", slug=slug))
        can_attempt, reason = db.can_user_attempt(assessment, user)
        attempts = db.get_user_attempts_for_assessment(assessment["id"], user["id"])
        questions = _serialize_questions_for_template(db.get_assessment_questions(assessment["id"]))
        if not can_attempt and not attempts:
            flash(reason, "error")
            return redirect(url_for("view_page", slug=slug))
        return render_template(
            "wiki/assessment_take.html",
            page=page,
            assessment=assessment,
            questions=questions,
            attempts=attempts,
            can_attempt=can_attempt,
            block_reason=reason,
        )

    @app.route("/page/<slug>/assessment/submit", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def submit_assessment(slug):
        """Submit one assessment attempt."""
        if not _assessment_plugin_enabled():
            abort(404)
        user = get_current_user()
        if not db.has_permission(user, "assessment.view"):
            abort(403)
        page, user, assessment = _load_page_and_assessment(slug)
        if not assessment:
            abort(404)
        can_attempt, reason = db.can_user_attempt(assessment, user)
        if not can_attempt:
            flash(reason, "error")
            return redirect(url_for("take_assessment", slug=slug))
        submitted = {}
        for key in request.form:
            if key.startswith("q_"):
                qid = key[2:]
                vals = request.form.getlist(key)
                submitted[qid] = vals if len(vals) > 1 else (vals[0] if vals else "")
        _attempt_id, total_points, max_points = db.submit_assessment_attempt(assessment, user["id"], submitted)
        flash(t("flash.assessment_submitted_successfully_score_totalpointsmaxpoints", total_points=total_points, max_points=max_points), "success")
        log_action(
            "assessment_submit",
            request,
            user=user,
            page_slug=page["slug"],
            assessment_id=assessment["id"],
            points=total_points,
            max_points=max_points,
        )
        return redirect(url_for("take_assessment", slug=slug))

    @app.route("/page/<slug>/assessment/manage", methods=["GET", "POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def manage_assessment(slug):
        """Create/update/delete an assessment for a wiki page."""
        if not _assessment_plugin_enabled():
            abort(404)
        user = get_current_user()
        if not db.has_permission(user, "assessment.manage"):
            flash(t("flash.you_do_not_have_permission_to_manage_assessments"), "error")
            return redirect(url_for("home"))
        page, user, assessment = _load_page_and_assessment(slug)
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_the_required_permissions_to"), "error")
            abort(403)

        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "delete":
                db.delete_assessment(page["id"])
                flash(t("flash.assessment_has_been_successfully_deleted"), "success")
                log_action("assessment_delete", request, user=user, page_slug=page["slug"])
                return redirect(url_for("view_page", slug=slug))

            title = (request.form.get("title") or "").strip()[:200]
            description = (request.form.get("description") or "").strip()[:2000]
            if not title:
                flash(t("flash.assessment_title_is_required_to_continue"), "error")
                return redirect(url_for("manage_assessment", slug=slug))
            allow_multiple = bool(request.form.get("allow_multiple_attempts"))
            banned_roles = request.form.getlist("banned_roles")
            banned_users_csv = (request.form.get("banned_users") or "").strip()
            banned_users = [u for u in (x.strip() for x in banned_users_csv.split(",")) if u]
            try:
                questions = _parse_questions_json(request.form.get("questions_json", "[]"))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("manage_assessment", slug=slug))
            try:
                assessment_id = db.upsert_assessment(
                    page["id"],
                    title=title,
                    description=description,
                    allow_multiple_attempts=allow_multiple,
                    banned_roles=banned_roles,
                    banned_users=banned_users,
                    updated_by=user["id"],
                )
            except db.IntegrityError:
                flash(t("flash.failed_to_save_assessment"), "error")
                return redirect(url_for("manage_assessment", slug=slug))
            try:
                db.set_assessment_questions(assessment_id, questions)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("manage_assessment", slug=slug))
            flash(t("flash.assessment_has_been_successfully_saved"), "success")
            log_action("assessment_save", request, user=user, page_slug=page["slug"], assessment_id=assessment_id)
            return redirect(url_for("manage_assessment", slug=slug))

        questions = []
        banned_roles = []
        banned_users = []
        if assessment:
            questions = _serialize_questions_for_template(db.get_assessment_questions(assessment["id"]))
            banned_roles = json.loads(assessment["banned_roles_json"] or "[]")
            banned_users = json.loads(assessment["banned_users_json"] or "[]")
        questions_json = json.dumps(questions, indent=2)
        return render_template(
            "wiki/assessment_manage.html",
            page=page,
            assessment=assessment,
            questions_json=questions_json,
            banned_roles=banned_roles,
            banned_users=",".join(banned_users),
        )

    @app.route("/page/<slug>/assessment/results")
    @login_required
    @admin_required
    def assessment_results(slug):
        """Admin view for all attempts on a page assessment."""
        if not _assessment_plugin_enabled():
            abort(404)
        page, _user, assessment = _load_page_and_assessment(slug)
        if not assessment:
            abort(404)
        attempts = db.get_assessment_attempts(assessment["id"])
        attempt_details = {}
        for a in attempts:
            attempt_details[a["id"]] = _hydrate_answer_rows(db.get_attempt_answers(a["id"]))
        return render_template(
            "wiki/assessment_results.html",
            page=page,
            assessment=assessment,
            attempts=attempts,
            attempt_details=attempt_details,
        )

    @app.route("/page/<slug>/assessment/reset/<user_id>", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def assessment_reset_user(slug, user_id):
        """Allow admins to clear a user's attempts for retakes."""
        if not _assessment_plugin_enabled():
            abort(404)
        page, user, assessment = _load_page_and_assessment(slug)
        if not assessment:
            abort(404)
        db.reset_user_attempts(assessment["id"], user_id)
        flash(t("flash.assessment_attempts_have_been_successfully_reset_for_this"), "success")
        log_action("assessment_reset_user", request, user=user, page_slug=page["slug"], target_user=user_id)
        return redirect(url_for("assessment_results", slug=slug))

    @app.route("/settings/assessment-points")
    @login_required
    def user_assessment_points():
        """Show a points badge and source breakdown for the current user."""
        if not _assessment_plugin_enabled():
            abort(404)
        settings = db.get_site_settings() or {}
        if not settings.get("assessment_points_badge_enabled"):
            abort(404)
        user = get_current_user()
        total_points, sources = db.get_user_assessment_points(user["id"])
        source_answers = {}
        for row in sources:
            source_answers[row["attempt_id"]] = _hydrate_answer_rows(
                db.get_attempt_answers(row["attempt_id"])
            )
        return render_template(
            "account/assessment_points.html",
            total_points=total_points,
            sources=sources,
            source_answers=source_answers,
        )
