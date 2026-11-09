"""Thin wrapper around `transcript_english_review.py`'s Gemini call - the
topic lookup exists only to keep this endpoint scoped and guarded the same
way the rest of the transcript pipeline is (real topic, not already
approved), not because the review itself reads or writes anything on the
topic document. Kept as its own service rather than folded into
`TranscriptPipelineService`, whose constructor already threads through five
pipeline collaborators for a genuinely different concern (the transcript
state machine) - this one only ever needs a topic lookup and the reviewer.
"""

from typing import Optional

from app.core.ai_cost_control import (
    claim_generation,
    compute_generation_key,
    complete_generation,
    fail_generation,
)
from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.transcript_english_review import (
    MODEL,
    TranscriptEnglishReviewer,
    TranscriptReviewError,
    TranscriptReviewResult,
    get_english_reviewer,
)
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import TranscriptState
from app.repositories.topic_content_repository import TopicContentRepository


class TranscriptReviewService:
    def __init__(self, topic_repository: TopicContentRepository, reviewer: TranscriptEnglishReviewer) -> None:
        self.topic_repository = topic_repository
        self.reviewer = reviewer

    async def review_english(
        self, topic_id: str, transcript: str, requested_by_id: Optional[str] = None
    ) -> TranscriptReviewResult:
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

        # Purely advisory and never persisted (see this module's own
        # docstring) - blocks only a genuinely concurrent duplicate request
        # (see `claim_generation`'s own docstring); a later, sequential
        # re-review of the same text is always allowed.
        generation_key = compute_generation_key(
            AIGenerationType.TRANSCRIPT_ENGLISH_REVIEW,
            topic_id=topic_id,
            transcript=transcript,
            model=MODEL,
        )
        claim = await claim_generation(
            generation_type=AIGenerationType.TRANSCRIPT_ENGLISH_REVIEW,
            generation_key=generation_key,
            model=MODEL,
            topic_id=topic_id,
            requested_by_id=requested_by_id,
        )

        try:
            result = await self.reviewer.review(transcript)
        except TranscriptReviewError as exc:
            await fail_generation(claim, exc)
            raise
        usage = getattr(self.reviewer, "last_usage", None)
        await complete_generation(
            claim,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        return result


def get_transcript_review_service() -> TranscriptReviewService:
    return TranscriptReviewService(TopicContentRepository(), get_english_reviewer())
