"""Tests for page assessment (poll/test) feature."""

import json

import db


def _create_page(user_id, title="Assessment Page", slug="assessment-page"):
    return db.create_page(title, slug, "Assessment content", user_id=user_id)


def _questions_payload(points_correct=2):
    return json.dumps([
        {
            "prompt": "2 + 2 = ?",
            "question_type": "single_choice",
            "options": ["3", "4", "5"],
            "correct_answers": ["4"],
            "points_correct": points_correct,
            "points_incorrect": 0,
            "is_required": True,
        },
        {
            "prompt": "Name one yellow fruit",
            "question_type": "free_text",
            "options": [],
            "correct_answers": ["banana"],
            "points_correct": 3,
            "points_incorrect": 0,
            "is_required": True,
        },
    ])


def test_editor_can_create_assessment_for_page(client, admin_user, editor_user):
    page_id = _create_page(admin_user, slug="assessment-manage")
    page = db.get_page(page_id)
    client.post("/login", data={"username": "editor", "password": "editor123"})

    response = client.post(
        f"/page/{page['slug']}/assessment/manage",
        data={
            "action": "save",
            "title": "Quick Test",
            "description": "A short checkpoint.",
            "questions_json": _questions_payload(),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Assessment has been successfully saved." in response.data
    assessment = db.get_assessment_for_page(page["id"])
    assert assessment is not None
    assert assessment["title"] == "Quick Test"
    assert len(db.get_assessment_questions(assessment["id"])) == 2


def test_single_attempt_only_blocks_second_submit(client, admin_user, regular_user):
    page_id = _create_page(admin_user, slug="assessment-once")
    page = db.get_page(page_id)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(
        f"/page/{page['slug']}/assessment/manage",
        data={
            "action": "save",
            "title": "One Attempt",
            "description": "",
            "questions_json": _questions_payload(),
        },
    )
    assessment = db.get_assessment_for_page(page["id"])
    q = db.get_assessment_questions(assessment["id"])[0]

    client.get("/logout")
    client.post("/login", data={"username": "user", "password": "user123"})
    first = client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{q['id']}": "4"}, follow_redirects=True)
    second = client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{q['id']}": "4"}, follow_redirects=True)

    assert first.status_code == 200
    assert b"Assessment submitted successfully." in first.data
    assert b"You have already submitted this assessment." in second.data


def test_multiple_attempts_enabled_allows_retake(client, admin_user, regular_user):
    page_id = _create_page(admin_user, slug="assessment-multi")
    page = db.get_page(page_id)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(
        f"/page/{page['slug']}/assessment/manage",
        data={
            "action": "save",
            "title": "Retake Allowed",
            "description": "",
            "allow_multiple_attempts": "1",
            "questions_json": _questions_payload(),
        },
    )
    assessment = db.get_assessment_for_page(page["id"])
    q = db.get_assessment_questions(assessment["id"])[0]

    client.get("/logout")
    client.post("/login", data={"username": "user", "password": "user123"})
    client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{q['id']}": "4"})
    second = client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{q['id']}": "4"})

    assert second.status_code in (302, 303)
    assert db.count_user_attempts(assessment["id"], regular_user) == 2


def test_banned_role_cannot_submit_assessment(client, admin_user, regular_user):
    page_id = _create_page(admin_user, slug="assessment-banned")
    page = db.get_page(page_id)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(
        f"/page/{page['slug']}/assessment/manage",
        data={
            "action": "save",
            "title": "Role Ban",
            "description": "",
            "banned_roles": "user",
            "questions_json": _questions_payload(),
        },
    )

    client.get("/logout")
    client.post("/login", data={"username": "user", "password": "user123"})
    response = client.get(f"/page/{page['slug']}/assessment", follow_redirects=True)
    assert response.status_code == 200
    assert b"not allowed to submit" in response.data


def test_admin_can_review_results_and_reset_user_attempts(client, admin_user, regular_user):
    page_id = _create_page(admin_user, slug="assessment-results")
    page = db.get_page(page_id)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(
        f"/page/{page['slug']}/assessment/manage",
        data={
            "action": "save",
            "title": "Admin Review",
            "description": "",
            "questions_json": _questions_payload(),
        },
    )
    assessment = db.get_assessment_for_page(page["id"])
    q = db.get_assessment_questions(assessment["id"])[0]

    client.get("/logout")
    client.post("/login", data={"username": "user", "password": "user123"})
    client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{q['id']}": "4"})

    client.get("/logout")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    results = client.get(f"/page/{page['slug']}/assessment/results")
    assert results.status_code == 200
    assert b"Admin Review" in results.data
    assert b"user" in results.data

    reset = client.post(f"/page/{page['slug']}/assessment/reset/{regular_user}", follow_redirects=True)
    assert reset.status_code == 200
    assert db.count_user_attempts(assessment["id"], regular_user) == 0


def test_account_points_badge_route_requires_admin_setting(client, admin_user, regular_user):
    client.post("/login", data={"username": "user", "password": "user123"})
    hidden = client.get("/settings/assessment-points")
    assert hidden.status_code == 404

    db.update_site_settings(assessment_points_badge_enabled=1)
    shown = client.get("/settings/assessment-points")
    assert shown.status_code == 200
    assert b"Assessment Points" in shown.data


def test_assessment_route_404_when_plugin_disabled(client, admin_user):
    page_id = _create_page(admin_user, slug="assessment-plugin-off")
    page = db.get_page(page_id)
    db.disable_plugin("assessments")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    response = client.get(f"/page/{page['slug']}/assessment")
    assert response.status_code == 404
