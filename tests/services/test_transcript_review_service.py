"""Kanban follow-up to card #11: the English-proofreading aid over the
transcript review queue, behind a fake reviewer - no real network call here.
"""

import pytest

from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.transcript_english_review import TranscriptCorrection, TranscriptReviewResult
from app.models.topic_content import Level, TopicContent, TranscriptState
from app.repositories.topic_content_repository import TopicContentRepository
from app.services.transcript_review_service import TranscriptReviewService


class FakeEnglishReviewer:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else TranscriptReviewResult(
            corrected_transcript="Hello, world.",
            corrections=[
                TranscriptCorrection(
                    original="hello world",
                    corrected="Hello, world.",
                    category="punctuation",
                    explanation="Added a comma and capitalized the greeting.",
                )
            ],
        )
        self.error = error
        self.calls: list[str] = []

    async def review(self, transcript):
        self.calls.append(transcript)
        if self.error is not None:
            raise self.error
        return self.result


def _make_service(reviewer=None):
    return TranscriptReviewService(TopicContentRepository(), reviewer or FakeEnglishReviewer())


async def _make_topic() -> TopicContent:
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    await topic.insert()
    return topic


async def test_review_english_returns_the_reviewers_result():
    topic = await _make_topic()
    reviewer = FakeEnglishReviewer()
    service = _make_service(reviewer=reviewer)

    result = await service.review_english(topic.id, "hello world")

    assert result.corrected_transcript == "Hello, world."
    assert len(result.corrections) == 1
    assert reviewer.calls == ["hello world"]


async def test_review_english_unknown_topic_is_404():
    with pytest.raises(NotFoundException):
        await _make_service().review_english("not-a-real-id", "hello world")


async def test_review_english_rejects_blank_transcript():
    topic = await _make_topic()
    with pytest.raises(UnprocessableException):
        await _make_service().review_english(topic.id, "   ")


async def test_review_english_rejects_an_already_approved_topic():
    topic = await _make_topic()
    await TopicContentRepository().update_fields(
        topic.id, {"transcript_state": TranscriptState.APPROVED_FOR_AI, "approved_transcript": "Hello world."}
    )
    with pytest.raises(ConflictException):
        await _make_service().review_english(topic.id, "hello world")


async def test_review_english_propagates_review_errors():
    topic = await _make_topic()
    reviewer = FakeEnglishReviewer(error=RuntimeError("gemini is down"))
    with pytest.raises(RuntimeError):
        await _make_service(reviewer=reviewer).review_english(topic.id, "hello world")
