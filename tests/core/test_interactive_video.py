"""Model/validation coverage for the Practice with Mariam checkpoint kind
(`app.models.topic_content.CheckpointKind.PRACTICE_WITH_MARIAM`) added
alongside the pre-existing exercise checkpoint kind - see
`app.core.interactive_video.validate_interactive_video_timeline`."""

import pytest

from app.core.exceptions import UnprocessableException
from app.core.interactive_video import validate_interactive_video_timeline
from app.models.topic_content import (
    CheckpointKind,
    ExerciseKind,
    InteractiveVideoCheckpoint,
    LessonExercise,
)


def _exercise(kind=ExerciseKind.MULTIPLE_CHOICE, allow_resubmit=True):
    return LessonExercise(kind=kind, prompt="Pick one", options=["a", "b"], correct_option=0, allow_resubmit=allow_resubmit)


def test_a_legacy_checkpoint_built_without_kind_defaults_to_exercise_and_still_validates():
    """Backward compatibility: a pre-existing stored checkpoint (or one built
    the old way in code/tests) never set `kind` - it must still construct and
    validate exactly as before."""
    checkpoint = InteractiveVideoCheckpoint(timestamp_seconds=5, exercise_index=0)
    assert checkpoint.kind == CheckpointKind.EXERCISE
    validate_interactive_video_timeline([checkpoint], [_exercise()])  # must not raise


def test_practice_with_mariam_checkpoint_requires_a_conversation_goal():
    checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        opening_line="Hi! Let's talk about your weekend.",
    )
    with pytest.raises(UnprocessableException):
        validate_interactive_video_timeline([checkpoint], [])


def test_practice_with_mariam_checkpoint_requires_an_opening_line():
    checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        conversation_goal="Practice describing weekend plans.",
    )
    with pytest.raises(UnprocessableException):
        validate_interactive_video_timeline([checkpoint], [])


def test_practice_with_mariam_checkpoint_rejects_an_exercise_index():
    checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        exercise_index=0, conversation_goal="Practice describing weekend plans.",
        opening_line="Hi! Let's talk about your weekend.",
    )
    with pytest.raises(UnprocessableException):
        validate_interactive_video_timeline([checkpoint], [_exercise()])


def test_a_well_formed_practice_with_mariam_checkpoint_coexists_with_exercise_checkpoints():
    exercise_checkpoint = InteractiveVideoCheckpoint(timestamp_seconds=5, exercise_index=0)
    practice_checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=15, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        learning_objective="Describe weekend plans.",
        conversation_goal="Practice describing weekend plans with Mariam.",
        opening_line="Hi! What are your plans this weekend?",
        session_duration_minutes=3,
    )
    validate_interactive_video_timeline(
        [exercise_checkpoint, practice_checkpoint], [_exercise()], duration_seconds=30,
    )  # must not raise


def test_practice_with_mariam_checkpoints_still_enforce_chronological_unique_timestamps():
    first = InteractiveVideoCheckpoint(
        timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        conversation_goal="Goal", opening_line="Hi",
    )
    second = InteractiveVideoCheckpoint(
        timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        conversation_goal="Goal", opening_line="Hi",
    )
    with pytest.raises(UnprocessableException):
        validate_interactive_video_timeline([first, second], [])


def test_practice_with_mariam_checkpoint_must_be_before_the_end_of_the_video():
    checkpoint = InteractiveVideoCheckpoint(
        timestamp_seconds=40, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
        conversation_goal="Goal", opening_line="Hi",
    )
    with pytest.raises(UnprocessableException):
        validate_interactive_video_timeline([checkpoint], [], duration_seconds=30)


def test_session_duration_minutes_bounds_are_pydantic_enforced():
    with pytest.raises(Exception):
        InteractiveVideoCheckpoint(
            timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
            conversation_goal="Goal", opening_line="Hi", session_duration_minutes=11,
        )
    with pytest.raises(Exception):
        InteractiveVideoCheckpoint(
            timestamp_seconds=5, kind=CheckpointKind.PRACTICE_WITH_MARIAM,
            conversation_goal="Goal", opening_line="Hi", session_duration_minutes=0,
        )
