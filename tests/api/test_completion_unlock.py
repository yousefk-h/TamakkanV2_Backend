"""COMPLETION unlock policy (`app.models.program.UnlockPolicy.COMPLETION`):
a lesson becomes eligible only once the previous lesson is genuinely done,
never by calendar date, verified end-to-end through the real API (the same
server-side entitlement check that already denies direct access to a locked
topic). Each lesson type's completion signal is exercised separately - a
Standard lesson's answerable exercises, an Interactive Video's checkpoints
(exercise + Practice-with-Mariam), and a standalone Practice-with-Mariam-only
topic - since `TopicCompletionService` treats them differently and none of
them may fall back to the weak legacy `_had_a_real_conversation` heuristic.
"""

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.topic_content import (
    CheckpointKind,
    InteractiveVideoCheckpoint,
    InteractiveVideoCompletionRule,
    Level,
    LessonFlow,
    TopicContent,
)
from app.models.user import Role, User
from app.schema.challenge import LessonDayState
from app.utils.time import utcnow

FAKE_INTERNAL_API_KEY = "fake-internal-agent-key"


@pytest.fixture(autouse=True)
def _fake_livekit_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake-secret-thats-long-enough")
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", FAKE_INTERNAL_API_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _login(client, email: str, role: Role, **extra):
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"}).json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


def _create_completion_program(client, admin_headers, code: str, name: str):
    response = client.post("/api/v1/programs", json={
        "code": code, "name": name, "language": "English",
        "base_levels": ["beginner"], "level_profile_ids": [], "unlock_policy": "completion",
    }, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _stage_id(program: dict) -> str:
    return program["levels"][0]["id"]


def _create_cohort(client, admin_headers, *, name, program, stage_id, lead_teacher_id):
    response = client.post("/api/v1/cohorts", json={
        "name": name, "program_id": program["id"], "program_level_ids": [stage_id],
        "lead_teacher_id": lead_teacher_id, "teacher_assignments": [], "start_date": utcnow().isoformat(),
    }, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _enroll(client, admin_headers, cohort_id: str, student_id: str, stage_id: str):
    response = client.post(f"/api/v1/cohorts/{cohort_id}/students", json={
        "student_ids": [student_id], "program_level_id": stage_id,
    }, headers=admin_headers)
    assert response.status_code == 201, response.text


def _cohort_calendar_id(client, admin_headers, cohort_id: str) -> str:
    templates = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    return next(t for t in templates if t["cohort_id"] == cohort_id)["id"]


def _schedule_one_day(client, admin_headers, template_id: str, topic_ids: list[str]):
    """One day-slot holding several topics, each gated as its own
    independent node (requirement: a day is never one bundled unit)."""
    response = client.patch(f"/api/v1/calendar-templates/{template_id}", json={
        "days": [{"day": 1, "topic_ids": topic_ids, "custom_minutes": {}}],
    }, headers=admin_headers)
    assert response.status_code == 200, response.text


def _create_exercise_topic(client, teacher_headers, program: dict, stage_id: str, title: str) -> dict:
    topic = client.post("/api/v1/topics", json={
        "title": title, "level": "beginner",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Pick", "options": ["A", "B"], "correct_option": 0}],
    }, headers=teacher_headers).json()
    placed = client.post(f"/api/v1/programs/{program['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": stage_id, "status": "published",
    }, headers=teacher_headers)
    assert placed.status_code == 201, placed.text
    # Publishing pins a revision snapshot and saves the Topic, bumping `updated_at` -
    # re-fetch so callers submit an attempt against the current value, not a stale one.
    return client.get(f"/api/v1/topics/{topic['id']}", headers=teacher_headers).json()


async def _publish_direct(program: dict, stage_id: str, topic: TopicContent, teacher_id: str) -> None:
    """Direct-insert path for a topic authored structurally (Interactive
    Video checkpoints, or deliberately zero exercises) rather than through
    the Lesson Builder API - mirrors `tests/api/test_sessions.py`'s own
    `_make_checkpoint_topic` convention."""
    await ProgramTopic(
        program_id=program["id"], topic_id=topic.id, program_level_id=stage_id,
        level=Level.BEGINNER, status=ProgramTopicStatus.PUBLISHED,
        assigned_by_id=teacher_id, published_revision=topic.content_revision,
    ).insert()


def _state_for(calendar: dict, topic_id: str) -> str | None:
    for day in calendar["lesson_days"]:
        for slot in day["topics"]:
            if slot["topic_id"] == topic_id:
                return slot["state"]
    return None


async def test_completion_policy_unlocks_standard_lessons_one_at_a_time_independent_of_calendar_day(client):
    """Three Standard lessons placed in ONE day-slot (never one bundled
    unit) unlock strictly in order as their own exercises are answered
    correctly - never by elapsed time, and a locked lesson is denied via the
    real entitlement check, not just hidden by the frontend."""
    admin, admin_headers = await _login(client, "cu-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "cu-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "cu-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_completion_program(client, admin_headers, "cu-general", "Completion General")
    assert program["unlock_policy"] == "completion"
    stage = _stage_id(program)
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort["id"], student.id, stage)

    t1 = _create_exercise_topic(client, teacher_headers, program, stage, "Lesson 1")
    t2 = _create_exercise_topic(client, teacher_headers, program, stage, "Lesson 2")
    t3 = _create_exercise_topic(client, teacher_headers, program, stage, "Lesson 3")
    template_id = _cohort_calendar_id(client, admin_headers, cohort["id"])
    _schedule_one_day(client, admin_headers, template_id, [t1["id"], t2["id"], t3["id"]])

    calendar = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert calendar["unlock_policy"] == "completion"
    assert _state_for(calendar, t1["id"]) == LessonDayState.CURRENT.value
    assert _state_for(calendar, t2["id"]) == LessonDayState.LOCKED.value
    assert _state_for(calendar, t3["id"]) == LessonDayState.LOCKED.value

    # Direct API access to the locked lesson is denied server-side - not just hidden by the UI.
    denied = client.get(f"/api/v1/topics/{t2['id']}", headers=student_headers)
    assert denied.status_code == 403, denied.text

    # A wrong answer does not unlock the next lesson.
    wrong = client.post("/api/v1/exercise-attempts", json={
        "topic_id": t1["id"], "exercise_index": 0, "topic_updated_at": t1["updated_at"], "answer": 1,
    }, headers=student_headers)
    assert wrong.status_code == 201, wrong.text
    still_locked = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(still_locked, t1["id"]) == LessonDayState.CURRENT.value
    assert _state_for(still_locked, t2["id"]) == LessonDayState.LOCKED.value

    # The correct answer unlocks the NEXT lesson immediately - no arbitrary wait.
    right = client.post("/api/v1/exercise-attempts", json={
        "topic_id": t1["id"], "exercise_index": 0, "topic_updated_at": t1["updated_at"], "answer": 0,
    }, headers=student_headers)
    assert right.status_code == 201, right.text
    after = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(after, t1["id"]) == LessonDayState.DONE.value
    assert _state_for(after, t2["id"]) == LessonDayState.CURRENT.value
    assert _state_for(after, t3["id"]) == LessonDayState.LOCKED.value
    now_open = client.get(f"/api/v1/topics/{t2['id']}", headers=student_headers)
    assert now_open.status_code == 200, now_open.text


async def test_completion_policy_unlocks_interactive_video_only_after_every_checkpoint(client):
    """An Interactive Video lesson with one EXERCISE checkpoint (CORRECT
    rule) and one PRACTICE_WITH_MARIAM checkpoint only completes - and
    unlocks the next lesson - once BOTH are genuinely satisfied."""
    admin, admin_headers = await _login(client, "cu-iv-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "cu-iv-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "cu-iv-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_completion_program(client, admin_headers, "cu-iv", "Completion Interactive")
    stage = _stage_id(program)
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort["id"], student.id, stage)

    exercise_checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=10, kind=CheckpointKind.EXERCISE, exercise_index=0,
        completion_rule=InteractiveVideoCompletionRule.CORRECT,
    )
    practice_checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=30, kind=CheckpointKind.PRACTICE_WITH_MARIAM, exercise_index=None,
        learning_objective="Order a coffee.", conversation_goal="Practice ordering a coffee.",
        opening_line="Let's practice ordering a coffee.", session_duration_minutes=2,
    )
    iv_topic = TopicContent(
        title="Interactive lesson", level=Level.BEGINNER, lesson_flow=LessonFlow.INTERACTIVE_VIDEO,
        uploaded_video_url="https://cdn.example.test/lesson.mp4",
        interactive_video_checkpoints=[exercise_checkpoint, practice_checkpoint],
        lesson_exercises=[{"kind": "multiple_choice", "prompt": "Pick", "options": ["A", "B"], "correct_option": 0}],
        created_by_id=teacher.id,
    )
    await iv_topic.insert()
    await _publish_direct(program, stage, iv_topic, teacher.id)

    next_topic = _create_exercise_topic(client, teacher_headers, program, stage, "After interactive")
    template_id = _cohort_calendar_id(client, admin_headers, cohort["id"])
    _schedule_one_day(client, admin_headers, template_id, [iv_topic.id, next_topic["id"]])

    calendar = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(calendar, iv_topic.id) == LessonDayState.CURRENT.value
    assert _state_for(calendar, next_topic["id"]) == LessonDayState.LOCKED.value

    # Wrong exercise checkpoint answer: still locked.
    topic_resp = client.get(f"/api/v1/topics/{iv_topic.id}", headers=student_headers).json()
    wrong = client.post("/api/v1/exercise-attempts", json={
        "topic_id": iv_topic.id, "exercise_index": 0, "topic_updated_at": topic_resp["updated_at"], "answer": 1,
    }, headers=student_headers)
    assert wrong.status_code == 201, wrong.text
    mid = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(mid, next_topic["id"]) == LessonDayState.LOCKED.value

    # Correct exercise checkpoint, but the Mariam checkpoint not yet practiced: still locked.
    right = client.post("/api/v1/exercise-attempts", json={
        "topic_id": iv_topic.id, "exercise_index": 0, "topic_updated_at": topic_resp["updated_at"], "answer": 0,
    }, headers=student_headers)
    assert right.status_code == 201, right.text
    mid2 = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(mid2, iv_topic.id) == LessonDayState.CURRENT.value
    assert _state_for(mid2, next_topic["id"]) == LessonDayState.LOCKED.value

    # An early manual hangup on the Mariam checkpoint must NOT unlock it either.
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": iv_topic.id, "checkpoint_id": practice_checkpoint.id},
        headers=student_headers,
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": [
            {"speaker": "student", "text": "hi", "timestamp": "2026-01-01T00:00:00"},
            {"speaker": "mariam", "text": "hello", "timestamp": "2026-01-01T00:00:01"},
        ]},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    still = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(still, next_topic["id"]) == LessonDayState.LOCKED.value

    # The agent's own max-duration timer firing, with a genuine two-sided transcript: now it unlocks.
    started2 = client.post(
        "/api/v1/sessions/start", json={"topic_id": iv_topic.id, "checkpoint_id": practice_checkpoint.id},
        headers=student_headers,
    ).json()
    client.post(
        f"/api/v1/sessions/{started2['session_id']}/mark-ended",
        json={"end_reason": "max_duration_reached", "transcript_history": [
            {"speaker": "student", "text": "I'd like a coffee please.", "timestamp": "2026-01-01T00:00:00"},
            {"speaker": "mariam", "text": "What size would you like?", "timestamp": "2026-01-01T00:00:02"},
        ]},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    after = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(after, iv_topic.id) == LessonDayState.DONE.value
    assert _state_for(after, next_topic["id"]) == LessonDayState.CURRENT.value


async def test_completion_policy_standalone_practice_topic_requires_the_strict_max_duration_rule(client):
    """A Standard lesson authored with ZERO exercises (pure discussion
    practice) can only be completed via a genuine standalone Mariam session,
    gated by the same strict rule as a checkpoint - never the weak legacy
    `_had_a_real_conversation` heuristic."""
    admin, admin_headers = await _login(client, "cu-mariam-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "cu-mariam-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "cu-mariam-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_completion_program(client, admin_headers, "cu-mariam", "Completion Mariam")
    stage = _stage_id(program)
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort["id"], student.id, stage)

    practice_topic = TopicContent(
        title="Free talk", level=Level.BEGINNER, lesson_flow=LessonFlow.STANDARD,
        uploaded_video_url="https://cdn.example.test/free-talk.mp4", created_by_id=teacher.id,
    )
    await practice_topic.insert()
    await _publish_direct(program, stage, practice_topic, teacher.id)
    next_topic = _create_exercise_topic(client, teacher_headers, program, stage, "After free talk")
    template_id = _cohort_calendar_id(client, admin_headers, cohort["id"])
    _schedule_one_day(client, admin_headers, template_id, [practice_topic.id, next_topic["id"]])

    calendar = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(calendar, practice_topic.id) == LessonDayState.CURRENT.value
    assert _state_for(calendar, next_topic["id"]) == LessonDayState.LOCKED.value

    # 60+ seconds and 2 real turns each - enough for the legacy heuristic, but an early manual hangup
    # (not the agent's own max-duration timer) must still NOT unlock a COMPLETION-policy lesson.
    started = client.post("/api/v1/sessions/start", json={"topic_id": practice_topic.id}, headers=student_headers).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": [
            {"speaker": "student", "text": "Hi there, how are you today?", "timestamp": "2026-01-01T00:00:00"},
            {"speaker": "mariam", "text": "I'm doing great, thanks for asking!", "timestamp": "2026-01-01T00:00:30"},
            {"speaker": "student", "text": "Let's talk about the weather.", "timestamp": "2026-01-01T00:01:00"},
            {"speaker": "mariam", "text": "Sure, it's lovely out today.", "timestamp": "2026-01-01T00:01:30"},
        ]},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    still_locked = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(still_locked, next_topic["id"]) == LessonDayState.LOCKED.value

    started2 = client.post("/api/v1/sessions/start", json={"topic_id": practice_topic.id}, headers=student_headers).json()
    client.post(
        f"/api/v1/sessions/{started2['session_id']}/mark-ended",
        json={"end_reason": "max_duration_reached", "transcript_history": [
            {"speaker": "student", "text": "Hi there, how are you today?", "timestamp": "2026-01-01T00:00:00"},
            {"speaker": "mariam", "text": "I'm doing great, thanks for asking!", "timestamp": "2026-01-01T00:00:02"},
        ]},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    after = client.get("/api/v1/me/challenge-calendar", headers=student_headers).json()
    assert _state_for(after, practice_topic.id) == LessonDayState.DONE.value
    assert _state_for(after, next_topic["id"]) == LessonDayState.CURRENT.value
