"""Shared, server-side Interactive Video readiness validation."""

from app.core.exceptions import UnprocessableException
from app.models.topic_content import (
    CheckpointKind,
    ExerciseKind,
    InteractiveVideoCheckpoint,
    InteractiveVideoCompletionRule,
    LessonExercise,
    TopicContent,
)


ANSWERABLE_KINDS = frozenset({
    ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.IMAGE_COMPLETION,
    ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING,
    ExerciseKind.GAME, ExerciseKind.WRITING, ExerciseKind.REFLECTION,
    ExerciseKind.WORD_ORDER, ExerciseKind.ERROR_CORRECTION,
    ExerciseKind.DIALOGUE_COMPLETION,
})
AUTO_GRADED_KINDS = ANSWERABLE_KINDS - {ExerciseKind.WRITING, ExerciseKind.REFLECTION}


def validate_interactive_video_timeline(
    checkpoints: list[InteractiveVideoCheckpoint], exercises: list[LessonExercise], *, duration_seconds: float | None = None
) -> None:
    if not checkpoints:
        raise UnprocessableException("Add at least one video checkpoint before publishing this lesson")
    last_timestamp = -1.0
    seen_timestamps: set[float] = set()
    seen_exercises: set[int] = set()
    for checkpoint in checkpoints:
        timestamp = checkpoint.timestamp_seconds
        if duration_seconds is not None and timestamp >= duration_seconds:
            raise UnprocessableException("Every checkpoint must be before the end of the video")
        if timestamp <= last_timestamp:
            raise UnprocessableException("Checkpoint times must be in chronological order")
        if round(timestamp, 3) in seen_timestamps:
            raise UnprocessableException("Two checkpoints cannot pause at the same time")

        if checkpoint.kind == CheckpointKind.PRACTICE_WITH_MARIAM:
            if checkpoint.exercise_index is not None:
                raise UnprocessableException(
                    f"Checkpoint {checkpoint.id} at {timestamp}s is Practice with Mariam and cannot also "
                    "reference a lesson exercise"
                )
            if not (checkpoint.conversation_goal or "").strip():
                raise UnprocessableException(
                    f"Checkpoint {checkpoint.id} at {timestamp}s needs a conversation goal before it can be saved"
                )
            if not (checkpoint.opening_line or "").strip():
                raise UnprocessableException(
                    f"Checkpoint {checkpoint.id} at {timestamp}s needs an opening line before it can be saved"
                )
            # `session_duration_minutes` bounds are already pydantic-enforced
            # (ge=1, le=10); completion_rule is not applicable to this kind.
        else:
            if checkpoint.exercise_index is None:
                raise UnprocessableException(
                    f"Checkpoint {checkpoint.id} at {timestamp}s must reference a lesson exercise"
                )
            if checkpoint.exercise_index >= len(exercises):
                raise UnprocessableException("Each checkpoint must reference an existing lesson exercise")
            if checkpoint.exercise_index in seen_exercises:
                raise UnprocessableException("Each exercise can be used by only one video checkpoint")
            exercise = exercises[checkpoint.exercise_index]
            if exercise.kind not in ANSWERABLE_KINDS:
                raise UnprocessableException("This exercise type cannot unlock an Interactive Video checkpoint")
            if checkpoint.completion_rule == InteractiveVideoCompletionRule.CORRECT:
                if exercise.kind not in AUTO_GRADED_KINDS:
                    raise UnprocessableException("Correct-answer checkpoints require an automatically graded exercise")
                if not exercise.allow_resubmit:
                    raise UnprocessableException("Correct-answer checkpoints require an exercise that allows retries")
            seen_exercises.add(checkpoint.exercise_index)
        last_timestamp = timestamp
        seen_timestamps.add(round(timestamp, 3))


def validate_interactive_video_ready(topic: TopicContent) -> None:
    if not topic.uploaded_video_url:
        raise UnprocessableException("Interactive Video requires a teacher-uploaded video. YouTube is not supported yet.")
    if topic.learning_objectives and not topic.objectives_approved:
        raise UnprocessableException("Approve learning objectives before publishing this Interactive Video lesson")
    validate_interactive_video_timeline(topic.interactive_video_checkpoints, topic.lesson_exercises)
