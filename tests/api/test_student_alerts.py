"""The admin roster's warning badge and the student-detail "Alerts" section -
`GET /users/alerts` and `GET /users/{student_id}/alerts`. See
`ProgressService.get_alerts`/`get_alerts_for_students` for the computation.
"""

from datetime import timedelta

from app.core.security import hash_password
from app.models.user import Role, User
from app.utils.time import utcnow


async def _login(client, role: Role, email: str, password: str = "correct-password") -> str:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_student(client, admin_token: str, email: str = "student@example.com") -> dict:
    return client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "A Student"},
        headers=_headers(admin_token),
    ).json()


async def _create_topic(client, admin_token: str, level: str = "beginner") -> dict:
    return client.post("/api/v1/topics", json={"title": "Topic", "level": level}, headers=_headers(admin_token)).json()


async def test_a_student_with_no_plan_has_no_alerts(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)

    resp = client.get(f"/api/v1/users/{student['id']}/alerts", headers=_headers(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_rollover_count"] == 0
    assert body["missed_submission_days"] == []
    assert body["has_alerts"] is False


async def test_a_standard_student_with_no_active_template_has_no_rollover_alert(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "beginner"},
        headers=_headers(admin_token),
    )

    resp = client.get(f"/api/v1/users/{student['id']}/alerts", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert resp.json()["has_alerts"] is False


async def test_the_roster_endpoint_returns_alerts_keyed_by_student_id(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    first = await _create_student(client, admin_token, email="first@example.com")
    second = await _create_student(client, admin_token, email="second@example.com")

    resp = client.get("/api/v1/users/alerts", headers=_headers(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {first["id"], second["id"]}
    assert body[first["id"]]["has_alerts"] is False


async def test_an_admin_cannot_see_another_admins_student_alerts(client):
    owner_token = await _login(client, Role.TEACHER, "owner@example.com")
    other_token = await _login(client, Role.TEACHER, "other@example.com")
    student = await _create_student(client, owner_token)

    resp = client.get(f"/api/v1/users/{student['id']}/alerts", headers=_headers(other_token))
    assert resp.status_code == 403


async def test_a_students_own_roster_only_includes_their_own_students(client):
    owner_token = await _login(client, Role.TEACHER, "owner@example.com")
    other_token = await _login(client, Role.TEACHER, "other@example.com")
    await _create_student(client, owner_token, email="mine@example.com")
    await _create_student(client, other_token, email="theirs@example.com")

    resp = client.get("/api/v1/users/alerts", headers=_headers(owner_token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_student_cannot_call_either_alerts_endpoint(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    student_token = await _login(client, Role.STUDENT, "s@example.com")

    assert client.get(f"/api/v1/users/{student['id']}/alerts", headers=_headers(student_token)).status_code == 403
    assert client.get("/api/v1/users/alerts", headers=_headers(student_token)).status_code == 403


async def test_a_student_with_a_rolled_over_lesson_shows_a_rollover_alert(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    topic = await _create_topic(client, admin_token)
    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "T", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers=_headers(admin_token),
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers=_headers(admin_token))
    # A start date 2 days back anchors "today" to day 3, pushing day 1's
    # still-uncompleted lesson into the rollover backlog.
    start_date = (utcnow() - timedelta(days=2)).date().isoformat()
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "beginner", "start_date": start_date},
        headers=_headers(admin_token),
    )

    resp = client.get(f"/api/v1/users/{student['id']}/alerts", headers=_headers(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_rollover_count"] == 2  # day 1 and day 2 both missed, carried to day 3
    assert body["has_alerts"] is True
