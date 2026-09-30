"""Assessments: one quiz per wiki page, attempts and automatic scoring.

Tables (unchanged from 1.4): ``assessments`` (one row per page),
``assessment_questions``, ``assessment_attempts`` and ``assessment_answers``.

Scoring
-------
* ``single_choice`` - correct when the chosen option is one of the correct answers;
* ``multiple_choice`` - correct when exactly the correct options are chosen;
* ``free_text`` - correct when the text matches an accepted answer, ignoring
  case and surrounding spaces. A free-text question without accepted answers
  is an open (ungraded) question: it scores nothing and does not count
  towards the maximum.

A correct answer earns ``points_correct``; anything else (including no
answer) earns ``points_incorrect``. Stored answers keep the 1.4 shape: a
string for single choice and free text, a list of option texts for multiple
choice, ``null`` when unanswered.

The answer key never leaves the server before a submission. Afterwards it is
shown to managers, and to takers only when they cannot try again.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ....core.timeutil import now_sql
from ... import auth
from ...db import db
from ...permissions import ROLES
from ...registry import emit
from ..pages import service as pages

QUESTION_TYPES = ("single_choice", "multiple_choice", "free_text")
CHOICE_TYPES = frozenset({"single_choice", "multiple_choice"})
MAX_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_PROMPT = 1000
MAX_OPTION = 200
MAX_OPTIONS = 20
MAX_QUESTIONS = 100
MAX_FREE_TEXT = 2000
MAX_POINTS = 1000


class AssessmentError(ValueError):
    """A refused change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


@dataclass
class QuestionInput:
    """One question as submitted by the editor form."""

    prompt: str
    question_type: str
    options: list[str]
    correct_answers: list[str]
    points_correct: int = 1
    points_incorrect: int = 0
    is_required: bool = True
    id: int | None = None
    errors: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


# ── JSON helpers ──────────────────────────────────────────────────────────────


def _load_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _strings(values: list[Any]) -> list[str]:
    return [text for text in (str(v).strip() for v in values if v is not None) if text]


def _normal(value: Any) -> str:
    return str(value).strip().casefold()


def decode_answer(raw: str | None) -> Any:
    try:
        return json.loads(raw or "null")
    except (TypeError, ValueError):
        return raw


# ── Reading ──────────────────────────────────────────────────────────────────


def for_page(page_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM assessments WHERE page_id = ?", (page_id,))


def questions(assessment_id: int) -> list[dict[str, Any]]:
    """Questions with parsed options and answer key (server side only)."""
    rows = db.all(
        "SELECT * FROM assessment_questions WHERE assessment_id = ? ORDER BY sort_order, id", (assessment_id,)
    )
    for row in rows:
        row["options"] = _strings(_load_list(row.pop("options_json")))
        row["correct_answers"] = _strings(_load_list(row.pop("correct_answers_json")))
    return rows


def questions_for_taker(assessment_id: int) -> list[dict[str, Any]]:
    """Questions as the answer form needs them: never the answer key."""
    return [
        {"id": q["id"], "prompt": q["prompt"], "question_type": q["question_type"], "options": q["options"],
         "is_required": bool(q["is_required"]), "points_correct": q["points_correct"]}
        for q in questions(assessment_id)
    ]


def banned_roles(assessment: dict[str, Any]) -> list[str]:
    return [role for role in _strings(_load_list(assessment.get("banned_roles_json"))) if role in ROLES]


def banned_user_ids(assessment: dict[str, Any]) -> list[str]:
    return _strings(_load_list(assessment.get("banned_users_json")))


def banned_usernames(assessment: dict[str, Any]) -> list[str]:
    ids = banned_user_ids(assessment)
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return db.column(f"SELECT username FROM users WHERE id IN ({marks}) ORDER BY username COLLATE NOCASE", ids)


def attempts_of(assessment_id: int, user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT * FROM assessment_attempts WHERE assessment_id = ? AND user_id = ? ORDER BY attempt_number DESC",
        (assessment_id, user_id),
    )


def all_attempts(assessment_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT a.*, u.username FROM assessment_attempts a JOIN users u ON u.id = a.user_id "
        "WHERE a.assessment_id = ? ORDER BY u.username COLLATE NOCASE, a.attempt_number DESC",
        (assessment_id,),
    )


def attempt(attempt_id: int) -> dict[str, Any] | None:
    return db.one(
        "SELECT a.*, u.username FROM assessment_attempts a JOIN users u ON u.id = a.user_id WHERE a.id = ?",
        (attempt_id,),
    )


def attempt_answers(attempt_id: int) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT ans.*, q.prompt, q.question_type, q.correct_answers_json FROM assessment_answers ans "
        "JOIN assessment_questions q ON q.id = ans.question_id WHERE ans.attempt_id = ? "
        "ORDER BY q.sort_order, q.id",
        (attempt_id,),
    )
    for row in rows:
        row["answer"] = decode_answer(row.pop("answer_json"))
        row["correct_answers"] = _strings(_load_list(row.pop("correct_answers_json")))
    return rows


def overview() -> list[dict[str, Any]]:
    """Every assessment with its page and counts (administration)."""
    return db.all(
        "SELECT a.id, a.title, a.allow_multiple_attempts, a.updated_at, p.title AS page_title, p.slug AS page_slug, "
        "(SELECT COUNT(*) FROM assessment_questions q WHERE q.assessment_id = a.id) AS question_count, "
        "(SELECT COUNT(*) FROM assessment_attempts t WHERE t.assessment_id = a.id) AS attempt_count, "
        "(SELECT COUNT(DISTINCT t.user_id) FROM assessment_attempts t WHERE t.assessment_id = a.id) AS taker_count "
        "FROM assessments a JOIN pages p ON p.id = a.page_id ORDER BY p.title COLLATE NOCASE"
    )


# ── Points ───────────────────────────────────────────────────────────────────


def total_points(user_id: str) -> int:
    """A user's points: the best attempt of each assessment, summed.

    Other features (badges) may call this. Unlike 1.4, retaking an
    assessment does not add up points again.
    """
    return int(db.scalar(
        "SELECT COALESCE(SUM(best), 0) FROM (SELECT MAX(total_points) AS best FROM assessment_attempts "
        "WHERE user_id = ? GROUP BY assessment_id)",
        (user_id,), default=0,
    ))


def point_sources(user_id: str) -> list[dict[str, Any]]:
    """The user's attempts with their page, newest first."""
    rows = db.all(
        "SELECT t.id AS attempt_id, t.attempt_number, t.total_points, t.max_points, t.submitted_at, "
        "a.title AS assessment_title, p.title, p.slug, p.category_id, p.pending_deletion, p.is_deindexed, "
        "p.builder_public, CASE WHEN p.builder_json != '' THEN 1 ELSE 0 END AS has_builder "
        "FROM assessment_attempts t "
        "JOIN assessments a ON a.id = t.assessment_id JOIN pages p ON p.id = a.page_id "
        "WHERE t.user_id = ? ORDER BY t.submitted_at DESC, t.id DESC",
        (user_id,),
    )
    sources = []
    for row in rows:
        if not pages.can_view(row):
            continue
        sources.append({
            "attempt_id": row["attempt_id"], "attempt_number": row["attempt_number"],
            "total_points": row["total_points"], "max_points": row["max_points"],
            "submitted_at": row["submitted_at"], "assessment_title": row["assessment_title"],
            "page_title": row["title"], "page_slug": row["slug"],
        })
    return sources


# ── Access rules ─────────────────────────────────────────────────────────────


def can_manage(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Create, edit, delete and review the assessment of *page*."""
    user = auth.current_user() if user is None else user
    return bool(user) and auth.has_permission("assessment.manage", user) and pages.can_edit(page, user)


def can_take(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Open the assessment form of *page* (ignoring bans and used attempts)."""
    user = auth.current_user() if user is None else user
    return bool(user) and auth.has_permission("assessment.view", user) and pages.can_view(page, user)


def block_reason(assessment: dict[str, Any], user: dict[str, Any], *, attempts: int | None = None) -> str | None:
    """Why *user* may not submit now (a translation key), or None."""
    if user.get("role") in banned_roles(assessment):
        return "assessments.blocked.role"
    if user["id"] in banned_user_ids(assessment):
        return "assessments.blocked.user"
    if not assessment["allow_multiple_attempts"]:
        count = attempts if attempts is not None else db.scalar(
            "SELECT COUNT(*) FROM assessment_attempts WHERE assessment_id = ? AND user_id = ?",
            (assessment["id"], user["id"]), default=0,
        )
        if count:
            return "assessments.blocked.already_submitted"
    return None


def reveals_answers(assessment: dict[str, Any], page: dict[str, Any], user: dict[str, Any]) -> bool:
    """Whether *user* may see the answer key of a submitted attempt."""
    return can_manage(page, user) or block_reason(assessment, user, attempts=1) is not None


# ── Authoring ────────────────────────────────────────────────────────────────


def _points(raw: Any, default: int, errors: list, key: str) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        errors.append(("assessments.error.points_invalid", {"field": key}))
        return default
    if abs(value) > MAX_POINTS:
        errors.append(("assessments.error.points_range", {"maximum": MAX_POINTS}))
        return default
    return value


def _lines(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def question_from_form(data: dict[str, Any]) -> QuestionInput | None:
    """Validate one question of the editor form; None for an untouched blank slot."""
    prompt = (data.get("prompt") or "").strip()
    options = _lines(data.get("options"))
    correct = _lines(data.get("correct"))
    if not prompt and not options and not correct and not data.get("id"):
        return None
    errors: list[tuple[str, dict[str, Any]]] = []
    question_type = data.get("type") or ""
    if question_type not in QUESTION_TYPES:
        errors.append(("assessments.error.type_invalid", {}))
        question_type = "single_choice"
    if not prompt:
        errors.append(("assessments.error.prompt_required", {}))
    elif len(prompt) > MAX_PROMPT:
        errors.append(("assessments.error.prompt_too_long", {"maximum": MAX_PROMPT}))
    if question_type in CHOICE_TYPES:
        _check_choices(question_type, options, correct, errors)
    else:
        options = []
        if any(len(answer) > MAX_OPTION for answer in correct):
            errors.append(("assessments.error.option_too_long", {"maximum": MAX_OPTION}))
    try:
        question_id = int(data["id"]) if data.get("id") else None
    except ValueError:
        question_id = None
    return QuestionInput(
        prompt=prompt[:MAX_PROMPT], question_type=question_type, options=options, correct_answers=correct,
        points_correct=_points(data.get("points_correct"), 1, errors, "points_correct"),
        points_incorrect=_points(data.get("points_incorrect"), 0, errors, "points_incorrect"),
        is_required=bool(data.get("required")), id=question_id, errors=errors,
    )


def _check_choices(question_type: str, options: list[str], correct: list[str], errors: list) -> None:
    folded = [_normal(option) for option in options]
    if len(options) < 2:
        errors.append(("assessments.error.options_min", {}))
    if len(options) > MAX_OPTIONS:
        errors.append(("assessments.error.options_max", {"maximum": MAX_OPTIONS}))
    if len(set(folded)) != len(folded):
        errors.append(("assessments.error.options_duplicate", {}))
    if any(len(option) > MAX_OPTION for option in options):
        errors.append(("assessments.error.option_too_long", {"maximum": MAX_OPTION}))
    if not correct:
        errors.append(("assessments.error.correct_required", {}))
    elif any(_normal(answer) not in folded for answer in correct):
        errors.append(("assessments.error.correct_not_option", {}))
    elif question_type == "single_choice" and len({_normal(a) for a in correct}) > 1:
        errors.append(("assessments.error.single_one_correct", {}))
    # Store the correct answers spelled exactly like their options.
    by_fold = dict(zip(folded, options, strict=True))
    correct[:] = list(dict.fromkeys(by_fold.get(_normal(answer), answer) for answer in correct))


@dataclass
class Settings:
    title: str
    description: str
    allow_multiple_attempts: bool
    banned_roles: list[str]
    banned_usernames: list[str]


def resolve_usernames(names: list[str]) -> tuple[list[str], list[str]]:
    """User ids for *names*, and the names that match no account."""
    ids, unknown = [], []
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        row = db.one("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (name,))
        if row:
            ids.append(row["id"])
        else:
            unknown.append(name)
    return list(dict.fromkeys(ids)), unknown


def save(page: dict[str, Any], settings: Settings, question_inputs: list[QuestionInput], *,
         actor_id: str) -> dict[str, Any]:
    """Create or update the page's assessment and its questions.

    Existing questions are updated in place (their recorded answers stay);
    questions left out of the form are deleted with their answers.
    """
    title = settings.title.strip()
    if not title:
        raise AssessmentError("assessments.error.title_required")
    if len(title) > MAX_TITLE:
        raise AssessmentError("assessments.error.title_too_long", maximum=MAX_TITLE)
    if len(settings.description) > MAX_DESCRIPTION:
        raise AssessmentError("assessments.error.description_too_long", maximum=MAX_DESCRIPTION)
    if not question_inputs:
        raise AssessmentError("assessments.error.questions_required")
    if len(question_inputs) > MAX_QUESTIONS:
        raise AssessmentError("assessments.error.questions_max", maximum=MAX_QUESTIONS)
    if any(q.errors for q in question_inputs):
        raise AssessmentError("assessments.error.fix_questions")
    user_ids, unknown = resolve_usernames(settings.banned_usernames)
    if unknown:
        raise AssessmentError("assessments.error.unknown_users", names=", ".join(unknown))
    roles = [role for role in dict.fromkeys(settings.banned_roles) if role in ROLES]
    now = now_sql()
    values = {
        "title": title, "description": settings.description.strip(),
        "allow_multiple_attempts": 1 if settings.allow_multiple_attempts else 0,
        "banned_roles_json": json.dumps(roles), "banned_users_json": json.dumps(user_ids),
        "updated_by": actor_id, "updated_at": now,
    }
    with db.transaction():
        existing = for_page(page["id"])
        if existing:
            assessment_id = existing["id"]
            db.update("assessments", values, "id = ?", (assessment_id,))
        else:
            assessment_id = db.insert("assessments", {**values, "page_id": page["id"], "created_by": actor_id,
                                                      "created_at": now})
        _replace_questions(assessment_id, question_inputs)
    return for_page(page["id"])  # type: ignore[return-value]


def _replace_questions(assessment_id: int, question_inputs: list[QuestionInput]) -> None:
    current = set(db.column("SELECT id FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)))
    kept: set[int] = set()
    for order, q in enumerate(question_inputs):
        values = {
            "prompt": q.prompt, "question_type": q.question_type, "options_json": json.dumps(q.options),
            "correct_answers_json": json.dumps(q.correct_answers), "points_correct": q.points_correct,
            "points_incorrect": q.points_incorrect, "is_required": 1 if q.is_required else 0, "sort_order": order,
        }
        if q.id in current and q.id not in kept:
            db.update("assessment_questions", values, "id = ?", (q.id,))
            kept.add(q.id)  # type: ignore[arg-type]
        else:
            db.insert("assessment_questions", {**values, "assessment_id": assessment_id})
    for question_id in current - kept:
        db.execute("DELETE FROM assessment_questions WHERE id = ?", (question_id,))


def delete(page: dict[str, Any]) -> None:
    db.execute("DELETE FROM assessments WHERE page_id = ?", (page["id"],))


def reset_attempts(assessment_id: int, user_id: str) -> int:
    return db.execute(
        "DELETE FROM assessment_attempts WHERE assessment_id = ? AND user_id = ?", (assessment_id, user_id)
    ).rowcount


# ── Taking ───────────────────────────────────────────────────────────────────


def parse_answer(question: dict[str, Any], values: list[str]) -> Any:
    """Turn the submitted form values of one question into its stored answer."""
    if question["question_type"] == "free_text":
        text = (values[0] if values else "").strip()
        return text[:MAX_FREE_TEXT] or None
    chosen = []
    for value in values:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(question["options"]):
            chosen.append(question["options"][index])
    chosen = list(dict.fromkeys(chosen))
    if question["question_type"] == "single_choice":
        return chosen[0] if chosen else None
    return chosen or None


def is_graded(question: dict[str, Any]) -> bool:
    return question["question_type"] in CHOICE_TYPES or bool(question["correct_answers"])


def score(question: dict[str, Any], answer: Any) -> tuple[bool | None, int]:
    """(is_correct, awarded points) for one answer; is_correct is None when ungraded."""
    if not is_graded(question):
        return None, 0
    expected = {_normal(value) for value in question["correct_answers"]}
    if question["question_type"] == "multiple_choice":
        given = {_normal(value) for value in (answer if isinstance(answer, list) else [answer]) if value}
        correct = bool(given) and given == expected
    else:
        correct = answer is not None and not isinstance(answer, list) and _normal(answer) in expected
    return correct, question["points_correct"] if correct else question["points_incorrect"]


def missing_required(question_rows: list[dict[str, Any]], answers: dict[int, Any]) -> list[int]:
    return [q["id"] for q in question_rows if q["is_required"] and answers.get(q["id"]) is None]


def submit(assessment: dict[str, Any], page: dict[str, Any], user: dict[str, Any],
           answers: dict[int, Any]) -> dict[str, Any]:
    """Score and store one attempt. Raises AssessmentError when not allowed."""
    question_rows = questions(assessment["id"])
    if not question_rows:
        raise AssessmentError("assessments.error.no_questions")
    graded = []
    total = maximum = 0
    for q in question_rows:
        answer = answers.get(q["id"])
        is_correct, awarded = score(q, answer)
        total += awarded
        if is_graded(q):
            maximum += q["points_correct"]
        graded.append((q["id"], answer, is_correct, awarded))
    try:
        with db.transaction():
            reason = block_reason(assessment, user)
            if reason:
                raise AssessmentError(reason)
            number = int(db.scalar(
                "SELECT COALESCE(MAX(attempt_number), 0) FROM assessment_attempts "
                "WHERE assessment_id = ? AND user_id = ?", (assessment["id"], user["id"]), default=0,
            )) + 1
            attempt_id = db.insert("assessment_attempts", {
                "assessment_id": assessment["id"], "user_id": user["id"], "attempt_number": number,
                "total_points": total, "max_points": maximum, "submitted_at": now_sql(),
            })
            db.executemany(
                "INSERT INTO assessment_answers (attempt_id, question_id, answer_json, is_correct, awarded_points) "
                "VALUES (?, ?, ?, ?, ?)",
                [(attempt_id, qid, json.dumps(answer), None if ok is None else int(ok), points)
                 for qid, answer, ok, points in graded],
            )
    except sqlite3.IntegrityError as error:
        raise AssessmentError("assessments.blocked.already_submitted") from error
    emit("assessment.completed", user_id=user["id"], page_id=page["id"], assessment_id=assessment["id"],
         attempt_id=attempt_id, score=total, max_score=maximum)
    return attempt(attempt_id)  # type: ignore[return-value]
