"""P0: LiveSession and ExerciseAttempt must resolve against an explicit
Enrollment when a student has more than one active one, and never leak
entitlement/results across enrollments - even when the same Topic is reused
across Programs, or two Cohorts share one Program.
"""

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.live_session import LiveSession
from app.models.exercise_attempt import ExerciseAttempt
from app.models.topic_content import ExerciseKind, TopicContent
from app.models.user import Role, User
from app.services.exercise_attempt_service import exercise_hash
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


def _create_program(client, admin_headers, code: str, name: str):
    response = client.post("/api/v1/programs", json={
        "code": code, "name": name, "language": "English",
        "base_levels": ["beginner"], "level_profile_ids": [],
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


def _cohort_calendar_id(client, admin_headers, cohort_id: str) -> str:
    templates = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    return next(t for t in templates if t["cohort_id"] == cohort_id)["id"]


def _schedule_topic_day1(client, admin_headers, template_id: str, topic_id: str):
    response = client.patch(f"/api/v1/calendar-templates/{template_id}", json={
        "days": [{"day": 1, "topic_ids": [topic_id], "custom_minutes": {}}],
    }, headers=admin_headers)
    assert response.status_code == 200, response.text


def _create_published_topic(client, teacher_headers, admin_headers, program: dict, stage_id: str, title: str):
    topic = client.post("/api/v1/topics", json={
        "title": title, "level": "beginner",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Pick", "options": ["A", "B"], "correct_option": 0}],
    }, headers=teacher_headers).json()
    placed = client.post(f"/api/v1/programs/{program['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": stage_id, "status": "published",
    }, headers=teacher_headers)
    assert placed.status_code == 201, placed.text
    return topic


def _enroll(client, admin_headers, cohort_id: str, student_id: str, stage_id: str):
    response = client.post(f"/api/v1/cohorts/{cohort_id}/students", json={
        "student_ids": [student_id], "program_level_id": stage_id,
    }, headers=admin_headers)
    assert response.status_code == 201, response.text


async def test_two_enrollments_require_explicit_choice_and_stay_isolated(client):
    admin, admin_headers = await _login(client, "ei-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "ei-student@example.com", Role.STUDENT, created_by_id=admin.id)

    general = _create_program(client, admin_headers, "ei-general", "General English")
    business = _create_program(client, admin_headers, "ei-business", "Business English")
    general_stage, business_stage = _stage_id(general), _stage_id(business)

    general_cohort = _create_cohort(client, admin_headers, name="General Batch", program=general, stage_id=general_stage, lead_teacher_id=teacher.id)
    business_cohort = _create_cohort(client, admin_headers, name="Business Batch", program=business, stage_id=business_stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, general_cohort["id"], student.id, general_stage)
    _enroll(client, admin_headers, business_cohort["id"], student.id, business_stage)

    # Reuse: the SAME Topic is published (and scheduled on day 1) in BOTH Programs.
    topic = _create_published_topic(client, teacher_headers, admin_headers, general, general_stage, "Shared Lesson")
    placed_business = client.post(f"/api/v1/programs/{business['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": business_stage, "status": "published",
    }, headers=teacher_headers)
    assert placed_business.status_code == 201, placed_business.text

    general_template_id = _cohort_calendar_id(client, admin_headers, general_cohort["id"])
    business_template_id = _cohort_calendar_id(client, admin_headers, business_cohort["id"])
    _schedule_topic_day1(client, admin_headers, general_template_id, topic["id"])
    _schedule_topic_day1(client, admin_headers, business_template_id, topic["id"])

    enrollments = {e["program_id"]: e["id"] for e in client.get("/api/v1/enrollments/me", headers=student_headers).json()}
    general_enrollment_id = enrollments[general["id"]]
    business_enrollment_id = enrollments[business["id"]]

    # No enrollment_id given with 2 active enrollments -> ambiguous, refused.
    ambiguous = client.post("/api/v1/sessions/start", json={"topic_id": topic["id"]}, headers=student_headers)
    assert ambiguous.status_code == 422, ambiguous.text
    assert await LiveSession.find_all().to_list() == []

    started_general = client.post("/api/v1/sessions/start", json={
        "topic_id": topic["id"], "enrollment_id": general_enrollment_id,
    }, headers=student_headers)
    assert started_general.status_code == 200, started_general.text
    general_session = await LiveSession.get(started_general.json()["session_id"])
    assert general_session.program_id == general["id"]
    assert general_session.enrollment_id == general_enrollment_id

    started_business = client.post("/api/v1/sessions/start", json={
        "topic_id": topic["id"], "enrollment_id": business_enrollment_id,
    }, headers=student_headers)
    assert started_business.status_code == 200, started_business.text
    business_session = await LiveSession.get(started_business.json()["session_id"])
    assert business_session.program_id == business["id"]
    assert business_session.enrollment_id == business_enrollment_id
    assert business_session.id != general_session.id

    # Exercise attempts: same Topic, both enrollments, must not supersede or leak into each other.
    # Re-fetch (as the authoring teacher - entitlement doesn't apply to them):
    # publishing into two Programs pinned a revision snapshot each time, bumping `updated_at`.
    current_topic = client.get(f"/api/v1/topics/{topic['id']}", headers=teacher_headers).json()
    attempt_payload = {"topic_id": topic["id"], "exercise_index": 0, "topic_updated_at": current_topic["updated_at"], "answer": 0}
    submit_general = client.post("/api/v1/exercise-attempts", json={**attempt_payload, "enrollment_id": general_enrollment_id}, headers=student_headers)
    submit_business = client.post("/api/v1/exercise-attempts", json={**attempt_payload, "enrollment_id": business_enrollment_id}, headers=student_headers)
    assert submit_general.status_code == 201, submit_general.text
    assert submit_business.status_code == 201, submit_business.text

    mine_general = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic['id']}&enrollment_id={general_enrollment_id}", headers=student_headers).json()
    mine_business = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic['id']}&enrollment_id={business_enrollment_id}", headers=student_headers).json()
    assert len(mine_general) == 1 and mine_general[0]["id"] == submit_general.json()["id"]
    assert len(mine_business) == 1 and mine_business[0]["id"] == submit_business.json()["id"]
    both_attempts = await ExerciseAttempt.find_all().to_list()
    assert len(both_attempts) == 2 and all(not a.superseded for a in both_attempts)


async def test_an_enrollment_id_belonging_to_another_student_is_rejected(client):
    admin, admin_headers = await _login(client, "ei-cross-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-cross-teacher@example.com", Role.TEACHER)
    owner, owner_headers = await _login(client, "ei-cross-owner@example.com", Role.STUDENT, created_by_id=admin.id)
    intruder, intruder_headers = await _login(client, "ei-cross-intruder@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "ei-cross-general", "General English")
    stage = _stage_id(program)
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort["id"], owner.id, stage)
    topic = _create_published_topic(client, teacher_headers, admin_headers, program, stage, "Owner's Lesson")
    template_id = _cohort_calendar_id(client, admin_headers, cohort["id"])
    _schedule_topic_day1(client, admin_headers, template_id, topic["id"])

    owner_enrollment_id = client.get("/api/v1/enrollments/me", headers=owner_headers).json()[0]["id"]

    stolen = client.post("/api/v1/sessions/start", json={
        "topic_id": topic["id"], "enrollment_id": owner_enrollment_id,
    }, headers=intruder_headers)
    assert stolen.status_code == 403, stolen.text
    assert await LiveSession.find_all().to_list() == []

    stolen_attempt = client.post("/api/v1/exercise-attempts", json={
        "topic_id": topic["id"], "exercise_index": 0, "topic_updated_at": topic["updated_at"], "answer": 0,
        "enrollment_id": owner_enrollment_id,
    }, headers=intruder_headers)
    assert stolen_attempt.status_code == 403, stolen_attempt.text


async def test_handoff_retains_the_source_sessions_enrollment(client):
    admin, admin_headers = await _login(client, "ei-handoff-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-handoff-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "ei-handoff-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "ei-handoff-general", "General English")
    stage = _stage_id(program)
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort["id"], student.id, stage)
    topic = _create_published_topic(client, teacher_headers, admin_headers, program, stage, "Handoff Lesson")
    template_id = _cohort_calendar_id(client, admin_headers, cohort["id"])
    _schedule_topic_day1(client, admin_headers, template_id, topic["id"])

    enrollment_id = client.get("/api/v1/enrollments/me", headers=student_headers).json()[0]["id"]
    started = client.post("/api/v1/sessions/start", json={
        "topic_id": topic["id"], "enrollment_id": enrollment_id,
    }, headers=student_headers)
    assert started.status_code == 200, started.text
    source_id = started.json()["session_id"]

    handoff = client.post(f"/api/v1/sessions/{source_id}/handoff", headers=student_headers)
    assert handoff.status_code == 200, handoff.text
    target = await LiveSession.get(handoff.json()["session_id"])
    assert target.enrollment_id == enrollment_id
    assert target.program_id == program["id"]


async def test_get_topic_by_id_is_enrollment_aware(client):
    admin, admin_headers = await _login(client, "ei-topic-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-topic-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "ei-topic-student@example.com", Role.STUDENT, created_by_id=admin.id)

    general = _create_program(client, admin_headers, "ei-topic-general", "General English")
    business = _create_program(client, admin_headers, "ei-topic-business", "Business English")
    general_stage, business_stage = _stage_id(general), _stage_id(business)
    general_cohort = _create_cohort(client, admin_headers, name="General Batch", program=general, stage_id=general_stage, lead_teacher_id=teacher.id)
    business_cohort = _create_cohort(client, admin_headers, name="Business Batch", program=business, stage_id=business_stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, general_cohort["id"], student.id, general_stage)
    _enroll(client, admin_headers, business_cohort["id"], student.id, business_stage)

    topic = _create_published_topic(client, teacher_headers, admin_headers, general, general_stage, "Readable Lesson")
    general_template_id = _cohort_calendar_id(client, admin_headers, general_cohort["id"])
    _schedule_topic_day1(client, admin_headers, general_template_id, topic["id"])

    enrollments = {e["program_id"]: e["id"] for e in client.get("/api/v1/enrollments/me", headers=student_headers).json()}
    general_enrollment_id = enrollments[general["id"]]
    business_enrollment_id = enrollments[business["id"]]

    # No enrollment_id with 2 active enrollments -> ambiguous, refused.
    ambiguous = client.get(f"/api/v1/topics/{topic['id']}", headers=student_headers)
    assert ambiguous.status_code == 422, ambiguous.text

    # Resolved via the Program/stage where it's actually scheduled -> readable.
    readable = client.get(f"/api/v1/topics/{topic['id']}?enrollment_id={general_enrollment_id}", headers=student_headers)
    assert readable.status_code == 200, readable.text

    # The SAME topic id is not published/scheduled under the Business
    # enrollment at all -> correctly locked there, not leaked through.
    not_entitled = client.get(f"/api/v1/topics/{topic['id']}?enrollment_id={business_enrollment_id}", headers=student_headers)
    assert not_entitled.status_code == 403, not_entitled.text

    # An enrollment_id belonging to another student is rejected outright.
    other_student, other_headers = await _login(client, "ei-topic-other@example.com", Role.STUDENT, created_by_id=admin.id)
    stolen = client.get(f"/api/v1/topics/{topic['id']}?enrollment_id={general_enrollment_id}", headers=other_headers)
    assert stolen.status_code == 403, stolen.text


async def test_synthetic_legacy_attempt_stays_visible_when_unambiguous_but_hidden_once_ambiguous(client):
    """Migration-compatibility check: a hand-inserted pre-Enrollment
    `ExerciseAttempt` (enrollment_id=None) must still surface once the
    student gets a real Enrollment, AS LONG AS that Enrollment is the only
    plausible owner - and must stop being attributed to either once a second
    Enrollment on the same Program/stage makes it genuinely ambiguous."""
    admin, admin_headers = await _login(client, "ei-legacy-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-legacy-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "ei-legacy-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "ei-legacy-general", "General English")
    stage = _stage_id(program)
    cohort_a = _create_cohort(client, admin_headers, name="Batch A", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort_a["id"], student.id, stage)
    topic = _create_published_topic(client, teacher_headers, admin_headers, program, stage, "Legacy Lesson")
    template_a = _cohort_calendar_id(client, admin_headers, cohort_a["id"])
    _schedule_topic_day1(client, admin_headers, template_a, topic["id"])

    # Hand-insert a pre-Enrollment attempt, exactly as one written before Cohorts existed would look -
    # real exercise_hash (matching the actual, current lesson_exercises[0]) so the dedup logic in
    # `mine()` treats it as current, not stale.
    stored_topic = await TopicContent.get(topic["id"])
    legacy_attempt = ExerciseAttempt(
        student_id=student.id, student_name=student.full_name, program_id=program["id"], program_level_id=stage,
        enrollment_id=None, topic_id=topic["id"], topic_title=topic["title"], exercise_index=0,
        exercise_hash=exercise_hash(stored_topic.lesson_exercises[0]), exercise_kind=ExerciseKind.MULTIPLE_CHOICE,
        exercise_prompt="Pick", answer=0, answer_display="A", correct=True, correct_option=0, score=100, reviewed=True,
    )
    await legacy_attempt.insert()

    enrollment_a_id = client.get("/api/v1/enrollments/me", headers=student_headers).json()[0]["id"]

    # Unambiguous (only one enrollment on this Program/stage) -> the legacy row is visible.
    mine_unambiguous = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic['id']}&enrollment_id={enrollment_a_id}", headers=student_headers).json()
    assert len(mine_unambiguous) == 1 and mine_unambiguous[0]["id"] == legacy_attempt.id

    # A second Cohort of the SAME Program/stage makes the old row ambiguous -
    # also scheduling the Topic there so both enrollments are equally
    # entitled, isolating the assertion to attribution, not entitlement.
    cohort_b = _create_cohort(client, admin_headers, name="Batch B", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort_b["id"], student.id, stage)
    template_b = _cohort_calendar_id(client, admin_headers, cohort_b["id"])
    _schedule_topic_day1(client, admin_headers, template_b, topic["id"])
    enrollments = client.get("/api/v1/enrollments/me", headers=student_headers).json()
    enrollment_b_id = next(e["id"] for e in enrollments if e["id"] != enrollment_a_id)

    mine_a_now_ambiguous = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic['id']}&enrollment_id={enrollment_a_id}", headers=student_headers).json()
    mine_b = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic['id']}&enrollment_id={enrollment_b_id}", headers=student_headers).json()
    assert mine_a_now_ambiguous == []
    assert mine_b == []
    # The legacy row itself is untouched either way - never deleted, never reassigned.
    still_there = await ExerciseAttempt.get(legacy_attempt.id)
    assert still_there is not None and still_there.enrollment_id is None


async def test_two_cohorts_of_one_program_give_independent_session_entitlement(client):
    admin, admin_headers = await _login(client, "ei-stage-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "ei-stage-teacher@example.com", Role.TEACHER)
    student_a, headers_a = await _login(client, "ei-stage-student-a@example.com", Role.STUDENT, created_by_id=admin.id)
    student_b, headers_b = await _login(client, "ei-stage-student-b@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "ei-stage-general", "General English")
    stage = _stage_id(program)
    cohort_a = _create_cohort(client, admin_headers, name="Batch A", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    cohort_b = _create_cohort(client, admin_headers, name="Batch B", program=program, stage_id=stage, lead_teacher_id=teacher.id)
    _enroll(client, admin_headers, cohort_a["id"], student_a.id, stage)
    _enroll(client, admin_headers, cohort_b["id"], student_b.id, stage)

    topic = _create_published_topic(client, teacher_headers, admin_headers, program, stage, "Stage Lesson")
    template_a = _cohort_calendar_id(client, admin_headers, cohort_a["id"])
    template_b = _cohort_calendar_id(client, admin_headers, cohort_b["id"])
    # Only Cohort A schedules the topic - Cohort B's own calendar stays empty.
    _schedule_topic_day1(client, admin_headers, template_a, topic["id"])

    enrollment_a = client.get("/api/v1/enrollments/me", headers=headers_a).json()[0]["id"]
    enrollment_b = client.get("/api/v1/enrollments/me", headers=headers_b).json()[0]["id"]

    allowed = client.post("/api/v1/sessions/start", json={"topic_id": topic["id"], "enrollment_id": enrollment_a}, headers=headers_a)
    assert allowed.status_code == 200, allowed.text

    blocked = client.post("/api/v1/sessions/start", json={"topic_id": topic["id"], "enrollment_id": enrollment_b}, headers=headers_b)
    assert blocked.status_code == 403, blocked.text
