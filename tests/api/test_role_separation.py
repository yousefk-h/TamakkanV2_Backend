"""PROMPT-047/PROMPT-049: focused regression tests for Administrator (Center Admin) vs. Teacher separation.
Teacher-vs-Teacher isolation itself is already covered where it was tested before this change - see
`test_topics.py` / `test_units.py` (now logging in as `Role.TEACHER`) and `test_users_page.py`. This file covers
only what's new: the Administrator's center-wide reach (students, teachers, topics, and - PROMPT-049 - calendar
templates, classes, sessions, submissions, analytics), and that a Teacher cannot touch Administrator-only routes.
"""

from datetime import timedelta

from app.core.security import hash_password
from app.models.live_session import LiveSession, PracticeMode, SessionState
from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.models.topic_content import Level, TopicContent
from app.models.user import Role, User
from app.utils.time import utcnow
from tests.api.test_submissions import FakeAudioStorage, _override_storage

TOPIC_PAYLOAD = {
    "title": "Talking about Music",
    "level": "intermediate",
    "youtube_urls": ["https://youtube.com/watch?v=abc123"],
    "warm_up_questions": ["What's your favorite genre?"],
}


async def _login(client, role: Role, email: str, password: str = "correct-password") -> str:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def _create_student(client, token: str, email: str) -> dict:
    return client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Test Student"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()


async def _create_topic(client, token: str, **overrides) -> dict:
    return client.post(
        "/api/v1/topics", json={**TOPIC_PAYLOAD, **overrides}, headers={"Authorization": f"Bearer {token}"}
    ).json()


# --- 1. Administrator can list all teachers -------------------------------------------------------------------


async def test_administrator_can_list_all_teachers(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    await _login(client, Role.TEACHER, "teacher-a@example.com")
    await _login(client, Role.TEACHER, "teacher-b@example.com")

    resp = client.get("/api/v1/teachers", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    emails = {t["email"] for t in resp.json()}
    assert emails == {"teacher-a@example.com", "teacher-b@example.com"}


# --- 2. Administrator can list all students -------------------------------------------------------------------


async def test_administrator_can_list_all_students(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    teacher_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    await _create_student(client, teacher_a, "student-a@example.com")
    await _create_student(client, teacher_b, "student-b@example.com")

    resp = client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    emails = {s["email"] for s in resp.json()}
    assert emails == {"student-a@example.com", "student-b@example.com"}


# --- 3. Administrator can manage a student created by another teacher ------------------------------------------


async def test_administrator_can_manage_a_teachers_student(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    student = await _create_student(client, teacher_token, "student@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "new@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "new@example.com"

    deleted = client.delete(f"/api/v1/users/{student['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert deleted.status_code == 204


# --- 4. Teacher cannot manage another teacher's student ----------------------------------------------------------


async def test_teacher_cannot_manage_another_teachers_student(client):
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    teacher_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    student = await _create_student(client, teacher_a, "student@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "hijacked@example.com"},
        headers={"Authorization": f"Bearer {teacher_b}"},
    )
    assert resp.status_code == 403


# --- 5. Administrator can manage another teacher's topic ----------------------------------------------------------


async def test_administrator_can_manage_a_teachers_topic(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    topic = await _create_topic(client, teacher_token)

    resp = client.patch(
        f"/api/v1/topics/{topic['id']}",
        json={"title": "Edited by the Center Admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Edited by the Center Admin"

    deleted = client.delete(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert deleted.status_code == 204


async def test_administrator_sees_every_topic_in_the_center(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    teacher_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, teacher_a, title="Topic A")
    topic_b = await _create_topic(client, teacher_b, title="Topic B")

    resp = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {admin_token}"})
    ids = {t["id"] for t in resp.json()}
    assert {topic_a["id"], topic_b["id"]} <= ids


# --- 6. Teacher cannot manage another teacher's topic (see test_topics.py's renamed teacher_a/teacher_b tests) --


# --- 7. Teacher cannot create/manage teachers --------------------------------------------------------------------


async def test_teacher_cannot_create_a_teacher(client):
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    resp = client.post(
        "/api/v1/teachers",
        json={"email": "new-teacher@example.com", "password": "password123", "full_name": "New Teacher"},
        headers={"Authorization": f"Bearer {teacher_token}"},
    )
    assert resp.status_code == 403


async def test_teacher_cannot_list_teachers(client):
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    resp = client.get("/api/v1/teachers", headers={"Authorization": f"Bearer {teacher_token}"})
    assert resp.status_code == 403


async def test_teacher_cannot_manage_another_teachers_account(client):
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    await _login(client, Role.TEACHER, "teacher-b@example.com")
    teacher_b_user = await User.find_one(User.email == "teacher-b@example.com")

    resp = client.patch(
        f"/api/v1/teachers/{teacher_b_user.id}/email",
        json={"email": "hijacked@example.com"},
        headers={"Authorization": f"Bearer {teacher_a}"},
    )
    assert resp.status_code == 403

    resp = client.delete(f"/api/v1/teachers/{teacher_b_user.id}", headers={"Authorization": f"Bearer {teacher_a}"})
    assert resp.status_code == 403


# --- 8. Users pagination remains scoped correctly by role ---------------------------------------------------------


async def test_students_pagination_is_scoped_by_role(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    teacher_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    await _create_student(client, teacher_a, "student-a@example.com")
    await _create_student(client, teacher_b, "student-b@example.com")

    admin_page = client.get(
        "/api/v1/users", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    assert admin_page["total"] == 2

    teacher_page = client.get(
        "/api/v1/users", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {teacher_a}"}
    ).json()
    assert teacher_page["total"] == 1
    assert teacher_page["items"][0]["email"] == "student-a@example.com"


async def test_teachers_pagination_and_counts(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    await _login(client, Role.TEACHER, "teacher-a@example.com")
    await _login(client, Role.TEACHER, "teacher-b@example.com")

    page = client.get(
        "/api/v1/teachers", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    assert page["total"] == 2

    counts = client.get("/api/v1/teachers/counts", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert counts == {"total_teachers": 2, "active_teachers": 2}


# --- PROMPT-049: center-wide calendar templates / classes / sessions / submissions / analytics -------------------


async def test_administrator_can_open_and_list_a_teachers_topic(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    topic = await _create_topic(client, teacher_token)

    resp = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200 and resp.json()["id"] == topic["id"]


async def test_administrator_sees_and_manages_a_teachers_calendar_template(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    topic = await _create_topic(client, teacher_token, level="beginner")

    created = client.post(
        "/api/v1/calendar-templates",
        json={"name": "Beginner starter", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers={"Authorization": f"Bearer {teacher_token}"},
    ).json()

    listed = client.get("/api/v1/calendar-templates", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert any(t["id"] == created["id"] for t in listed)

    updated = client.patch(
        f"/api/v1/calendar-templates/{created['id']}",
        json={"name": "Renamed by Center Admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert updated.status_code == 200 and updated.json()["name"] == "Renamed by Center Admin"

    activated = client.post(f"/api/v1/calendar-templates/{created['id']}/activate", headers={"Authorization": f"Bearer {admin_token}"})
    assert activated.status_code == 200 and activated.json()["is_active"] is True


async def test_administrator_sees_and_manages_a_teachers_class(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    student = await _create_student(client, teacher_token, "student@example.com")
    client.post(
        "/api/v1/users/{}/assign-plan".format(student["id"]),
        json={"plan": "standard", "assigned_level": "beginner"},
        headers={"Authorization": f"Bearer {teacher_token}"},
    )

    when = (utcnow() + timedelta(days=2)).replace(microsecond=0).isoformat()
    created = client.post(
        "/api/v1/classes",
        json={"kind": "group", "title": "Group class", "scheduled_at": when, "duration_minutes": 30,
              "meet_link": "https://meet.example.com/x", "participant_ids": [student["id"]]},
        headers={"Authorization": f"Bearer {teacher_token}"},
    ).json()

    listed = client.get("/api/v1/classes", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert any(c["id"] == created["id"] for c in listed)

    updated = client.patch(
        f"/api/v1/classes/{created['id']}", json={"title": "Renamed by Center Admin"}, headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert updated.status_code == 200 and updated.json()["title"] == "Renamed by Center Admin"

    cancelled = client.delete(f"/api/v1/classes/{created['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert cancelled.status_code == 204


async def test_administrator_sees_a_teachers_sessions_and_submissions(client):
    _override_storage(FakeAudioStorage())
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    student_dict = await _create_student(client, teacher_token, "student@example.com")
    student = await User.get(student_dict["id"])
    topic = await _create_topic(client, teacher_token, level="beginner")
    topic_doc = await TopicContent.get(topic["id"])

    session = LiveSession(
        student=student, student_id=student.id, topic=topic_doc, topic_id=topic_doc.id, mode=PracticeMode.TOPIC_DISCUSSION,
        assessment_level=Level.BEGINNER.value, state=SessionState.ENDED, started_at=utcnow(),
    )
    await session.insert()
    submission = RecordingSubmission(
        student=student, student_id=student.id, kind=SubmissionKind.AUDIO_TASK, topic=topic_doc, topic_id=topic_doc.id,
        label=topic_doc.title, media_url="https://example.com/x.mp3", cloudinary_public_id="x", duration_seconds=30.0,
    )
    await submission.insert()

    sessions = client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert any(s["id"] == session.id for s in sessions)

    submissions = client.get("/api/v1/submissions", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert any(s["id"] == submission.id for s in submissions)


async def test_administrator_analytics_are_center_wide(client):
    admin_token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    teacher_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    teacher_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    await _create_student(client, teacher_a, "student-a@example.com")
    await _create_student(client, teacher_b, "student-b@example.com")

    admin_analytics = client.get("/api/v1/analytics", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert admin_analytics["roster_students"] == 2

    teacher_analytics = client.get("/api/v1/analytics", headers={"Authorization": f"Bearer {teacher_a}"}).json()
    assert teacher_analytics["roster_students"] == 1
