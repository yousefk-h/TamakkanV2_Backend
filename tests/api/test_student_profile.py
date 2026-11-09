"""GET /users/{student_id} - a single Student profile, authorized the same
way "My Students" already is (CohortService.may_reach_student). Added to fix
the "Student not found" bug: a Teacher could see a Student in My Students /
a Cohort roster but opening their profile 404'd, because the old lookup only
searched the created_by_id-scoped legacy roster.
"""

from tests.api.test_cohorts import _create_cohort, _create_program, _login, _stage_id
from app.models.user import Role


async def test_teacher_can_open_a_student_reached_only_via_cohort_enrollment(client):
    admin, admin_headers = await _login(client, "studentprofile-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "studentprofile-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "studentprofile-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Profile Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)

    student, _ = await _login(client, "studentprofile-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [student.id], "program_level_id": beginner}, headers=admin_headers)

    resp = client.get(f"/api/v1/users/{student.id}", headers=teacher_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == student.id


async def test_teacher_cannot_open_a_student_they_do_not_teach(client):
    admin, admin_headers = await _login(client, "studentprofile-admin-2@example.com", Role.ADMINISTRATOR)
    other_teacher, other_headers = await _login(client, "studentprofile-other-teacher@example.com", Role.TEACHER)
    teacher, _ = await _login(client, "studentprofile-owner-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "studentprofile-program-2", "Program 2")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Owner Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)

    student, _ = await _login(client, "studentprofile-unreached-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [student.id], "program_level_id": beginner}, headers=admin_headers)

    resp = client.get(f"/api/v1/users/{student.id}", headers=other_headers)
    assert resp.status_code == 404


async def test_student_profile_route_does_not_shadow_literal_routes(client):
    admin, admin_headers = await _login(client, "studentprofile-admin-3@example.com", Role.ADMINISTRATOR)
    assert client.get("/api/v1/users/counts", headers=admin_headers).status_code == 200
    assert client.get("/api/v1/users/alerts", headers=admin_headers).status_code == 200


async def test_admin_can_open_any_student_profile(client):
    admin, admin_headers = await _login(client, "studentprofile-admin-4@example.com", Role.ADMINISTRATOR)
    student, _ = await _login(client, "studentprofile-student-4@example.com", Role.STUDENT, created_by_id=admin.id)

    resp = client.get(f"/api/v1/users/{student.id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text


async def test_unknown_student_id_returns_not_found(client):
    admin, admin_headers = await _login(client, "studentprofile-admin-5@example.com", Role.ADMINISTRATOR)
    resp = client.get("/api/v1/users/000000000000000000000000", headers=admin_headers)
    assert resp.status_code == 404
