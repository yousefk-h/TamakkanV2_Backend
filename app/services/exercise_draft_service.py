"""Read the approved source, generate suggestions, and keep teacher approval explicit."""

import time

from loguru import logger
from pydantic import BaseModel, Field

from app.core.ai_cost_control import (
    claim_generation,
    compute_generation_key,
    complete_generation,
    fail_generation,
)
from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableException, UnprocessableException, UpstreamServiceException
from app.core.exercise_drafting import ExerciseDrafter, ExerciseDraftingError, ExerciseDraftingNotConfigured, GENERATABLE_KINDS, MODEL, get_exercise_drafter
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import ExerciseKind
from app.models.user import User
from app.schema.topic import LessonExerciseIn
from app.services.topic_content_service import TopicContentService, get_topic_content_service


class DraftExercisesRequest(BaseModel):
    kind: ExerciseKind
    count: int = Field(default=1, ge=1, le=5)
    focus: str = Field(default="", max_length=500)
    content: str = Field(default="", max_length=5000)


class DraftExercisesResponse(BaseModel):
    exercises: list[LessonExerciseIn]
    source: str = "approved_transcript"
    model: str = MODEL


class ExerciseDraftService:
    def __init__(self, topics: TopicContentService, drafter: ExerciseDrafter) -> None:
        self.topics = topics
        self.drafter = drafter

    async def draft(self, topic_id: str, teacher: User, body: DraftExercisesRequest) -> DraftExercisesResponse:
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if body.kind not in GENERATABLE_KINDS:
            raise UnprocessableException("This skill uses teacher media or the existing vocabulary drafting flow")
        source = (topic.approved_transcript or "").strip()
        if len(source) < 100:
            raise UnprocessableException("Approve at least 100 characters of lesson source text before generating exercises")
        # Same cost-bounding cap used for content drafting (Settings
        # .MAX_DRAFTING_SOURCE_CHARS), not the older hardcoded 16000 - one
        # configurable ceiling for every drafting call site.
        bounded_source = source[: get_settings().MAX_DRAFTING_SOURCE_CHARS]

        generation_key = compute_generation_key(
            AIGenerationType.EXERCISE_DRAFT,
            topic_id=topic_id,
            kind=body.kind.value,
            count=body.count,
            focus=body.focus,
            content=body.content,
            source=bounded_source,
            model=MODEL,
        )
        # Blocks only a genuinely concurrent duplicate (a double-click, a
        # retried request that reached the server twice) - see
        # `app.core.ai_cost_control.claim_generation`'s own docstring on why
        # a later, sequential identical request is never blocked: nothing
        # from a draft call is persisted anywhere (the teacher still
        # reviews/accepts), so there is no stale cached result to return
        # instead, and "draft this same kind again" is a legitimate,
        # explicit teacher action, not waste.
        claim = await claim_generation(
            generation_type=AIGenerationType.EXERCISE_DRAFT,
            generation_key=generation_key,
            model=MODEL,
            topic_id=topic_id,
            requested_by_id=teacher.id,
        )

        started = time.perf_counter()
        try:
            exercises = await self.drafter.draft(title=topic.title, level=topic.level, source=bounded_source,
                                                  kind=body.kind, count=body.count, focus=body.focus,
                                                  content=body.content)
            if len(exercises) != body.count or any(item.kind != body.kind for item in exercises):
                raise ExerciseDraftingError("Provider returned the wrong exercise type or count")
            if body.content and body.kind in {ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.SERIES}:
                exercises = [item.model_copy(update={"content": body.content}) for item in exercises]
            usage = getattr(self.drafter, "last_usage", None)
            await complete_generation(
                claim,
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
            logger.info("exercise_draft success topic_id={} kind={} count={} latency_ms={:.0f} model={}",
                        topic_id, body.kind.value, len(exercises), (time.perf_counter() - started) * 1000, MODEL)
            return DraftExercisesResponse(exercises=exercises)
        except ExerciseDraftingNotConfigured as exc:
            await fail_generation(claim, exc)
            raise ServiceUnavailableException("AI exercise drafting is not configured in this development environment") from exc
        except ExerciseDraftingError as exc:
            await fail_generation(claim, exc)
            logger.warning("exercise_draft failure topic_id={} kind={} latency_ms={:.0f} error={}",
                           topic_id, body.kind.value, (time.perf_counter() - started) * 1000, type(exc).__name__)
            raise UpstreamServiceException("AI could not draft this skill. Try again or write it manually.") from exc


def get_exercise_draft_service() -> ExerciseDraftService:
    return ExerciseDraftService(get_topic_content_service(), get_exercise_drafter())
