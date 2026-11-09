"""GET /users/my-students - the real "My Students" population (Teacher
Workspace improvement): every Student with an active Enrollment in a Cohort
the caller actually teaches, stage-scoped same as the Cohort roster,
deduplicated across Cohorts/Programs. Deliberately distinct from
GET /users (created_by_id) - see app/services/cohort_service.py's own
docstring on list_my_students for why.
"""

from tests.api.test_cohorts import _create_cohort, _create_program, _login, _stage_id
from app.models.user import Role


async def test_a_teacher_sees_only_students_enrolled_in_cohorts_they_teach(client):
    admin, admin_headers = await _login(client, "mystudents-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "mystudents-teacher@example.com", Role.TEACHER)
    other_teacher, other_headers = await _login(client, "mystudents-other-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "mystudents-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    my_cohort = _create_cohort(client, admin_headers, name="My Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    other_cohort = _create_cohort(client, admin_headers, name="Other Cohort", program=program, stage_ids=[beginner], lead_teacher_id=other_teacher.id)

    my_student, _ = await _login(client, "mystudents-my-student@example.com", Role.STUDENT, created_by_id=admin.id)
    other_student, _ = await _login(client, "mystudents-other-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{my_cohort['id']}/students", json={"student_ids": [my_student.id], "program_level_id": beginner}, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{other_cohort['id']}/students", json={"student_ids": [other_student.id], "program_level_id": beginner}, headers=admin_headers)

    resp = client.get("/api/v1/users/my-students", headers=teacher_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [my_student.id]
    assert body["total"] == 1
    assert body["items"][0]["enrollments"][0]["cohort_name"] == "My Cohort"
    assert body["items"][0]["enrollments"][0]["program_name"] == "General English"

    # The other teacher's roster never leaks into this one's response at all.
    resp_other = client.get("/api/v1/users/my-students", headers=other_headers)
    assert [item["id"] for item in resp_other.json()["items"]] == [other_student.id]


async def test_a_student_enrolled_in_two_of_the_teachers_cohorts_appears_once(client):
    admin, admin_headers = await _login(client, "mystudents-dedup-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "mystudents-dedup-teacher@example.com", Role.TEACHER)

    program_a = _create_program(client, admin_headers, "mystudents-dedup-program-a", "Program A")
    program_b = _create_program(client, admin_headers, "mystudents-dedup-program-b", "Program B")
    stage_a = _stage_id(program_a, "beginner")
    stage_b = _stage_id(program_b, "beginner")
    cohort_a = _create_cohort(client, admin_headers, name="Cohort A", program=program_a, stage_ids=[stage_a], lead_teacher_id=teacher.id)
    cohort_b = _create_cohort(client, admin_headers, name="Cohort B", program=program_b, stage_ids=[stage_b], lead_teacher_id=teacher.id)

    student, _ = await _login(client, "mystudents-dedup-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort_a['id']}/students", json={"student_ids": [student.id], "program_level_id": stage_a}, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{cohort_b['id']}/students", json={"student_ids": [student.id], "program_level_id": stage_b}, headers=admin_headers)

    resp = client.get("/api/v1/users/my-students", headers=teacher_headers)
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert {e["cohort_name"] for e in body["items"][0]["enrollments"]} == {"Cohort A", "Cohort B"}


async def test_a_stage_scoped_teacher_sees_only_their_stages_students(client):
    admin, admin_headers = await _login(client, "mystudents-stage-admin@example.com", Role.ADMINISTRATOR)
    lead, lead_headers = await _login(client, "mystudents-stage-lead@example.com", Role.TEACHER)
    beginner_teacher, beginner_headers = await _login(client, "mystudents-stage-beginner-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "mystudents-stage-program", "Stage Program")
    beginner = _stage_id(program, "beginner")
    intermediate = _stage_id(program, "intermediate")
    cohort = _create_cohort(
        client, admin_headers, name="Mixed Batch", program=program, stage_ids=[beginner, intermediate],
        lead_teacher_id=lead.id,
        teacher_assignments=[{"teacher_id": beginner_teacher.id, "program_level_ids": [beginner]}],
    )
    beginner_student, _ = await _login(client, "mystudents-stage-beginner-student@example.com", Role.STUDENT, created_by_id=admin.id)
    intermediate_student, _ = await _login(client, "mystudents-stage-intermediate-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [beginner_student.id], "program_level_id": beginner}, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [intermediate_student.id], "program_level_id": intermediate}, headers=admin_headers)

    lead_resp = client.get("/api/v1/users/my-students", headers=lead_headers).json()
    assert {item["id"] for item in lead_resp["items"]} == {beginner_student.id, intermediate_student.id}

    scoped_resp = client.get("/api/v1/users/my-students", headers=beginner_headers).json()
    assert {item["id"] for item in scoped_resp["items"]} == {beginner_student.id}


async def test_my_students_search_and_cohort_filter(client):
    admin, admin_headers = await _login(client, "mystudents-filter-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "mystudents-filter-teacher@example.com", Role.TEACHER)
    program = _create_program(client, admin_headers, "mystudents-filter-program", "Filter Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Filter Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    ahmad, _ = await _login(client, "mystudents-filter-ahmad@example.com", Role.STUDENT, created_by_id=admin.id)
    sara, _ = await _login(client, "mystudents-filter-sara@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [ahmad.id, sara.id], "program_level_id": beginner}, headers=admin_headers)

    resp = client.get("/api/v1/users/my-students", params={"search": "ahmad"}, headers=teacher_headers)
    assert [item["id"] for item in resp.json()["items"]] == [ahmad.id]

    filtered = client.get("/api/v1/users/my-students", params={"cohort_id": cohort["id"]}, headers=teacher_headers)
    assert filtered.json()["total"] == 2


async def test_a_student_cannot_call_my_students(client):
    _admin, admin_headers = await _login(client, "mystudents-perm-admin@example.com", Role.ADMINISTRATOR)
    _student, student_headers = await _login(client, "mystudents-perm-student@example.com", Role.STUDENT)
    assert client.get("/api/v1/users/my-students", headers=student_headers).status_code == 403
