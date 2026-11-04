"""Read the approved source, generate suggestions, and keep teacher approval explicit."""

import time

from loguru import logger
from pydantic import BaseModel, Field

from app.core.exceptions import ServiceUnavailableException, UnprocessableException, UpstreamServiceException
from app.core.exercise_drafting import ExerciseDrafter, ExerciseDraftingError, ExerciseDraftingNotConfigured, GENERATABLE_KINDS, MODEL, get_exercise_drafter
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
        started = time.perf_counter()
        try:
            exercises = await self.drafter.draft(title=topic.title, level=topic.level, source=source[:16000],
                                                  kind=body.kind, count=body.count, focus=body.focus,
                                                  content=body.content)
            if len(exercises) != body.count or any(item.kind != body.kind for item in exercises):
                raise ExerciseDraftingError("Provider returned the wrong exercise type or count")
            if body.content and body.kind in {ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.SERIES}:
                exercises = [item.model_copy(update={"content": body.content}) for item in exercises]
            logger.info("exercise_draft success topic_id={} kind={} count={} latency_ms={:.0f} model={}",
                        topic_id, body.kind.value, len(exercises), (time.perf_counter() - started) * 1000, MODEL)
            return DraftExercisesResponse(exercises=exercises)
        except ExerciseDraftingNotConfigured as exc:
            raise ServiceUnavailableException("AI exercise drafting is not configured in this development environment") from exc
        except ExerciseDraftingError as exc:
            logger.warning("exercise_draft failure topic_id={} kind={} latency_ms={:.0f} error={}",
                           topic_id, body.kind.value, (time.perf_counter() - started) * 1000, type(exc).__name__)
            raise UpstreamServiceException("AI could not draft this skill. Try again or write it manually.") from exc


def get_exercise_draft_service() -> ExerciseDraftService:
    return ExerciseDraftService(get_topic_content_service(), get_exercise_drafter())
