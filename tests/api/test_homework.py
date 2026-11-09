"""Homework assignment, roster isolation, and student visibility."""

from datetime import timedelta

from app.core.security import hash_password
from app.models.topic_content import Level
from app.models.user import Role, User
from app.utils.time import utcnow


async def _login(client, email: str, role: Role, **extra):
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"}).json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


async def test_teacher_assigns_homework_only_to_roster_and_student_sees_it(client):
    teacher, teacher_headers = await _login(client, "homework-teacher@example.com", Role.TEACHER)
    other, _ = await _login(client, "homework-other@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "homework-student@example.com", Role.STUDENT, created_by_id=teacher.id,
                                             assigned_level=Level.BEGINNER)
    outsider, _ = await _login(client, "homework-outsider@example.com", Role.STUDENT, created_by_id=other.id)
    topic = client.post("/api/v1/topics", json={"title": "Homework lesson", "level": "beginner"}, headers=teacher_headers).json()
    body = {"title": "Finish the lesson", "instructions": "Listen and answer", "kind": "lesson",
            "due_at": (utcnow() + timedelta(days=2)).isoformat(), "participant_ids": [student.id], "topic_id": topic["id"]}
    created = client.post("/api/v1/homework", json=body, headers=teacher_headers)
    assert created.status_code == 201, created.text
    assert created.json()["topic_title"] == "Homework lesson"
    mine = client.get("/api/v1/homework/me", headers=student_headers)
    assert mine.status_code == 200 and [item["id"] for item in mine.json()] == [created.json()["id"]]
    assert client.post("/api/v1/homework", json={**body, "participant_ids": [outsider.id]}, headers=teacher_headers).status_code == 403
    assert client.get("/api/v1/homework", headers=student_headers).status_code == 403
    assert client.delete(f"/api/v1/homework/{created.json()['id']}", headers=teacher_headers).status_code == 204
    assert client.get("/api/v1/homework/me", headers=student_headers).json() == []


def _create_program(client, admin_headers):
    response = client.post("/api/v1/programs", json={
        "code": "hw-general", "name": "General English", "language": "English",
        "base_levels": ["beginner"], "level_profile_ids": [],
    }, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_homework_can_target_a_cohort_and_a_stage_scoped_teacher_can_manage_it(client):
    admin, admin_headers = await _login(client, "hw-admin@example.com", Role.ADMINISTRATOR)
    lead, lead_headers = await _login(client, "hw-lead@example.com", Role.TEACHER)
    stage_teacher, stage_headers = await _login(client, "hw-stage-teacher@example.com", Role.TEACHER)
    outsider_teacher, outsider_headers = await _login(client, "hw-outsider-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers)
    stage_id = program["levels"][0]["id"]
    cohort = client.post("/api/v1/cohorts", json={
        "name": "Batch", "program_id": program["id"], "program_level_ids": [stage_id],
        "lead_teacher_id": lead.id,
        "teacher_assignments": [{"teacher_id": stage_teacher.id, "program_level_ids": [stage_id]}],
        "start_date": utcnow().isoformat(),
    }, headers=admin_headers).json()

    student_a, student_a_headers = await _login(client, "hw-student-a@example.com", Role.STUDENT, created_by_id=admin.id)
    student_b, _ = await _login(client, "hw-student-b@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [student_a.id, student_b.id], "program_level_id": stage_id,
    }, headers=admin_headers)

    body = {
        "title": "Cohort homework", "instructions": "Do it", "kind": "writing",
        "due_at": (utcnow() + timedelta(days=2)).isoformat(), "cohort_id": cohort["id"],
    }
    # An unassigned teacher cannot target a Cohort they have no access to.
    assert client.post("/api/v1/homework", json=body, headers=outsider_headers).status_code == 403

    # The stage-scoped teacher CAN target the Cohort (reaches the whole roster via that stage).
    created = client.post("/api/v1/homework", json=body, headers=stage_headers)
    assert created.status_code == 201, created.text
    assert set(created.json()["participant_ids"]) == {student_a.id, student_b.id}
    assert created.json()["cohort_id"] == cohort["id"]
    assert created.json()["program_id"] == program["id"]

    # Both students see it; it's created by the stage teacher, not the Lead.
    assert len(client.get("/api/v1/homework/me", headers=student_a_headers).json()) == 1

    # The Lead Teacher (who didn't create it) can still manage/cancel it via Cohort access.
    assert client.delete(f"/api/v1/homework/{created.json()['id']}", headers=lead_headers).status_code == 204
    assert client.get("/api/v1/homework/me", headers=student_a_headers).json() == []

    # An outsider teacher (no Cohort access) still cannot manage Cohort-targeted homework.
    recreated = client.post("/api/v1/homework", json=body, headers=lead_headers)
    assert client.delete(f"/api/v1/homework/{recreated.json()['id']}", headers=outsider_headers).status_code == 404
