"""Operations counts come from scoped, persisted session records."""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.live_session import LiveSession, PracticeMode, SessionState
from app.models.user import Role, User

NOW = datetime(2026, 11, 4, 12)


async def user(email, role, **extra):
    item = User(email=email, role=role, password_hash="unused", full_name="Test", **extra)
    await item.insert()
    return item


def headers(item):
    return {"Authorization": f"Bearer {create_access_token(item.id, item.role)}"}


async def session(student, *, state=SessionState.CONNECTED, mode=PracticeMode.TOPIC_DISCUSSION, at=NOW, **extra):
    item = LiveSession(student=student, student_id=student.id, state=state, mode=mode, started_at=at, **extra)
    await item.insert()
    return item


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("app.api.v1.operations.utcnow", lambda: NOW)


async def test_operations_counts_real_states_and_scopes_teacher_roster(client):
    teacher = await user("teacher@example.com", Role.TEACHER)
    other = await user("other@example.com", Role.TEACHER)
    admin = await user("admin@example.com", Role.ADMINISTRATOR)
    student = await user("student@example.com", Role.STUDENT, created_by_id=teacher.id)
    outsider = await user("outsider@example.com", Role.STUDENT, created_by_id=other.id)
    await session(student)
    await session(student, mode=PracticeMode.VOICE_PRACTICE)
    await session(student, state=SessionState.CONNECTING)
    await session(student, state=SessionState.ENDED, ended_at=NOW, handoff_source_session_id="source")
    await session(student, state=SessionState.CONNECTION_FAILED)
    await session(student, state=SessionState.ENDED, at=NOW - timedelta(days=2), ended_at=NOW - timedelta(days=2))
    await session(student, is_deleted=True)
    await session(outsider)

    response = client.get("/api/v1/operations/sessions", headers=headers(teacher))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["connected_by_mode"] == {"topic_discussion": 1, "voice_practice": 1}
    assert (body["connecting"], body["connection_failures_24h"], body["ended_24h"], body["handoffs_24h"]) == (1, 1, 1, 1)
    assert len(body["recent_sessions"]) == 6
    assert all("transcript" not in row and "student_id" not in row for row in body["recent_sessions"])
    center = client.get("/api/v1/operations/sessions", headers=headers(admin)).json()
    assert center["connected_by_mode"]["topic_discussion"] == 2
    assert client.get("/api/v1/operations/sessions", headers=headers(student)).status_code == 403
    assert client.get("/api/v1/operations/sessions").status_code in (401, 403)
