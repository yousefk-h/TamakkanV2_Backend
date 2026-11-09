"""GET /users/{student_id}/learning-journey and /exercise-history - the
complete, deterministic student activity view (Teacher Workspace Increment
2). Builds on the exact Cohort/Enrollment scheduling flow already proven in
test_cohorts.py's test_teacher_reuses_topic_and_schedules_only_their_assigned_batch_stage.
"""

from app.models.user import Role
from tests.api.test_cohorts import _create_cohort, _create_program, _login, _stage_id


async def _schedule_topic_on_cohort(client, teacher_headers, cohort, stage_id, topic_id):
    templates = client.get("/api/v1/calendar-templates", headers=teacher_headers).json()
    calendar_id = next(item["id"] for item in templates if item["cohort_id"] == cohort["id"] and item["program_level_id"] == stage_id)
    resp = client.patch(f"/api/v1/calendar-templates/{calendar_id}", json={
        "days": [{"day": 1, "topic_ids": [topic_id], "custom_minutes": {}}],
    }, headers=teacher_headers)
    assert resp.status_code == 200, resp.text


async def test_learning_journey_aggregates_enrollment_schedule_and_graded_exercises(client):
    admin, admin_headers = await _login(client, "journey-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "journey-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "journey-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "journey-program", "Journey Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Journey Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    enrolled = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [student.id], "program_level_id": beginner}, headers=admin_headers)
    assert enrolled.status_code == 201, enrolled.text
    enrollment_id = enrolled.json()[0]["id"]

    topic = client.post("/api/v1/topics", json={
        "title": "Greetings Lesson", "level": "beginner",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Choose hello", "options": ["Hello", "Goodbye"], "correct_option": 0}],
    }, headers=teacher_headers)
    assert topic.status_code == 200, topic.text
    topic_id = topic.json()["id"]
    assigned = client.post(f"/api/v1/cohorts/{cohort['id']}/topics", json={"topic_id": topic_id, "program_level_id": beginner, "status": "published"}, headers=teacher_headers)
    assert assigned.status_code == 201, assigned.text
    await _schedule_topic_on_cohort(client, teacher_headers, cohort, beginner, topic_id)

    student_topic = client.get(f"/api/v1/topics/{topic_id}", params={"enrollment_id": enrollment_id}, headers=student_headers)
    assert student_topic.status_code == 200, student_topic.text
    revision = student_topic.json()["updated_at"]

    # Wrong answer, then a resubmission with the right one - both must survive in full history.
    first = client.post("/api/v1/exercise-attempts", json={
        "topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1, "enrollment_id": enrollment_id,
    }, headers=student_headers)
    assert first.status_code == 201, first.text
    assert first.json()["correct"] is False
    second = client.post("/api/v1/exercise-attempts", json={
        "topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0, "enrollment_id": enrollment_id,
    }, headers=student_headers)
    assert second.status_code == 201, second.text
    assert second.json()["correct"] is True

    journey = client.get(f"/api/v1/users/{student.id}/learning-journey", headers=teacher_headers)
    assert journey.status_code == 200, journey.text
    body = journey.json()
    assert body["student_id"] == student.id
    assert len(body["enrollments"]) == 1
    enrollment_journey = body["enrollments"][0]
    assert enrollment_journey["enrollment"]["id"] == enrollment_id
    assert enrollment_journey["enrollment"]["program_name"] == "Journey Program"
    assert enrollment_journey["enrollment"]["cohort_name"] == "Journey Batch"
    assert len(enrollment_journey["lessons"]) == 1
    lesson = enrollment_journey["lessons"][0]
    assert lesson["topic_id"] == topic_id
    assert lesson["graded_exercise_count"] == 1
    assert lesson["correct_exercise_count"] == 1
    exercise = lesson["exercises"][0]
    assert exercise["attempt_count"] == 2
    assert exercise["latest_correct"] is True
    assert exercise["latest_score"] == 100

    # Full attempt history (both the wrong and the corrected answer) must still be retrievable.
    history = client.get(f"/api/v1/users/{student.id}/exercise-history",
                         params={"topic_id": topic_id, "enrollment_id": enrollment_id}, headers=teacher_headers)
    assert history.status_code == 200, history.text
    history_body = history.json()
    assert len(history_body) == 2
    assert [item["correct"] for item in sorted(history_body, key=lambda i: i["created_at"])] == [False, True]
    assert sum(1 for item in history_body if item["superseded"]) == 1


async def test_a_teacher_not_assigned_to_the_students_cohort_is_refused(client):
    admin, admin_headers = await _login(client, "journey-perm-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "journey-perm-teacher@example.com", Role.TEACHER)
    outsider, outsider_headers = await _login(client, "journey-perm-outsider@example.com", Role.TEACHER)
    student, _ = await _login(client, "journey-perm-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "journey-perm-program", "Perm Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Perm Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [student.id], "program_level_id": beginner}, headers=admin_headers)

    assert client.get(f"/api/v1/users/{student.id}/learning-journey", headers=teacher_headers).status_code == 200
    assert client.get(f"/api/v1/users/{student.id}/learning-journey", headers=outsider_headers).status_code == 403


async def test_a_student_cannot_call_the_learning_journey_endpoint(client):
    _admin, admin_headers = await _login(client, "journey-student-perm-admin@example.com", Role.ADMINISTRATOR)
    student, student_headers = await _login(client, "journey-student-perm-student@example.com", Role.STUDENT, created_by_id=_admin.id)
    assert client.get(f"/api/v1/users/{student.id}/learning-journey", headers=student_headers).status_code == 403
