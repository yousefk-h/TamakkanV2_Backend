"""Thin wrapper around `transcript_english_review.py`'s Gemini call - the
topic lookup exists only to keep this endpoint scoped and guarded the same
way the rest of the transcript pipeline is (real topic, not already
approved), not because the review itself reads or writes anything on the
topic document. Kept as its own service rather than folded into
`TranscriptPipelineService`, whose constructor already threads through five
pipeline collaborators for a genuinely different concern (the transcript
state machine) - this one only ever needs a topic lookup and the reviewer.
"""

from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.transcript_english_review import (
    TranscriptEnglishReviewer,
    TranscriptReviewResult,
    get_english_reviewer,
)
from app.models.topic_content import TranscriptState
from app.repositories.topic_content_repository import TopicContentRepository


class TranscriptReviewService:
    def __init__(self, topic_repository: TopicContentRepository, reviewer: TranscriptEnglishReviewer) -> None:
        self.topic_repository = topic_repository
        self.reviewer = reviewer

    async def review_english(self, topic_id: str, transcript: str) -> TranscriptReviewResult:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        if topic.transcript_state == TranscriptState.APPROVED_FOR_AI:
            # Not a real restriction on the Gemini call itself - just keeping
            # this framed as "before you approve", matching where the button
            # actually lives in the builder, rather than leaving a stray path
            # to re-review text nothing downstream will ever read again.
            raise ConflictException("This transcript is already approved - there is nothing left to review")
        if not transcript.strip():
            raise UnprocessableException("There is no transcript text to review")
        return await self.reviewer.review(transcript)


def get_transcript_review_service() -> TranscriptReviewService:
    return TranscriptReviewService(TopicContentRepository(), get_english_reviewer())
