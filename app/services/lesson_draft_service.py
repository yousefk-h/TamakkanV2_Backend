"""Slice C: a single "Generate Lesson Draft" teacher action that orchestrates
the existing content-drafting and exercise-drafting pipelines into one
bounded, progressively-visible job, instead of a teacher clicking "AI draft"
separately for the avatar prompt and for every exercise card.

Deliberately NOT a new generation mechanism: every actual provider call this
orchestrates reuses the exact same Slice B-guarded call sites that already
exist (`TranscriptPipelineService.run_content_drafting`,
`app.core.exercise_drafting.get_exercise_drafter()` via the same
claim_generation/complete_generation/fail_generation/enforce_topic_budget
functions `ExerciseDraftService` itself uses) - this module only decides
*which* calls to make and in what order, and persists partial progress
after each one so a later failure never loses earlier work.

AI-suggested exercises land in `TopicContent.draft_exercises`, never
directly in `lesson_exercises` (the published/saved list) - a teacher must
explicitly accept each one (`accept_suggestion`) before it becomes part of
the real lesson. Content fields (avatar_prompt/opening_line/warm_up_questions/
vocabulary) go through the existing "fill only what's empty, never overwrite
a teacher's own edit" `run_content_drafting` unchanged.
"""

from typing import Optional

from loguru import logger

from app.core.ai_cost_control import (
    GenerationBudgetExceeded,
    claim_generation,
    compute_generation_key,
    complete_generation,
    fail_generation,
    remaining_topic_budget,
)
from app.core.config import get_settings
from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.exercise_drafting import (
    MODEL as EXERCISE_DRAFTING_MODEL,
    ExerciseDrafter,
    ExerciseDraftingError,
    ExerciseDraftingNotConfigured,
    get_exercise_drafter,
)
from app.core.exercise_quality import existing_exercises_for_duplicate_check, validate_exercise_suggestion
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import (
    ExerciseKind,
    LessonDraftExerciseSuggestion,
    LessonDraftJob,
    LessonDraftStepState,
    TopicContent,
)
from app.models.user import User
from app.repositories.topic_content_repository import TopicContentRepository
from app.services.transcript_pipeline_service import (
    MIN_SOURCE_CHARS_FOR_DRAFTING,
    NOT_ENOUGH_SOURCE_MESSAGE,
    TranscriptPipelineService,
    get_transcript_pipeline_service,
)
from app.services.topic_content_service import TopicContentService, get_topic_content_service
from app.utils.time import utcnow

# A small, bounded "starter pack" rather than every possible exercise kind -
# predictable cost, always exactly 3 kinds. A teacher who wants more uses the
# existing per-card "AI draft" action (ExerciseDraftService), which this
# slice leaves untouched.
DEFAULT_DRAFT_EXERCISE_KINDS: list[ExerciseKind] = [
    ExerciseKind.MULTIPLE_CHOICE,
    ExerciseKind.READING,
    ExerciseKind.GRAMMAR,
]

# Every kind this bundle may ever choose from, most-general first - the
# fallback order when a lesson has no approved objectives yet (or none of
# them match a skill below) is exactly `DEFAULT_DRAFT_EXERCISE_KINDS`.
_BALANCED_KIND_POOL: list[ExerciseKind] = [
    ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.READING, ExerciseKind.GRAMMAR,
    ExerciseKind.LISTENING, ExerciseKind.WORD_ORDER, ExerciseKind.ERROR_CORRECTION,
    ExerciseKind.DIALOGUE_COMPLETION,
]
# A small, plain keyword vocabulary per skill - not a classifier, just enough
# to notice when a Teacher's own approved objective already names a skill
# (e.g. "listen", "pronunciation") this bundle could usefully cover, so the
# 3 chosen kinds lean toward what THIS lesson is actually for rather than
# always being the same 3 regardless of objective. Kinds absent here
# (MULTIPLE_CHOICE) are the general-purpose fallback, never prioritized by
# keyword match alone.
_SKILL_KEYWORDS: dict[ExerciseKind, frozenset[str]] = {
    ExerciseKind.LISTENING: frozenset({"listen", "hear", "pronunciation", "audio"}),
    ExerciseKind.DIALOGUE_COMPLETION: frozenset({"conversation", "dialogue", "discuss", "respond", "ask"}),
    ExerciseKind.ERROR_CORRECTION: frozenset({"correct", "accuracy", "mistake"}),
    ExerciseKind.WORD_ORDER: frozenset({"sentence", "structure", "word order"}),
    ExerciseKind.READING: frozenset({"read", "passage", "comprehen"}),
    ExerciseKind.GRAMMAR: frozenset({"grammar", "tense", "rule"}),
}


def _balanced_default_kinds(topic: TopicContent) -> list[ExerciseKind]:
    """The 3 kinds this run targets, same count every time (never padded to
    backfill a skipped already-authored one - see `_plan`): the stable
    default bundle when there are no approved objectives yet to go on,
    otherwise the 3 kinds from `_BALANCED_KIND_POOL` whose skill keywords
    best match the lesson's own approved objectives, covering more of
    reading/listening/grammar/production/conversation than a fixed MC-heavy
    default would."""
    objective_text = " ".join(o.text.lower() for o in topic.learning_objectives if o.text)
    if not objective_text:
        return list(DEFAULT_DRAFT_EXERCISE_KINDS)
    matched = [
        kind for kind in _BALANCED_KIND_POOL
        if kind in _SKILL_KEYWORDS and any(word in objective_text for word in _SKILL_KEYWORDS[kind])
    ]
    rest = [kind for kind in _BALANCED_KIND_POOL if kind not in matched]
    return (matched + rest)[:3]

# One bounded retry when a drafted exercise fails the quality gate - never
# unbounded, and the retry is itself a normal cost-tracked/budget-checked
# generation, not a free extra call. Same constant/reasoning as
# `exercise_draft_service.MAX_DRAFT_ATTEMPTS`.
MAX_EXERCISE_DRAFT_ATTEMPTS = 2


def _content_already_filled(topic: TopicContent) -> bool:
    return bool(topic.vocabulary and topic.avatar_prompt and topic.opening_line and topic.warm_up_questions)


def _existing_kinds(topic: TopicContent) -> set[ExerciseKind]:
    return {item.kind for item in topic.lesson_exercises} | {item.kind for item in topic.draft_exercises}


class LessonDraftService:
    def __init__(
        self,
        topic_repository: TopicContentRepository,
        topics: TopicContentService,
        transcript_pipeline: TranscriptPipelineService,
        exercise_drafter: ExerciseDrafter,
    ) -> None:
        self.topic_repository = topic_repository
        self.topics = topics
        self.transcript_pipeline = transcript_pipeline
        self.exercise_drafter = exercise_drafter

    def _plan(self, topic: TopicContent) -> tuple[bool, list[ExerciseKind]]:
        """What this run would actually attempt: whether content drafting has
        anything left to fill, and which of the default exercise kinds
        aren't already present as a saved OR a pending-suggestion exercise
        (avoiding duplicate suggestions - a teacher who already has, or
        already asked for, a multiple_choice exercise doesn't get a second
        one from this action).
        """
        needs_content = not _content_already_filled(topic)
        existing = _existing_kinds(topic)
        needs_exercise_kinds = [kind for kind in _balanced_default_kinds(topic) if kind not in existing]
        return needs_content, needs_exercise_kinds

    async def start(self, topic_id: str, teacher: User) -> TopicContent:
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if topic.learning_objectives and not topic.objectives_approved:
            raise UnprocessableException("Review and approve learning objectives before generating lesson content")
        if not topic.approved_transcript or len(topic.approved_transcript.strip()) < MIN_SOURCE_CHARS_FOR_DRAFTING:
            raise UnprocessableException(NOT_ENOUGH_SOURCE_MESSAGE)
        if topic.lesson_draft_job is not None and topic.lesson_draft_job.state == LessonDraftStepState.RUNNING:
            raise ConflictException("A lesson draft is already being generated for this topic.")

        needs_content, needs_exercise_kinds = self._plan(topic)
        planned_calls = (1 if needs_content else 0) + len(needs_exercise_kinds)
        if planned_calls == 0:
            raise UnprocessableException(
                "There is nothing left to draft - every supported field and the default exercise kinds are "
                "already filled. Use the per-section regenerate actions to replace something specific."
            )

        # Checked up front, before any provider call - a bulk action must
        # never start work it cannot finish within the topic's remaining
        # daily budget (distinct from the per-call enforce_topic_budget
        # each individual claim still performs as its own, final guard).
        remaining = await remaining_topic_budget(topic_id)
        if planned_calls > remaining:
            raise GenerationBudgetExceeded(
                f"Generating this lesson draft needs up to {planned_calls} AI requests, but this lesson has only "
                f"{remaining} left in its daily limit. Approve/reject some pending suggestions, wait for the daily "
                "limit to reset, or ask an administrator to raise it."
            )

        job = LessonDraftJob(
            state=LessonDraftStepState.RUNNING,
            content_step=LessonDraftStepState.PENDING if needs_content else LessonDraftStepState.SKIPPED,
            exercise_steps={kind.value: LessonDraftStepState.PENDING for kind in needs_exercise_kinds},
            planned_calls=planned_calls,
            started_at=utcnow(),
        )
        await self.topic_repository.update_fields(topic_id, {"lesson_draft_job": job.model_dump()})
        return await self._get_or_404(topic_id)

    async def run(self, topic_id: str, requested_by_id: Optional[str] = None) -> None:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None or topic.lesson_draft_job is None:
            return  # deleted, or start() was never actually called
        needs_content, needs_exercise_kinds = self._plan(topic)

        if needs_content:
            await self._set_job_field(topic_id, content_step=LessonDraftStepState.RUNNING)
            try:
                await self.transcript_pipeline.run_content_drafting(topic_id, requested_by_id)
                await self._set_job_field(topic_id, content_step=LessonDraftStepState.DONE)
            except Exception as exc:
                # Best-effort, same as the underlying pipeline's own
                # reasoning: a failed content draft doesn't abort the rest
                # of this job - the teacher can still author it by hand,
                # and exercise drafting below is independent of it.
                logger.opt(exception=True).warning("Lesson draft content step failed for topic {}: {}", topic_id, exc)
                await self._set_job_field(topic_id, content_step=LessonDraftStepState.FAILED)

        for kind in needs_exercise_kinds:
            await self._set_job_field(topic_id, exercise_kind=kind, state=LessonDraftStepState.RUNNING)
            try:
                suggestion = await self._draft_one_exercise(topic_id, kind, requested_by_id)
                await self._append_draft_exercise(topic_id, suggestion)
                await self._set_job_field(topic_id, exercise_kind=kind, state=LessonDraftStepState.DONE)
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "Lesson draft exercise step failed for topic {} kind {}: {}", topic_id, kind.value, exc
                )
                await self._set_job_field(topic_id, exercise_kind=kind, state=LessonDraftStepState.FAILED)

        final = await self.topic_repository.get_by_id(topic_id)
        if final is None or final.lesson_draft_job is None:
            return
        any_failed = final.lesson_draft_job.content_step == LessonDraftStepState.FAILED or any(
            state == LessonDraftStepState.FAILED for state in final.lesson_draft_job.exercise_steps.values()
        )
        await self.topic_repository.update_fields(topic_id, {
            "lesson_draft_job": {
                **final.lesson_draft_job.model_dump(),
                "state": LessonDraftStepState.FAILED if any_failed else LessonDraftStepState.DONE,
                "completed_at": utcnow(),
            },
        })

    async def _draft_one_exercise(
        self, topic_id: str, kind: ExerciseKind, requested_by_id: Optional[str],
        exclude_draft_index: Optional[int] = None,
    ) -> LessonDraftExerciseSuggestion:
        """Mirrors `ExerciseDraftService.draft`'s own cost-control wiring
        exactly (same claim/complete/fail calls, same source bound) - kept
        as its own method rather than calling that service directly because
        this runs in a background task with only a topic id and a bare
        `requested_by_id` string, not the original request's `User`/
        ownership context (already verified once, in `start()`).
        """
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None or not topic.approved_transcript:
            raise NotFoundException("Topic not found")
        if topic.learning_objectives and not topic.objectives_approved:
            raise UnprocessableException("Review and approve learning objectives before generating lesson content")
        source = topic.approved_transcript.strip()[: get_settings().MAX_DRAFTING_SOURCE_CHARS]
        objective_texts = [o.text for o in topic.learning_objectives]
        # A regenerate replaces exactly one pending suggestion - it must not
        # be treated as a duplicate of the very thing it's about to replace.
        topic_for_siblings = topic
        if exclude_draft_index is not None:
            topic_for_siblings = topic.model_copy(update={
                "draft_exercises": [item for i, item in enumerate(topic.draft_exercises) if i != exclude_draft_index],
            })
        siblings = existing_exercises_for_duplicate_check(topic_for_siblings)

        last_issues: list[str] = []
        for attempt in range(1, MAX_EXERCISE_DRAFT_ATTEMPTS + 1):
            generation_key = compute_generation_key(
                AIGenerationType.EXERCISE_DRAFT,
                topic_id=topic_id,
                kind=kind.value,
                count=1,
                focus="",
                content="",
                source=source,
                model=EXERCISE_DRAFTING_MODEL,
                **({"attempt": attempt} if attempt > 1 else {}),
            )
            claim = await claim_generation(
                generation_type=AIGenerationType.EXERCISE_DRAFT,
                generation_key=generation_key,
                model=EXERCISE_DRAFTING_MODEL,
                topic_id=topic_id,
                requested_by_id=requested_by_id,
            )
            retry_focus = "" if attempt == 1 else (
                f"Previous attempt was rejected for: {'; '.join(last_issues)}. "
                "Write a clearly different, better-grounded question this time."
            )
            try:
                exercises = await self.exercise_drafter.draft(
                    title=topic.title, level=topic.level, source=source, kind=kind, count=1,
                    focus=retry_focus, content="",
                )
                if len(exercises) != 1 or exercises[0].kind != kind:
                    raise ExerciseDraftingError("Provider returned the wrong exercise type or count")
                issues = validate_exercise_suggestion(
                    exercises[0], source=source, objectives=objective_texts, level=topic.level, siblings=siblings,
                )
                if issues:
                    reasons = [f"{i.code}: {i.message}" for i in issues]
                    await fail_generation(claim, ExerciseDraftingError("; ".join(reasons)))
                    last_issues = reasons
                    if attempt < MAX_EXERCISE_DRAFT_ATTEMPTS:
                        continue
                    raise ExerciseDraftingError("; ".join(reasons))
            except ExerciseDraftingNotConfigured as exc:
                await fail_generation(claim, exc)
                raise
            except ExerciseDraftingError as exc:
                await fail_generation(claim, exc)
                if attempt < MAX_EXERCISE_DRAFT_ATTEMPTS:
                    last_issues = [str(exc)]
                    continue
                raise
            else:
                usage = getattr(self.exercise_drafter, "last_usage", None)
                await complete_generation(
                    claim, input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None)
                )
                exercise = exercises[0].model_dump()
                return LessonDraftExerciseSuggestion(kind=kind, exercise=exercise, state=LessonDraftStepState.DONE)
        # Unreachable: the loop above always returns or raises.
        raise ExerciseDraftingError("Exercise drafting exhausted its retry budget")

    async def _append_draft_exercise(self, topic_id: str, suggestion: LessonDraftExerciseSuggestion) -> None:
        current = await self.topic_repository.get_by_id(topic_id)
        if current is None:
            return
        drafts = [item.model_dump() for item in current.draft_exercises] + [suggestion.model_dump()]
        await self.topic_repository.update_fields(topic_id, {"draft_exercises": drafts})

    async def _set_job_field(
        self,
        topic_id: str,
        *,
        content_step: Optional[LessonDraftStepState] = None,
        exercise_kind: Optional[ExerciseKind] = None,
        state: Optional[LessonDraftStepState] = None,
    ) -> None:
        current = await self.topic_repository.get_by_id(topic_id)
        if current is None or current.lesson_draft_job is None:
            return
        job = current.lesson_draft_job.model_dump()
        if content_step is not None:
            job["content_step"] = content_step
        if exercise_kind is not None and state is not None:
            job["exercise_steps"][exercise_kind.value] = state
        await self.topic_repository.update_fields(topic_id, {"lesson_draft_job": job})

    async def accept_suggestion(self, topic_id: str, index: int, teacher: User) -> TopicContent:
        """Moves exactly one pending suggestion into the real, published
        `lesson_exercises` list - never auto-applied, and never touches any
        other suggestion or any manually-authored exercise."""
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if index < 0 or index >= len(topic.draft_exercises):
            raise NotFoundException("No such lesson draft suggestion")
        suggestion = topic.draft_exercises[index]
        remaining_drafts = [item.model_dump() for i, item in enumerate(topic.draft_exercises) if i != index]
        lesson_exercises = [item.model_dump() for item in topic.lesson_exercises] + [suggestion.exercise.model_dump()]
        await self.topic_repository.update_fields(
            topic_id, {"draft_exercises": remaining_drafts, "lesson_exercises": lesson_exercises}
        )
        return await self._get_or_404(topic_id)

    async def reject_suggestion(self, topic_id: str, index: int, teacher: User) -> TopicContent:
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if index < 0 or index >= len(topic.draft_exercises):
            raise NotFoundException("No such lesson draft suggestion")
        remaining_drafts = [item.model_dump() for i, item in enumerate(topic.draft_exercises) if i != index]
        await self.topic_repository.update_fields(topic_id, {"draft_exercises": remaining_drafts})
        return await self._get_or_404(topic_id)

    async def regenerate_suggestion(self, topic_id: str, index: int, teacher: User) -> TopicContent:
        """Replaces exactly one pending suggestion with a fresh draft of the
        SAME kind - never touches any other suggestion, any manually-
        authored exercise, or any already-accepted/published exercise. Goes
        through the identical cost-control path as the initial draft (its
        own concurrent-duplicate guard and daily budget check)."""
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if index < 0 or index >= len(topic.draft_exercises):
            raise NotFoundException("No such lesson draft suggestion")
        kind = topic.draft_exercises[index].kind
        fresh = await self._draft_one_exercise(topic_id, kind, teacher.id, exclude_draft_index=index)
        current = await self.topic_repository.get_by_id(topic_id)
        drafts = [item.model_dump() for item in current.draft_exercises]
        drafts[index] = fresh.model_dump()
        await self.topic_repository.update_fields(topic_id, {"draft_exercises": drafts})
        return await self._get_or_404(topic_id)

    async def _get_or_404(self, topic_id: str) -> TopicContent:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        return topic


def get_lesson_draft_service() -> LessonDraftService:
    return LessonDraftService(
        TopicContentRepository(), get_topic_content_service(), get_transcript_pipeline_service(), get_exercise_drafter()
    )
