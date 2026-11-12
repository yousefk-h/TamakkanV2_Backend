"""Cohorts and Enrollments: multi-enrollment students, independent per-Cohort
schedules for the same Program, stage-scoped teacher access, and that one
teacher's Cohort assignment never leaks into another's."""

from app.core.security import hash_password
from app.models.user import Role, User
from app.utils.time import utcnow


async def _login(client, email: str, role: Role, **extra):
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"}).json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


def _create_program(client, admin_headers, code: str, name: str):
    response = client.post("/api/v1/programs", json={
        "code": code, "name": name, "language": "English",
        "base_levels": ["beginner", "intermediate"], "level_profile_ids": [],
    }, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _create_cohort(client, admin_headers, *, name, program, stage_ids, lead_teacher_id, teacher_assignments=None):
    response = client.post("/api/v1/cohorts", json={
        "name": name,
        "program_id": program["id"],
        "program_level_ids": stage_ids,
        "lead_teacher_id": lead_teacher_id,
        "teacher_assignments": teacher_assignments or [],
        "start_date": utcnow().isoformat(),
    }, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _stage_id(program: dict, difficulty: str) -> str:
    return next(level["id"] for level in program["levels"] if level["difficulty_level"] == difficulty)


async def test_center_admin_creates_two_cohorts_with_own_teachers_students_and_schedules(client):
    admin, admin_headers = await _login(client, "cohort-admin@example.com", Role.ADMINISTRATOR)
    teacher_a, teacher_a_headers = await _login(client, "cohort-teacher-a@example.com", Role.TEACHER)
    teacher_b, _ = await _login(client, "cohort-teacher-b@example.com", Role.TEACHER)
    student1, _ = await _login(client, "cohort-student-1@example.com", Role.STUDENT, created_by_id=admin.id)
    student2, _ = await _login(client, "cohort-student-2@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "cohort-general-english", "General English")
    beginner = _stage_id(program, "beginner")

    october = _create_cohort(
        client, admin_headers, name="General English - October Batch", program=program,
        stage_ids=[beginner], lead_teacher_id=teacher_a.id,
    )
    november = _create_cohort(
        client, admin_headers, name="General English - November Batch", program=program,
        stage_ids=[beginner], lead_teacher_id=teacher_b.id,
    )
    assert october["id"] != november["id"]
    assert october["program_id"] == november["program_id"] == program["id"]

    enroll_oct = client.post(f"/api/v1/cohorts/{october['id']}/students", json={
        "student_ids": [student1.id], "program_level_id": beginner,
    }, headers=admin_headers)
    enroll_nov = client.post(f"/api/v1/cohorts/{november['id']}/students", json={
        "student_ids": [student2.id], "program_level_id": beginner,
    }, headers=admin_headers)
    assert enroll_oct.status_code == 201, enroll_oct.text
    assert enroll_nov.status_code == 201, enroll_nov.text

    oct_roster = client.get(f"/api/v1/cohorts/{october['id']}/roster", headers=admin_headers).json()
    nov_roster = client.get(f"/api/v1/cohorts/{november['id']}/roster", headers=admin_headers).json()
    assert {e["student_id"] for e in oct_roster} == {student1.id}
    assert {e["student_id"] for e in nov_roster} == {student2.id}

    # Each Cohort got its own COHORT-kind calendar for the shared stage -
    # editing one must never touch the other or the Program's own calendar.
    templates = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    cohort_templates = {t["cohort_id"]: t for t in templates if t["cohort_id"]}
    assert set(cohort_templates) == {october["id"], november["id"]}

    topic = client.post("/api/v1/topics", json={
        "title": "Cohort lesson", "level": "beginner",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Pick", "options": ["A", "B"], "correct_option": 0}],
    }, headers=teacher_a_headers).json()
    client.post(f"/api/v1/programs/{program['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": beginner, "status": "published",
    }, headers=teacher_a_headers)

    scheduled_oct = client.patch(
        f"/api/v1/calendar-templates/{cohort_templates[october['id']]['id']}",
        json={"days": [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {}}]},
        headers=admin_headers,
    )
    assert scheduled_oct.status_code == 200, scheduled_oct.text

    # November's own calendar is untouched by October's edit.
    nov_template = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    nov_days = next(t for t in nov_template if t["cohort_id"] == november["id"])["days"]
    assert nov_days == []
    oct_days = next(t for t in nov_template if t["cohort_id"] == october["id"])["days"]
    assert oct_days[0]["topic_ids"] == [topic["id"]]


async def test_student_with_two_enrollments_sees_independent_schedules(client):
    admin, admin_headers = await _login(client, "multi-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "multi-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "multi-student@example.com", Role.STUDENT, created_by_id=admin.id)

    general = _create_program(client, admin_headers, "multi-general-english", "General English")
    business = _create_program(client, admin_headers, "multi-business-english", "Business English")
    general_beginner = _stage_id(general, "beginner")
    business_intermediate = _stage_id(business, "intermediate")

    general_cohort = _create_cohort(
        client, admin_headers, name="General Batch", program=general, stage_ids=[general_beginner], lead_teacher_id=teacher.id,
    )
    business_cohort = _create_cohort(
        client, admin_headers, name="Business Batch", program=business, stage_ids=[business_intermediate], lead_teacher_id=teacher.id,
    )
    client.post(f"/api/v1/cohorts/{general_cohort['id']}/students", json={
        "student_ids": [student.id], "program_level_id": general_beginner,
    }, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{business_cohort['id']}/students", json={
        "student_ids": [student.id], "program_level_id": business_intermediate,
    }, headers=admin_headers)

    enrollments = client.get("/api/v1/enrollments/me", headers=student_headers).json()
    assert len(enrollments) == 2
    program_ids = {e["program_id"] for e in enrollments}
    assert program_ids == {general["id"], business["id"]}

    general_topic = client.post("/api/v1/topics", json={
        "title": "General lesson", "level": "beginner",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Pick", "options": ["A", "B"], "correct_option": 0}],
    }, headers=teacher_headers).json()
    client.post(f"/api/v1/programs/{general['id']}/topics", json={
        "topic_id": general_topic["id"], "program_level_id": general_beginner, "status": "published",
    }, headers=teacher_headers)
    general_templates = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    general_cohort_template = next(t for t in general_templates if t["cohort_id"] == general_cohort["id"])
    client.patch(f"/api/v1/calendar-templates/{general_cohort_template['id']}", json={
        "days": [{"day": 1, "topic_ids": [general_topic["id"]], "custom_minutes": {}}],
    }, headers=admin_headers)

    general_enrollment_id = next(e["id"] for e in enrollments if e["program_id"] == general["id"])
    business_enrollment_id = next(e["id"] for e in enrollments if e["program_id"] == business["id"])

    general_schedule = client.get(f"/api/v1/enrollments/{general_enrollment_id}/schedule", headers=student_headers).json()
    business_schedule = client.get(f"/api/v1/enrollments/{business_enrollment_id}/schedule", headers=student_headers).json()
    assert general_schedule["has_template"] is True
    assert general_schedule["days"][0]["topic_ids"] == [general_topic["id"]]
    # My Learning receives human-readable, progress-derived state from the
    # same enrollment context used for entitlement; it must not invent it.
    assert general_schedule["days"][0]["topic_titles"][general_topic["id"]] == "General lesson"
    assert general_schedule["days"][0]["topic_states"][general_topic["id"]] == "current"
    assert business_schedule["has_template"] is True
    assert business_schedule["days"] == []  # never scheduled - must not leak the General topic


async def test_a_student_cannot_read_another_students_enrollment_schedule(client):
    admin, admin_headers = await _login(client, "cross-admin@example.com", Role.ADMINISTRATOR)
    teacher, _ = await _login(client, "cross-teacher@example.com", Role.TEACHER)
    owner, owner_headers = await _login(client, "cross-owner@example.com", Role.STUDENT, created_by_id=admin.id)
    intruder, intruder_headers = await _login(client, "cross-intruder@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "cross-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [owner.id], "program_level_id": beginner,
    }, headers=admin_headers)

    owner_enrollment_id = client.get("/api/v1/enrollments/me", headers=owner_headers).json()[0]["id"]
    forbidden = client.get(f"/api/v1/enrollments/{owner_enrollment_id}/schedule", headers=intruder_headers)
    assert forbidden.status_code == 403
    assert client.get("/api/v1/enrollments/me", headers=intruder_headers).json() == []


async def test_multi_level_cohort_scopes_each_teacher_to_their_own_stage(client):
    admin, admin_headers = await _login(client, "stage-admin@example.com", Role.ADMINISTRATOR)
    lead, lead_headers = await _login(client, "stage-lead@example.com", Role.TEACHER)
    beginner_teacher, beginner_headers = await _login(client, "stage-beginner-teacher@example.com", Role.TEACHER)
    intermediate_teacher, intermediate_headers = await _login(client, "stage-intermediate-teacher@example.com", Role.TEACHER)
    outsider, outsider_headers = await _login(client, "stage-outsider@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "stage-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    intermediate = _stage_id(program, "intermediate")

    cohort = _create_cohort(
        client, admin_headers, name="Mixed Levels Batch", program=program, stage_ids=[beginner, intermediate],
        lead_teacher_id=lead.id,
        teacher_assignments=[
            {"teacher_id": beginner_teacher.id, "program_level_ids": [beginner]},
            {"teacher_id": intermediate_teacher.id, "program_level_ids": [intermediate]},
        ],
    )

    beginner_student, _ = await _login(client, "stage-beginner-student@example.com", Role.STUDENT, created_by_id=admin.id)
    intermediate_student, _ = await _login(client, "stage-intermediate-student@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [beginner_student.id], "program_level_id": beginner,
    }, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [intermediate_student.id], "program_level_id": intermediate,
    }, headers=admin_headers)

    # The Lead Teacher sees every stage's students.
    lead_roster = client.get(f"/api/v1/cohorts/{cohort['id']}/roster", headers=lead_headers).json()
    assert {e["student_id"] for e in lead_roster} == {beginner_student.id, intermediate_student.id}

    # A stage-scoped teacher sees only their own stage's students.
    beginner_roster = client.get(f"/api/v1/cohorts/{cohort['id']}/roster", headers=beginner_headers).json()
    assert {e["student_id"] for e in beginner_roster} == {beginner_student.id}
    intermediate_roster = client.get(f"/api/v1/cohorts/{cohort['id']}/roster", headers=intermediate_headers).json()
    assert {e["student_id"] for e in intermediate_roster} == {intermediate_student.id}

    # A teacher never assigned to this Cohort at all is refused outright.
    assert client.get(f"/api/v1/cohorts/{cohort['id']}/roster", headers=outsider_headers).status_code == 403
    assert client.get(f"/api/v1/cohorts/{cohort['id']}", headers=outsider_headers).status_code == 403


async def test_cohort_response_resolves_teacher_names_and_never_leaks_a_raw_id(client):
    """Regression: the Cohort Teachers tab was rendering a raw teacher_id
    whenever a name couldn't be resolved. `CohortPublic` now denormalizes a
    `teacher_name` onto the lead and every assignment server-side; a deleted
    teacher account must resolve to `None` (a graceful "unavailable"
    placeholder on the frontend), never to the id itself."""
    admin, admin_headers = await _login(client, "names-admin@example.com", Role.ADMINISTRATOR)
    lead, _ = await _login(client, "names-lead@example.com", Role.TEACHER)
    gone, _ = await _login(client, "names-gone@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "names-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(
        client, admin_headers, name="Name Resolution Batch", program=program, stage_ids=[beginner],
        lead_teacher_id=lead.id,
        teacher_assignments=[{"teacher_id": gone.id, "program_level_ids": [beginner]}],
    )
    assert cohort["lead_teacher_name"] == lead.full_name
    assert cohort["teacher_assignments"][0]["teacher_name"] == gone.full_name

    gone_doc = await User.get(gone.id)
    gone_doc.is_deleted = True
    await gone_doc.save()

    reloaded = client.get(f"/api/v1/cohorts/{cohort['id']}", headers=admin_headers).json()
    assert reloaded["teacher_assignments"][0]["teacher_name"] is None
    assert reloaded["teacher_assignments"][0]["teacher_id"] == gone.id, "the id itself is preserved for permissions"


async def test_enrolling_a_non_student_or_outside_the_cohorts_stages_is_rejected(client):
    admin, admin_headers = await _login(client, "reject-admin@example.com", Role.ADMINISTRATOR)
    teacher, _ = await _login(client, "reject-teacher@example.com", Role.TEACHER)
    other_teacher, _ = await _login(client, "reject-other-teacher@example.com", Role.TEACHER)
    student, _ = await _login(client, "reject-student@example.com", Role.STUDENT, created_by_id=admin.id)

    program = _create_program(client, admin_headers, "reject-general-english", "General English")
    beginner = _stage_id(program, "beginner")
    intermediate = _stage_id(program, "intermediate")
    cohort = _create_cohort(client, admin_headers, name="Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)

    not_a_student = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [other_teacher.id], "program_level_id": beginner,
    }, headers=admin_headers)
    assert not_a_student.status_code == 422

    wrong_stage = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [student.id], "program_level_id": intermediate,
    }, headers=admin_headers)
    assert wrong_stage.status_code == 422

    only_administrator = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [student.id], "program_level_id": beginner,
    })
    assert only_administrator.status_code == 401


async def test_batch_candidate_search_pagination_and_bulk_enrollment_are_scoped(client):
    admin, admin_headers = await _login(client, "candidate-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "candidate-teacher@example.com", Role.TEACHER)
    enrolled, _ = await _login(client, "candidate-enrolled@example.com", Role.STUDENT, created_by_id=admin.id)
    await _login(client, "candidate-alice@example.com", Role.STUDENT, created_by_id=admin.id)
    bob, _ = await _login(client, "candidate-bob@example.com", Role.STUDENT, created_by_id=admin.id)
    teacher_student, _ = await _login(
        client, "candidate-teacher-student@example.com", Role.STUDENT, created_by_id=teacher.id
    )

    program = _create_program(client, admin_headers, "candidate-program", "Candidate Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Candidate Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    initial = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [enrolled.id], "program_level_id": beginner,
    }, headers=admin_headers)
    assert initial.status_code == 201, initial.text

    # Already-enrolled students are not candidates, and server pagination is
    # over the matching candidate set rather than a browser-side first-100 cap.
    page = client.get(f"/api/v1/cohorts/{cohort['id']}/student-candidates?page=1&limit=1", headers=admin_headers)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1
    searched = client.get(f"/api/v1/cohorts/{cohort['id']}/student-candidates?search=bob&page=1&limit=25", headers=admin_headers)
    assert [item["id"] for item in searched.json()["items"]] == [bob.id]

    # An assigned teacher sees and can enroll only student accounts they own;
    # center-wide candidate visibility remains an administrator privilege.
    teacher_page = client.get(
        f"/api/v1/cohorts/{cohort['id']}/student-candidates?page=1&limit=25", headers=teacher_headers
    )
    assert teacher_page.status_code == 200, teacher_page.text
    assert [item["id"] for item in teacher_page.json()["items"]] == [teacher_student.id]
    teacher_result = client.post(f"/api/v1/cohorts/{cohort['id']}/students/bulk", json={
        "student_ids": [teacher_student.id], "program_level_id": beginner,
    }, headers=teacher_headers)
    assert teacher_result.status_code == 201, teacher_result.text
    assert [item["student_id"] for item in teacher_result.json()["created"]] == [teacher_student.id]
    assert client.post(f"/api/v1/cohorts/{cohort['id']}/students/bulk", json={
        "student_ids": [bob.id], "program_level_id": beginner,
    }, headers=teacher_headers).status_code == 403

    # The new endpoint reports a stale duplicate selection explicitly and
    # retains a student's separate Program/Batch enrollment elsewhere.
    result = client.post(f"/api/v1/cohorts/{cohort['id']}/students/bulk", json={
        "student_ids": [bob.id, enrolled.id], "program_level_id": beginner,
    }, headers=admin_headers)
    assert result.status_code == 201, result.text
    assert [item["student_id"] for item in result.json()["created"]] == [bob.id]
    assert result.json()["already_enrolled_student_ids"] == [enrolled.id]

    roster = client.get(f"/api/v1/cohorts/{cohort['id']}/roster/page?search=bob&page=1&limit=1", headers=admin_headers)
    assert roster.status_code == 200, roster.text
    assert roster.json()["total"] == 1
    assert roster.json()["items"][0]["student_id"] == bob.id


async def test_only_administrator_creates_cohorts(client):
    admin, admin_headers = await _login(client, "create-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "create-teacher@example.com", Role.TEACHER)
    program = _create_program(client, admin_headers, "create-general-english", "General English")
    beginner = _stage_id(program, "beginner")

    forbidden = client.post("/api/v1/cohorts", json={
        "name": "Batch", "program_id": program["id"], "program_level_ids": [beginner],
        "lead_teacher_id": teacher.id, "teacher_assignments": [], "start_date": utcnow().isoformat(),
    }, headers=teacher_headers)
    assert forbidden.status_code == 403


async def test_administrator_can_pause_a_batch_and_keep_its_end_date_optional(client):
    admin, admin_headers = await _login(client, "pause-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "pause-teacher@example.com", Role.TEACHER)
    program = _create_program(client, admin_headers, "pause-program", "Pause Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Pause Batch", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    response = client.patch(f"/api/v1/cohorts/{cohort['id']}", json={"status": "inactive", "end_date": None}, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "inactive"
    assert response.json()["end_date"] is None
    teacher_response = client.patch(f"/api/v1/cohorts/{cohort['id']}", json={"status": "active"}, headers=teacher_headers)
    assert teacher_response.status_code == 200, teacher_response.text
    assert teacher_response.json()["status"] == "active"
    assert client.patch(f"/api/v1/cohorts/{cohort['id']}", json={"lead_teacher_id": teacher.id}, headers=teacher_headers).status_code == 403


async def test_teacher_reuses_topic_and_schedules_only_their_assigned_batch_stage(client):
    admin, admin_headers = await _login(client, "workflow-admin@example.com", Role.ADMINISTRATOR)
    lead, _ = await _login(client, "workflow-lead@example.com", Role.TEACHER)
    teacher, teacher_headers = await _login(client, "workflow-teacher@example.com", Role.TEACHER)
    outsider, outsider_headers = await _login(client, "workflow-outsider@example.com", Role.TEACHER)
    student, student_headers = await _login(
        client, "workflow-student@example.com", Role.STUDENT, created_by_id=admin.id
    )

    program = _create_program(client, admin_headers, "workflow-program", "Workflow Program")
    beginner = _stage_id(program, "beginner")
    intermediate = _stage_id(program, "intermediate")
    cohort = _create_cohort(
        client,
        admin_headers,
        name="Workflow Batch",
        program=program,
        stage_ids=[beginner, intermediate],
        lead_teacher_id=lead.id,
        teacher_assignments=[{"teacher_id": teacher.id, "program_level_ids": [beginner]}],
    )
    enrolled = client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={
        "student_ids": [student.id], "program_level_id": beginner,
    }, headers=admin_headers)
    assert enrolled.status_code == 201, enrolled.text
    enrollment_id = enrolled.json()[0]["id"]

    detail = client.get(f"/api/v1/cohorts/{cohort['id']}", headers=teacher_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["accessible_program_level_ids"] == [beginner]

    topic = client.post("/api/v1/topics", json={
        "title": "Reusable workflow lesson",
        "level": "beginner",
        "lesson_exercises": [{
            "kind": "multiple_choice", "prompt": "Choose hello",
            "options": ["Hello", "Goodbye"], "correct_option": 0,
        }],
    }, headers=teacher_headers)
    assert topic.status_code == 200, topic.text

    assigned = client.post(f"/api/v1/cohorts/{cohort['id']}/topics", json={
        "topic_id": topic.json()["id"], "program_level_id": beginner, "status": "published",
    }, headers=teacher_headers)
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["program_level_id"] == beginner

    wrong_stage = client.post(f"/api/v1/cohorts/{cohort['id']}/topics", json={
        "topic_id": topic.json()["id"], "program_level_id": intermediate, "status": "published",
    }, headers=teacher_headers)
    assert wrong_stage.status_code == 422
    assert client.post(f"/api/v1/cohorts/{cohort['id']}/topics", json={
        "topic_id": topic.json()["id"], "program_level_id": beginner, "status": "published",
    }, headers=outsider_headers).status_code == 403

    # The Teacher can now see and update only their assigned Cohort-stage
    # calendar even though the Center Admin is its persisted owner.
    teacher_templates = client.get("/api/v1/calendar-templates", headers=teacher_headers)
    assert teacher_templates.status_code == 200, teacher_templates.text
    cohort_templates = [item for item in teacher_templates.json() if item["cohort_id"] == cohort["id"]]
    assert len(cohort_templates) == 1
    assert cohort_templates[0]["program_level_id"] == beginner
    calendar_id = cohort_templates[0]["id"]

    scheduled = client.patch(f"/api/v1/calendar-templates/{calendar_id}", json={
        "days": [{"day": 1, "topic_ids": [topic.json()["id"]], "custom_minutes": {}}],
    }, headers=teacher_headers)
    assert scheduled.status_code == 200, scheduled.text

    schedule = client.get(f"/api/v1/enrollments/{enrollment_id}/schedule", headers=student_headers)
    assert schedule.status_code == 200, schedule.text
    assert schedule.json()["days"][0]["topic_ids"] == [topic.json()["id"]]
