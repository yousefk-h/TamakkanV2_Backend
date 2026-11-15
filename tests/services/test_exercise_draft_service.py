"""Fix 2: a swallowed provider/schema exception must surface a sanitized
summary of the real failure (type + message), both in the raised exception
and the log - never just the exception's type name - while never leaking a
configured GEMINI_API_KEY value."""

import pytest

from app.core.config import get_settings
from app.core.exceptions import UpstreamServiceException
from app.core.exercise_drafting import ExerciseDraftingError
from app.models.topic_content import ExerciseKind, Level, TopicContent
from app.models.user import Role, User
from app.schema.topic import TopicCreate
from app.services.exercise_draft_service import (
    DraftExercisesRequest,
    ExerciseDraftService,
    _sanitize_exc_message,
)
from app.services.topic_content_service import get_topic_content_service


class AlwaysFailsDrafter:
    last_usage = None

    def __init__(self, message: str):
        self.message = message

    async def draft(self, **kwargs):
        raise ExerciseDraftingError(self.message)


async def _make_topic(teacher: User) -> TopicContent:
    topics = get_topic_content_service()
    topic = await topics.create(TopicCreate(title="Routine", level=Level.BEGINNER), teacher)
    stored = await TopicContent.get(topic.id)
    stored.approved_transcript = "Maya describes her typical morning routine. " * 5
    await stored.save()
    return stored


async def test_sanitize_exc_message_redacts_configured_gemini_api_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "GEMINI_API_KEY", "sk-test-secret-123")
    exc = ValueError("provider failed with key sk-test-secret-123 in payload")
    sanitized = _sanitize_exc_message(exc)
    assert "sk-test-secret-123" not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_exc_message_truncates_long_messages():
    exc = ValueError("x" * 500)
    sanitized = _sanitize_exc_message(exc)
    assert len(sanitized) <= 203  # 200 + "..."


async def test_draft_failure_surfaces_sanitized_real_error_after_retries():
    teacher = User(email="exercise-draft-fail@example.com", role=Role.TEACHER, password_hash="x", full_name="Teacher")
    await teacher.insert()
    topic = await _make_topic(teacher)

    distinctive = "upstream provider exploded: invalid schema for field 'prompt'"
    service = ExerciseDraftService(get_topic_content_service(), AlwaysFailsDrafter(distinctive))

    with pytest.raises(UpstreamServiceException) as exc_info:
        await service.draft(
            str(topic.id), teacher,
            DraftExercisesRequest(kind=ExerciseKind.READING, count=1),
        )
    message = str(exc_info.value)
    assert "ExerciseDraftingError" in message
    assert distinctive in message
    assert "Try again or write it manually" in message


async def test_draft_failure_redacts_api_key_from_raised_message(monkeypatch):
    monkeypatch.setattr(get_settings(), "GEMINI_API_KEY", "sk-leaky-test-key")

    teacher = User(email="exercise-draft-fail-key@example.com", role=Role.TEACHER, password_hash="x", full_name="Teacher")
    await teacher.insert()
    topic = await _make_topic(teacher)

    leaking_message = "auth failed for key sk-leaky-test-key"
    service = ExerciseDraftService(get_topic_content_service(), AlwaysFailsDrafter(leaking_message))

    with pytest.raises(UpstreamServiceException) as exc_info:
        await service.draft(
            str(topic.id), teacher,
            DraftExercisesRequest(kind=ExerciseKind.READING, count=1),
        )
    message = str(exc_info.value)
    assert "sk-leaky-test-key" not in message
    assert "[REDACTED]" in message
