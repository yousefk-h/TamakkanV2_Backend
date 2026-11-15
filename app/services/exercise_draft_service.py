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
from app.core.exercise_quality import existing_exercises_for_duplicate_check, validate_exercise_suggestion
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import ExerciseKind
from app.models.user import User
from app.schema.topic import LessonExerciseIn
from app.services.topic_content_service import TopicContentService, get_topic_content_service

# One bounded retry when the first batch fails quality validation - never
# unbounded, and the retry is itself a normal cost-tracked/budget-checked
# generation (see `ai_cost_control.claim_generation`), not a free extra call.
MAX_DRAFT_ATTEMPTS = 2


def _sanitize_exc_message(exc: Exception, *, max_len: int = 200) -> str:
    """A short, safe-to-surface summary of a provider/schema exception.

    Provider exceptions shouldn't normally contain the configured Gemini API
    key, but this is defensive: redact it on sight before the message ever
    reaches a raised exception or a log line, then bound the length.
    """
    message = str(exc)
    api_key = get_settings().GEMINI_API_KEY
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = message.strip()
    if len(message) > max_len:
        message = message[:max_len].rstrip() + "..."
    return message


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
        if topic.learning_objectives and not topic.objectives_approved:
            raise UnprocessableException("Review and approve learning objectives before generating lesson content")
        if body.kind not in GENERATABLE_KINDS:
            raise UnprocessableException("This skill uses teacher media or the existing vocabulary drafting flow")
        source = (topic.approved_transcript or "").strip()
        if len(source) < 100:
            raise UnprocessableException("Approve at least 100 characters of lesson source text before generating exercises")
        # Same cost-bounding cap used for content drafting (Settings
        # .MAX_DRAFTING_SOURCE_CHARS), not the older hardcoded 16000 - one
        # configurable ceiling for every drafting call site.
        bounded_source = source[: get_settings().MAX_DRAFTING_SOURCE_CHARS]
        objective_texts = [o.text for o in topic.learning_objectives]
        siblings = existing_exercises_for_duplicate_check(topic)

        last_issues: list[str] = []
        for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
            # Each attempt is its own tracked generation - a retry is a real,
            # separate provider call, never a free re-ask, and still subject
            # to the same per-topic daily budget as any other request.
            generation_key = compute_generation_key(
                AIGenerationType.EXERCISE_DRAFT,
                topic_id=topic_id,
                kind=body.kind.value,
                count=body.count,
                focus=body.focus,
                content=body.content,
                source=bounded_source,
                model=MODEL,
                # Omitted on the first attempt so the key is byte-for-byte
                # the pre-Slice-4 one - an external caller computing the same
                # key to detect a genuinely concurrent duplicate (see
                # test_exercise_draft_rejects_a_genuinely_concurrent_duplicate_request)
                # must still match it.
                **({"attempt": attempt} if attempt > 1 else {}),
            )
            claim = await claim_generation(
                generation_type=AIGenerationType.EXERCISE_DRAFT,
                generation_key=generation_key,
                model=MODEL,
                topic_id=topic_id,
                requested_by_id=teacher.id,
            )
            started = time.perf_counter()
            retry_focus = body.focus if attempt == 1 else (
                f"{body.focus} (Previous attempt was rejected for: {'; '.join(last_issues)}. "
                "Write clearly different, better-grounded questions this time.)"
            ).strip()
            try:
                exercises = await self.drafter.draft(title=topic.title, level=topic.level, source=bounded_source,
                                                      kind=body.kind, count=body.count, focus=retry_focus,
                                                      content=body.content)
                if len(exercises) != body.count or any(item.kind != body.kind for item in exercises):
                    raise ExerciseDraftingError("Provider returned the wrong exercise type or count")
                if body.content and body.kind in {ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.SERIES}:
                    exercises = [item.model_copy(update={"content": body.content}) for item in exercises]

                batch_issues: list[str] = []
                validated_so_far: list = []
                for item in exercises:
                    issues = validate_exercise_suggestion(
                        item, source=bounded_source, objectives=objective_texts,
                        level=topic.level, siblings=siblings + validated_so_far,
                    )
                    if issues:
                        batch_issues.extend(f"{i.code}: {i.message}" for i in issues)
                    else:
                        validated_so_far.append(item)

                usage = getattr(self.drafter, "last_usage", None)
                if batch_issues:
                    await fail_generation(claim, ExerciseDraftingError("; ".join(batch_issues)))
                    last_issues = batch_issues
                    logger.warning(
                        "exercise_draft quality-rejected topic_id={} kind={} attempt={} issues={}",
                        topic_id, body.kind.value, attempt, batch_issues,
                    )
                    if attempt < MAX_DRAFT_ATTEMPTS:
                        continue
                    raise UpstreamServiceException(
                        "AI could not draft a high-quality exercise for this skill after a retry "
                        f"({'; '.join(batch_issues)}). Try again with a different focus, or write it manually."
                    )

                await complete_generation(
                    claim,
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                )
                logger.info("exercise_draft success topic_id={} kind={} count={} attempt={} latency_ms={:.0f} model={}",
                            topic_id, body.kind.value, len(exercises), attempt, (time.perf_counter() - started) * 1000, MODEL)
                return DraftExercisesResponse(exercises=exercises)
            except ExerciseDraftingNotConfigured as exc:
                await fail_generation(claim, exc)
                raise ServiceUnavailableException("AI exercise drafting is not configured in this development environment") from exc
            except ExerciseDraftingError as exc:
                await fail_generation(claim, exc)
                sanitized_msg = _sanitize_exc_message(exc)
                logger.warning(
                    "exercise_draft failure topic_id={} kind={} attempt={} latency_ms={:.0f} error={} - {}",
                    topic_id, body.kind.value, attempt, (time.perf_counter() - started) * 1000,
                    type(exc).__name__, sanitized_msg,
                )
                if attempt < MAX_DRAFT_ATTEMPTS:
                    last_issues = [str(exc)]
                    continue
                raise UpstreamServiceException(
                    f"AI could not draft this skill: {type(exc).__name__} - {sanitized_msg}. "
                    "Try again or write it manually."
                ) from exc
        # Unreachable: the loop above always returns or raises.
        raise UpstreamServiceException("AI could not draft this skill. Try again or write it manually.")


def get_exercise_draft_service() -> ExerciseDraftService:
    return ExerciseDraftService(get_topic_content_service(), get_exercise_drafter())
