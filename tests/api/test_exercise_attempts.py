"""Persisted lesson answers, trusted scoring, and teacher review boundaries."""

from datetime import timedelta

from app.core.security import hash_password
from app.models.topic_content import Level
from app.models.user import PlanType, Role, User
from app.utils.time import utcnow


async def _login(client, email: str, role: Role, **extra) -> tuple[str, str]:
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"})
    assert response.status_code == 200
    return response.json()["access_token"], user.id


async def test_student_answers_and_teacher_review_are_persisted_and_scoped(client):
    teacher_token, teacher_id = await _login(client, "lesson-teacher@example.com", Role.TEACHER)
    other_token, _ = await _login(client, "other-teacher@example.com", Role.TEACHER)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_token, _ = await _login(client, "lesson-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    outsider_token, _ = await _login(client, "other-student@example.com", Role.STUDENT)
    student_headers = {"Authorization": f"Bearer {student_token}"}
    exercises = [
        {"kind": "multiple_choice", "prompt": "Choose the greeting", "options": ["Hello", "Goodbye"], "correct_option": 0, "allow_resubmit": False},
        {"kind": "writing", "prompt": "Write two words", "min_words": 2, "deadline_at": (utcnow() + timedelta(days=1)).isoformat()},
        {"kind": "game", "prompt": "Match", "pairs": [{"left": "hello", "right": "مرحبا"}, {"left": "bye", "right": "وداعا"}]},
        {"kind": "writing", "prompt": "Expired response", "deadline_at": (utcnow() - timedelta(minutes=1)).isoformat()},
    ]
    topic_response = client.post("/api/v1/topics", json={"title": "Greetings", "level": "beginner", "lesson_exercises": exercises}, headers=teacher_headers)
    assert topic_response.status_code == 200
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert template.status_code == 200
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    assert student_topic.json()["lesson_exercises"][0]["correct_option"] is None
    revision = student_topic.json()["updated_at"]
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers={"Authorization": f"Bearer {outsider_token}"}).status_code == 403
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers=teacher_headers).status_code == 403
    choice = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers=student_headers)
    assert choice.status_code == 201
    assert choice.json()["correct"] is True
    assert choice.json()["score"] == 100
    assert choice.json()["reviewed"] is True
    assert choice.json()["correct_option"] == 0
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1}, headers=student_headers).status_code == 409
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "one"}, headers=student_headers).status_code == 422
    writing = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "Hello teacher"}, headers=student_headers)
    assert writing.status_code == 201
    assert writing.json()["reviewed"] is False
    first_writing_id = writing.json()["id"]
    writing = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "Good morning"}, headers=student_headers)
    assert writing.status_code == 201
    game = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 2, "topic_updated_at": revision, "answer": [1, 0]}, headers=student_headers)
    assert game.status_code == 201
    assert game.json()["score"] == 0
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 3, "topic_updated_at": revision, "answer": "Too late"}, headers=student_headers).status_code == 403

    queue = client.get("/api/v1/exercise-attempts/review", headers=teacher_headers)
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()] == [writing.json()["id"]]
    assert client.get("/api/v1/exercise-attempts/review", headers={"Authorization": f"Bearer {other_token}"}).json() == []
    route = f"/api/v1/exercise-attempts/{writing.json()['id']}/review"
    assert client.patch(f"/api/v1/exercise-attempts/{first_writing_id}/review", json={"score": 50}, headers=teacher_headers).status_code == 404
    assert client.patch(route, json={"score": 85, "feedback": "Good start"}, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
    reviewed = client.patch(route, json={"score": 85, "feedback": "Good start"}, headers=teacher_headers)
    assert reviewed.status_code == 200
    assert reviewed.json()["reviewed"] is True
    mine = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic_id}", headers=student_headers)
    assert mine.status_code == 200
    assert {item["exercise_index"] for item in mine.json()} == {0, 1, 2}
    assert next(item for item in mine.json() if item["exercise_index"] == 1)["feedback"] == "Good start"
    assert client.get("/api/v1/exercise-attempts/review", headers=teacher_headers).json() == []

    client.patch(f"/api/v1/topics/{topic_id}", json={"title": "Updated greetings"}, headers=teacher_headers)
    stale = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1}, headers=student_headers)
    assert stale.status_code == 409
