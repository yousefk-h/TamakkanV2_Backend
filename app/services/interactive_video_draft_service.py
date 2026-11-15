"""Cost-controlled, non-destructive Interactive Video timeline proposals."""

from app.core.ai_cost_control import claim_generation, complete_generation, compute_generation_key, fail_generation
from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableException, UnprocessableException, UpstreamServiceException
from app.core.interactive_video import validate_interactive_video_timeline
from app.core.interactive_video_drafting import (
    MODEL, InteractiveVideoDrafter, InteractiveVideoDraftingError,
    InteractiveVideoDraftingNotConfigured, TimelineProposal,
    get_interactive_video_drafter,
)
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import InteractiveVideoCheckpoint, InteractiveVideoTimelineSuggestion, LessonExercise, LessonFlow
from app.models.user import User
from app.schema.topic import LessonExerciseIn
from app.services.topic_content_service import TopicContentService, get_topic_content_service


class InteractiveVideoDraftService:
    def __init__(self, topics: TopicContentService, drafter: InteractiveVideoDrafter) -> None:
        self.topics = topics
        self.drafter = drafter

    async def draft(self, topic_id: str, teacher: User, refresh: bool = False) -> list[InteractiveVideoTimelineSuggestion]:
        topic = await self.topics.get_own_topic(topic_id, teacher)
        if topic.lesson_flow != LessonFlow.INTERACTIVE_VIDEO or not topic.uploaded_video_url:
            raise UnprocessableException("Interactive Video suggestions require a teacher-uploaded Interactive Video lesson")
        if topic.learning_objectives and not topic.objectives_approved:
            raise UnprocessableException("Approve learning objectives before requesting timeline suggestions")
        timestamped = [segment for segment in topic.raw_transcript if segment.duration > 0 or segment.start > 0]
        if not timestamped or len((topic.approved_transcript or "").strip()) < 100:
            raise UnprocessableException("Approve timestamped video source material before requesting timeline suggestions")
        source = "\n".join(f"[{segment.start:.1f}s-{segment.start + segment.duration:.1f}s] {segment.text}" for segment in timestamped)
        source = source[:get_settings().MAX_DRAFTING_SOURCE_CHARS]
        exercises = [f"{index}: {exercise.kind.value} — {exercise.prompt}" for index, exercise in enumerate(topic.lesson_exercises)]
        objectives = [objective.text for objective in topic.learning_objectives]
        generation_key = compute_generation_key(
            AIGenerationType.INTERACTIVE_VIDEO_TIMELINE_DRAFT, topic_id=topic_id, title=topic.title,
            source=source, objectives=objectives, exercises=exercises, existing=[item.model_dump(mode="json") for item in topic.interactive_video_checkpoints], model=MODEL,
        )
        if not refresh and topic.interactive_video_draft_generation_key == generation_key and topic.interactive_video_drafts:
            return topic.interactive_video_drafts
        claim = await claim_generation(generation_type=AIGenerationType.INTERACTIVE_VIDEO_TIMELINE_DRAFT, generation_key=generation_key, model=MODEL, topic_id=topic_id, requested_by_id=teacher.id)
        try:
            proposed = await self.drafter.draft(title=topic.title, level=topic.level, timestamped_source=source, objectives=objectives, exercises=exercises)
            max_time = max(segment.start + segment.duration for segment in timestamped)
            drafts: list[InteractiveVideoTimelineSuggestion] = []
            accepted = list(topic.interactive_video_checkpoints)
            draft_exercises: list[LessonExercise] = []
            for proposal in proposed:
                exercise_index = proposal.exercise_index
                exercise: LessonExercise | None = None
                if proposal.exercise is not None:
                    try:
                        exercise = LessonExerciseIn.model_validate(proposal.exercise)
                    except Exception:
                        continue
                    exercise_index = len(topic.lesson_exercises) + len(draft_exercises)
                if exercise_index is None:
                    continue
                candidate = InteractiveVideoCheckpoint(
                    timestamp_seconds=proposal.timestamp_seconds,
                    exercise_index=exercise_index,
                    completion_rule=proposal.completion_rule,
                )
                try:
                    validate_interactive_video_timeline(
                        sorted([*accepted, candidate], key=lambda item: item.timestamp_seconds),
                        [*topic.lesson_exercises, *draft_exercises, *([exercise] if exercise else [])],
                        duration_seconds=max_time,
                    )
                except UnprocessableException:
                    continue
                accepted.append(candidate)
                if exercise:
                    draft_exercises.append(exercise)
                drafts.append(InteractiveVideoTimelineSuggestion(
                    **candidate.model_dump(), rationale=proposal.rationale.strip(), exercise=exercise,
                ))
            if not drafts:
                raise InteractiveVideoDraftingError("Provider returned no valid checkpoint suggestions")
        except InteractiveVideoDraftingNotConfigured as exc:
            await fail_generation(claim, exc)
            raise ServiceUnavailableException("AI timeline suggestions are not configured in this development environment") from exc
        except InteractiveVideoDraftingError as exc:
            await fail_generation(claim, exc)
            raise UpstreamServiceException("AI timeline suggestions could not be generated. Edit the timeline manually or try again.") from exc
        usage = getattr(self.drafter, "last_usage", None)
        await complete_generation(claim, input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None))
        await self.topics.topic_repository.update_fields(topic_id, {
            "interactive_video_drafts": [item.model_dump() for item in drafts],
            "interactive_video_draft_generation_key": generation_key,
        })
        return drafts


def get_interactive_video_draft_service() -> InteractiveVideoDraftService:
    return InteractiveVideoDraftService(get_topic_content_service(), get_interactive_video_drafter())
