"""Assessments: authoring, taking, scoring, results and access rules."""

from __future__ import annotations

import json
import threading

import pytest
from flask import Blueprint, g

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.assessments import service, slots
from bananawiki.wiki.features.pages import service as pages


@pytest.fixture
def app(app_factory):
    application = app_factory()
    if "pages.view" not in application.view_functions:
        # Stand-in for the pages feature so links to wiki pages can be built.
        stub = Blueprint("pages", __name__)
        stub.add_url_rule("/page/<slug>", "view", lambda slug: slug)
        application.register_blueprint(stub)
    return application


@pytest.fixture
def page(app):
    with app.test_request_context(), connection_scope():
        return pages.create("Fruit facts", "Bananas are berries.", author_id=None)


@pytest.fixture
def editor(make_user):
    return make_user("editor1", role="editor")


@pytest.fixture
def reader(make_user):
    return make_user("reader1")


def _form(**overrides):
    data = {
        "title": "Fruit quiz", "description": "A short check.",
        "questions-0-prompt": "2 + 2 = ?", "questions-0-type": "single_choice",
        "questions-0-options": "3\n4\n5", "questions-0-correct": "4",
        "questions-0-points_correct": "2", "questions-0-points_incorrect": "0", "questions-0-required": "1",
        "questions-1-prompt": "Pick the fruits", "questions-1-type": "multiple_choice",
        "questions-1-options": "Banana\nCarrot\nApple", "questions-1-correct": "banana\nApple",
        "questions-1-points_correct": "3", "questions-1-required": "1",
        "questions-2-prompt": "Name a yellow fruit", "questions-2-type": "free_text",
        "questions-2-correct": "Banana\nLemon", "questions-2-points_correct": "1",
        "questions-3-prompt": "Any comments?", "questions-3-type": "free_text",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _create_quiz(client, login, editor, page, **overrides):
    login(client, editor)
    response = client.post(f"/page/{page['slug']}/assessment/manage", data=_form(**overrides))
    assert response.status_code == 302, response.data[:2000]
    client.post("/logout")


def _questions(db, page):
    assessment = db.one("SELECT * FROM assessments WHERE page_id = ?", (page["id"],))
    rows = db.all("SELECT * FROM assessment_questions WHERE assessment_id = ? ORDER BY sort_order", (assessment["id"],))
    return assessment, rows


def test_editor_creates_quiz_and_answer_key_is_normalised(client, login, editor, page, db):
    _create_quiz(client, login, editor, page)
    assessment, rows = _questions(db, page)
    assert assessment["title"] == "Fruit quiz" and assessment["created_by"] == editor["id"]
    assert [r["question_type"] for r in rows] == ["single_choice", "multiple_choice", "free_text", "free_text"]
    assert json.loads(rows[1]["correct_answers_json"]) == ["Banana", "Apple"]
    assert rows[3]["is_required"] == 0


def test_manage_requires_permission_and_edit_rights(client, login, reader, page, make_user, db):
    login(client, reader)
    assert client.get(f"/page/{page['slug']}/assessment/manage").status_code == 403
    assert client.post(f"/page/{page['slug']}/assessment/manage", data=_form()).status_code == 403
    client.post("/logout")
    limited = make_user("limited_editor", role="editor")
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (limited["id"],))
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'page.edit_all')", (limited["id"],))
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'page.view_all')", (limited["id"],))
    login(client, limited)
    assert client.get(f"/page/{page['slug']}/assessment/manage").status_code == 403


def test_validation_rerenders_form_with_messages(client, login, editor, page, db):
    login(client, editor)
    response = client.post(f"/page/{page['slug']}/assessment/manage",
                           data=_form(**{"questions-0-correct": "7", "questions-1-options": "Banana"}))
    assert response.status_code == 400
    assert b"Every correct answer must be one of the options." in response.data
    assert b"at least two options" in response.data
    assert b"Pick the fruits" in response.data  # the input is kept
    assert db.scalar("SELECT COUNT(*) FROM assessments") == 0
    response = client.post(f"/page/{page['slug']}/assessment/manage", data=_form(banned_users="ghost"))
    assert response.status_code == 400 and b"ghost" in response.data


def test_add_question_without_javascript(client, login, editor, page):
    login(client, editor)
    response = client.post(f"/page/{page['slug']}/assessment/manage", data=_form(add_question="1"))
    assert response.status_code == 200
    assert b'name="questions-4-prompt"' in response.data


def test_take_page_never_contains_the_answer_key(client, login, editor, reader, page):
    _create_quiz(client, login, editor, page, **{"questions-0-options": "alpha\nomega-correct",
                                                 "questions-0-correct": "omega-correct"})
    login(client, reader)
    response = client.get(f"/page/{page['slug']}/assessment")
    assert response.status_code == 200
    assert b"omega-correct" in response.data  # the option is shown...
    assert b"Lemon" not in response.data and b"Banana" in response.data  # ...free-text answers are not
    assert b"correct_answers" not in response.data


def test_scoring_and_attempt_page(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    response = client.post(f"/page/{page['slug']}/assessment/submit", data={
        f"q_{rows[0]['id']}": "1",            # "4": correct, 2 points
        f"q_{rows[1]['id']}": ["0", "2"],     # Banana + Apple: correct, 3 points
        f"q_{rows[2]['id']}": "  LEMON ",     # accepted answer, 1 point
        f"q_{rows[3]['id']}": "Great page",   # open question, not scored
    })
    assert response.status_code == 302
    attempt = db.one("SELECT * FROM assessment_attempts WHERE user_id = ?", (reader["id"],))
    assert (attempt["total_points"], attempt["max_points"], attempt["attempt_number"]) == (6, 6, 1)
    answers = db.all("SELECT * FROM assessment_answers WHERE attempt_id = ? ORDER BY question_id", (attempt["id"],))
    assert [json.loads(a["answer_json"]) for a in answers] == ["4", ["Banana", "Apple"], "LEMON", "Great page"]
    assert [a["is_correct"] for a in answers] == [1, 1, 1, None]
    detail = client.get(response.headers["Location"])
    assert detail.status_code == 200 and b"6/6" in detail.data
    # Single attempt: the key is revealed after submitting.
    assert b"Lemon" in detail.data


def test_wrong_and_partial_answers(app, page, editor, db, client, login):
    _create_quiz(client, login, editor, page, **{"questions-0-points_incorrect": "-1"})
    _, rows = _questions(db, page)
    with app.test_request_context(), connection_scope():
        qs = {q["id"]: q for q in service.questions(rows[0]["assessment_id"])}
        assert service.score(qs[rows[0]["id"]], "3") == (False, -1)
        assert service.score(qs[rows[1]["id"]], ["Banana"]) == (False, 0)
        assert service.score(qs[rows[1]["id"]], ["banana", "APPLE"]) == (True, 3)
        assert service.score(qs[rows[2]["id"]], None) == (False, 0)
        assert service.score(qs[rows[3]["id"]], "anything") == (None, 0)
        assert service.parse_answer(qs[rows[0]["id"]], ["9"]) is None
        assert service.parse_answer(qs[rows[1]["id"]], ["0", "0", "x"]) == ["Banana"]


def test_required_questions_are_enforced(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    response = client.post(f"/page/{page['slug']}/assessment/submit", data={f"q_{rows[0]['id']}": "1"})
    assert response.status_code == 422
    assert b"Answer every required question." in response.data
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 0


def _submit_all(client, page, rows):
    return client.post(f"/page/{page['slug']}/assessment/submit", data={
        f"q_{rows[0]['id']}": "0", f"q_{rows[1]['id']}": "0", f"q_{rows[2]['id']}": "x",
    })


def test_single_attempt_blocks_second_submission(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    assert _submit_all(client, page, rows).status_code == 302
    second = _submit_all(client, page, rows)
    assert second.status_code == 302 and second.headers["Location"].endswith("/assessment")
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 1
    assert b"already submitted" in client.get(f"/page/{page['slug']}/assessment").data


def test_concurrent_single_attempt_submissions_store_one(app, editor, reader, page, db, client, login):
    _create_quiz(client, login, editor, page)
    assessment, rows = _questions(db, page)
    answers = {rows[0]["id"]: "4", rows[1]["id"]: ["Banana"], rows[2]["id"]: "x"}
    outcomes = []

    def worker():
        with app.test_request_context(), connection_scope():
            try:
                service.submit(assessment, page, reader, answers)
                outcomes.append("ok")
            except service.AssessmentError as exc:
                outcomes.append(exc.key)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 1


def test_multiple_attempts_hide_key_and_points_count_best(client, login, editor, reader, page, db, app):
    _create_quiz(client, login, editor, page, allow_multiple_attempts="1")
    _, rows = _questions(db, page)
    login(client, reader)
    first = _submit_all(client, page, rows)
    good = client.post(f"/page/{page['slug']}/assessment/submit", data={
        f"q_{rows[0]['id']}": "1", f"q_{rows[1]['id']}": ["0", "2"], f"q_{rows[2]['id']}": "banana"})
    assert good.status_code == 302
    numbers = db.column("SELECT attempt_number FROM assessment_attempts ORDER BY id")
    assert numbers == [1, 2]
    detail = client.get(first.headers["Location"])
    assert b"Lemon" not in detail.data and b"stay hidden" in detail.data
    with app.test_request_context(), connection_scope():
        assert service.total_points(reader["id"]) == 6


def test_banned_role_and_user(client, login, editor, reader, page, make_user, db):
    _create_quiz(client, login, editor, page, banned_roles="user")
    _, rows = _questions(db, page)
    login(client, reader)
    assert b"Your role may not take this quiz." in client.get(f"/page/{page['slug']}/assessment").data
    _submit_all(client, page, rows)
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 0
    client.post("/logout")
    other_editor = make_user("editor2", role="editor")
    _create_quiz(client, login, editor, page, banned_users="EDITOR2")
    login(client, other_editor)
    assert b"Your account may not take this quiz." in client.get(f"/page/{page['slug']}/assessment").data


def test_attempt_pages_are_private(client, login, editor, reader, page, make_user, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    location = _submit_all(client, page, rows).headers["Location"]
    client.post("/logout")
    login(client, make_user("nosy"))
    assert client.get(location).status_code == 404
    client.post("/logout")
    login(client, editor)
    assert client.get(location).status_code == 200


def test_hidden_page_quiz_is_not_reachable(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    db.execute("UPDATE pages SET is_deindexed = 1 WHERE id = ?", (page["id"],))
    login(client, reader)
    assert client.get(f"/page/{page['slug']}/assessment").status_code == 404


def test_results_reset_and_delete(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    _submit_all(client, page, rows)
    assert client.get(f"/page/{page['slug']}/assessment/results").status_code == 403
    assert client.post(f"/page/{page['slug']}/assessment/reset/{reader['id']}").status_code == 403
    client.post("/logout")
    login(client, editor)
    results = client.get(f"/page/{page['slug']}/assessment/results")
    assert results.status_code == 200 and b"reader1" in results.data
    assert client.post(f"/page/{page['slug']}/assessment/reset/{reader['id']}").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM assessment_attempts") == 0
    assert client.post(f"/page/{page['slug']}/assessment/reset/{reader['id']}").status_code == 404
    assert client.post(f"/page/{page['slug']}/assessment/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM assessments") == 0


def test_editing_keeps_recorded_answers_of_kept_questions(client, login, editor, reader, page, db):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    _submit_all(client, page, rows)
    client.post("/logout")
    login(client, editor)
    form = _form(**{"questions-0-id": str(rows[0]["id"]), "questions-1-id": str(rows[1]["id"]),
                    "questions-2-id": str(rows[2]["id"]), "questions-3-id": str(rows[3]["id"]),
                    "questions-3-remove": "1", "questions-0-prompt": "Two plus two?"})
    assert client.post(f"/page/{page['slug']}/assessment/manage", data=form).status_code == 302
    _, after = _questions(db, page)
    assert [r["id"] for r in after] == [rows[0]["id"], rows[1]["id"], rows[2]["id"]]
    assert after[0]["prompt"] == "Two plus two?"
    assert db.scalar("SELECT COUNT(*) FROM assessment_answers") == 3


def test_foreign_question_ids_are_not_adopted(client, login, editor, page, db, app):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    with app.test_request_context(), connection_scope():
        other = pages.create("Other page", "x", author_id=None)
    login(client, editor)
    client.post(f"/page/{other['slug']}/assessment/manage",
                data=_form(**{"questions-0-id": str(rows[0]["id"]), "questions-0-prompt": "Hijack"}))
    _, original = _questions(db, page)
    assert original[0]["prompt"] == "2 + 2 = ?"


def test_points_page_and_setting(client, login, editor, reader, page, db, admin):
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    login(client, reader)
    _submit_all(client, page, rows)
    assert client.get("/settings/assessment-points").status_code == 404
    assert client.post("/admin/assessments", data={"assessment_points_badge_enabled": "1"}).status_code == 403
    client.post("/logout")
    login(client, admin)
    assert client.get("/admin/assessments").status_code == 200
    client.post("/admin/assessments", data={"assessment_points_badge_enabled": "1"})
    client.post("/logout")
    login(client, reader)
    response = client.get("/settings/assessment-points")
    assert response.status_code == 200 and b"Fruit quiz" in response.data
    assert b"Lemon" not in response.data  # no answer key on the overview


def test_completed_event_is_emitted(app, editor, reader, page, db, client, login):
    _create_quiz(client, login, editor, page)
    assessment, rows = _questions(db, page)
    received = []
    registry = app.extensions["bananawiki.registry"]
    registry._handlers.setdefault("assessment.completed", []).append(("assessments", lambda **kw: received.append(kw)))
    with app.test_request_context(), connection_scope():
        service.submit(assessment, page, reader, {rows[0]["id"]: "4"})
    assert received and received[0]["user_id"] == reader["id"] and received[0]["page_id"] == page["id"]
    assert (received[0]["score"], received[0]["max_score"]) == (2, 6)


def test_slots(app, editor, reader, page, db, client, login):
    with app.test_request_context(), connection_scope():
        g.user = g.real_user = editor
        assert "Add quiz" in slots.page_header_actions(page)
        assert slots.page_below_content(page) == ""
    _create_quiz(client, login, editor, page)
    with app.test_request_context(), connection_scope():
        g.user = g.real_user = reader
        assert slots.page_header_actions(page) == ""
        panel = slots.page_below_content(page)
        assert "Take the quiz" in panel and "Lemon" not in panel
        g.user = g.real_user = None
        assert slots.page_header_actions(page) == ""


def test_disabled_feature_answers_404(app, client, login, editor, page):
    from bananawiki.wiki import registry

    with app.test_request_context(), connection_scope():
        registry.set_enabled("assessments", False)
    login(client, editor)
    assert client.get(f"/page/{page['slug']}/assessment/manage").status_code == 404
