"""Page assessment (poll/test) helpers."""

import json
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


_ALLOWED_QUESTION_TYPES = {"single_choice", "multiple_choice", "free_text"}


def _now_db_format():
    """Return current UTC timestamp as database datetime text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_string_list(value):
    """Normalize input into a trimmed, non-empty list of strings."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def upsert_assessment(page_id, *, title, description, allow_multiple_attempts,
                      banned_roles=None, banned_users=None, updated_by=None):
    """Create or update a page assessment and return its id."""
    banned_roles = _normalize_string_list(banned_roles or [])
    banned_users = _normalize_string_list(banned_users or [])
    now = _now_db_format()
    with get_db_context() as conn:
        existing = conn.execute(
            "SELECT id FROM assessments WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE assessments SET title = ?, description = ?, "
                "allow_multiple_attempts = ?, banned_roles_json = ?, banned_users_json = ?, "
                "updated_by = ?, updated_at = ? WHERE id = ?",
                (
                    title,
                    description,
                    1 if allow_multiple_attempts else 0,
                    json.dumps(banned_roles),
                    json.dumps(banned_users),
                    updated_by,
                    now,
                    existing["id"],
                ),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO assessments (page_id, title, description, allow_multiple_attempts, "
            "banned_roles_json, banned_users_json, created_by, updated_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                page_id,
                title,
                description,
                1 if allow_multiple_attempts else 0,
                json.dumps(banned_roles),
                json.dumps(banned_users),
                updated_by,
                updated_by,
                now,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def delete_assessment(page_id):
    """Delete a page assessment (if present)."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM assessments WHERE page_id = ?", (page_id,))
        conn.commit()


@retry_on_busy
def get_assessment_for_page(page_id):
    """Return an assessment row for page_id, or None."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM assessments WHERE page_id = ?",
            (page_id,),
        ).fetchone()


def set_assessment_questions(assessment_id, questions):
    """Replace all questions for an assessment."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM assessment_questions WHERE assessment_id = ?", (assessment_id,))
        for idx, q in enumerate(questions or []):
            q_type = str(q.get("question_type") or "").strip()
            if q_type not in _ALLOWED_QUESTION_TYPES:
                raise ValueError(f"Invalid question type: {q_type}")
            prompt = str(q.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("Question prompt is required.")
            options = _normalize_string_list(q.get("options", []))
            correct = _normalize_string_list(q.get("correct_answers", []))
            if q_type in ("single_choice", "multiple_choice"):
                if len(options) < 2:
                    raise ValueError("Choice questions require at least two options")
                if not correct:
                    raise ValueError("Choice questions require at least one correct answer")
            points_correct = int(q.get("points_correct", 1))
            points_incorrect = int(q.get("points_incorrect", 0))
            is_required = 1 if q.get("is_required", True) else 0
            conn.execute(
                "INSERT INTO assessment_questions (assessment_id, prompt, question_type, options_json, "
                "correct_answers_json, points_correct, points_incorrect, is_required, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assessment_id,
                    prompt,
                    q_type,
                    json.dumps(options),
                    json.dumps(correct),
                    points_correct,
                    points_incorrect,
                    is_required,
                    idx,
                ),
            )
        conn.commit()


@retry_on_busy
def get_assessment_questions(assessment_id):
    """Return ordered questions for an assessment."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM assessment_questions WHERE assessment_id = ? ORDER BY sort_order, id",
            (assessment_id,),
        ).fetchall()
    return rows


def _normalize_answer(value):
    """Normalize a submitted/expected answer for case-insensitive matching.

    ``None`` is normalized to an empty string so unanswered and blank-text
    submissions are treated uniformly during grading.
    """
    if isinstance(value, list):
        return sorted({str(v).strip().lower() for v in value if str(v).strip()})
    if value is None:
        return ""
    return str(value).strip().lower()


def _score_question(question, submitted):
    """Return (is_correct, awarded_points) for one question submission."""
    q_type = question["question_type"]
    expected_raw = json.loads(question["correct_answers_json"] or "[]")
    expected = _normalize_answer(expected_raw)
    given = _normalize_answer(submitted)
    if q_type == "multiple_choice":
        is_correct = bool(given and given == expected)
    else:  # single_choice / free_text
        is_correct = bool(given and isinstance(given, str) and given in expected)
    points = question["points_correct"] if is_correct else question["points_incorrect"]
    return is_correct, points


@retry_on_busy
def count_user_attempts(assessment_id, user_id):
    """Return how many attempts user_id has submitted for assessment_id."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM assessment_attempts WHERE assessment_id = ? AND user_id = ?",
            (assessment_id, user_id),
        ).fetchone()
    return int(row["c"] if row else 0)


def can_user_attempt(assessment_row, user):
    """Return (ok, reason) for whether user can take the assessment."""
    if not assessment_row:
        return False, "Assessment not found."
    if not user:
        return False, "You must be logged in to continue."
    banned_roles = set(_normalize_string_list(json.loads(assessment_row["banned_roles_json"] or "[]")))
    banned_users = set(_normalize_string_list(json.loads(assessment_row["banned_users_json"] or "[]")))
    if user["role"] in banned_roles:
        return False, "Your role is not allowed to submit this assessment."
    if user["id"] in banned_users:
        return False, "Your account is not allowed to submit this assessment."
    if not assessment_row["allow_multiple_attempts"]:
        if count_user_attempts(assessment_row["id"], user["id"]) > 0:
            return False, "You have already submitted this assessment."
    return True, ""


def submit_assessment_attempt(assessment_row, user_id, submitted_answers):
    """Store a full attempt and return its row id and score tuple."""
    assessment_id = assessment_row["id"]
    questions = get_assessment_questions(assessment_id)
    attempt_number = count_user_attempts(assessment_id, user_id) + 1
    total_points = 0
    max_points = 0
    graded = []
    for q in questions:
        qid = q["id"]
        raw_val = submitted_answers.get(str(qid))
        if raw_val is None:
            raw_val = submitted_answers.get(qid)
        is_correct, awarded = _score_question(q, raw_val)
        total_points += int(awarded)
        max_points += int(q["points_correct"])
        graded.append((qid, raw_val, is_correct, int(awarded)))

    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO assessment_attempts (assessment_id, user_id, attempt_number, total_points, max_points, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (assessment_id, user_id, attempt_number, total_points, max_points, _now_db_format()),
        )
        attempt_id = cur.lastrowid
        for qid, raw_val, is_correct, awarded in graded:
            conn.execute(
                "INSERT INTO assessment_answers (attempt_id, question_id, answer_json, is_correct, awarded_points) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    qid,
                    json.dumps(raw_val),
                    1 if is_correct else 0,
                    awarded,
                ),
            )
        conn.commit()
    return attempt_id, total_points, max_points


@retry_on_busy
def get_assessment_attempts(assessment_id):
    """Return attempts with usernames for admin review."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT a.*, u.username FROM assessment_attempts a "
            "JOIN users u ON u.id = a.user_id "
            "WHERE a.assessment_id = ? ORDER BY a.submitted_at DESC",
            (assessment_id,),
        ).fetchall()


@retry_on_busy
def get_user_attempts_for_assessment(assessment_id, user_id):
    """Return a user's attempts for an assessment, newest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM assessment_attempts WHERE assessment_id = ? AND user_id = ? "
            "ORDER BY submitted_at DESC",
            (assessment_id, user_id),
        ).fetchall()


@retry_on_busy
def get_attempt_answers(attempt_id):
    """Return answer rows joined with question metadata for an attempt."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ans.*, q.prompt, q.question_type, q.correct_answers_json "
            "FROM assessment_answers ans "
            "JOIN assessment_questions q ON q.id = ans.question_id "
            "WHERE ans.attempt_id = ? ORDER BY q.sort_order, q.id",
            (attempt_id,),
        ).fetchall()


def reset_user_attempts(assessment_id, user_id):
    """Delete all attempts for user_id on assessment_id."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM assessment_attempts WHERE assessment_id = ? AND user_id = ?",
            (assessment_id, user_id),
        )
        conn.commit()


@retry_on_busy
def get_user_assessment_points(user_id):
    """Return total points and point source rows for a user."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_points), 0) AS total_points FROM assessment_attempts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        sources = conn.execute(
            "SELECT a.id AS attempt_id, a.total_points, a.max_points, a.submitted_at, "
            "p.title AS page_title, p.slug AS page_slug, ass.title AS assessment_title "
            "FROM assessment_attempts a "
            "JOIN assessments ass ON ass.id = a.assessment_id "
            "JOIN pages p ON p.id = ass.page_id "
            "WHERE a.user_id = ? ORDER BY a.submitted_at DESC",
            (user_id,),
        ).fetchall()
    return int(row["total_points"] if row else 0), sources
