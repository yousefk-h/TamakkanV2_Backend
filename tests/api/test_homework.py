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
