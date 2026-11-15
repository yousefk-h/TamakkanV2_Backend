import json

import pytest
from pydantic import ValidationError

from app.core.interactive_video_drafting import SYSTEM_PROMPT, TimelineExerciseProposal, TimelineProposal, TimelineProposals
from app.services.interactive_video_draft_service import _is_literal_recall


def test_timeline_provider_contract_has_no_open_ended_json_object():
    """Gemini native structured output cannot reliably accept dict[Any]."""
    schema = json.dumps(TimelineProposals.model_json_schema())
    assert "additionalProperties" not in schema


def test_timeline_activity_proposal_is_a_concrete_editable_exercise():
    proposal = TimelineExerciseProposal.model_validate({
        "kind": "writing",
        "prompt": "Write two sentences about your routine.",
        "min_words": 20,
    })
    assert proposal.kind == "writing"
    assert proposal.model_dump(exclude_none=True, exclude_defaults=True) == {
        "kind": "writing",
        "prompt": "Write two sentences about your routine.",
        "min_words": 20,
    }


def test_timeline_proposal_requires_a_learning_action_not_a_transcript_copy():
    useful = TimelineProposal.model_validate({
        "timestamp_seconds": 12, "rationale": "Students apply the greeting in a new workplace situation.",
        "learning_action": "apply", "learning_goal": "Choose an appropriate greeting for a new colleague.",
        "source_evidence": "The presenter models a formal greeting.",
        "exercise": {"kind": "multiple_choice", "prompt": "You meet a new colleague at work. Which greeting fits best?", "options": ["Hello, nice to meet you.", "Good night."], "correct_option": 0},
    })
    literal = TimelineProposal.model_validate({
        "timestamp_seconds": 12, "rationale": "A recall check.",
        "exercise": {"kind": "multiple_choice", "prompt": "What does the speaker say?", "options": ["Hello", "Bye"], "correct_option": 0},
    })
    assert useful.learning_action == "apply"
    assert not _is_literal_recall(useful)
    assert _is_literal_recall(literal)


def test_series_is_not_offered_as_a_checkpoint_exercise_kind():
    """`series` is passive read-aloud content with no submit/completion
    signal (see `ANSWERABLE_KINDS` in app.core.interactive_video), so Gemini
    must never be told it is a pickable checkpoint exercise kind - the offered
    kind list must not contain it, even though the prompt now explicitly
    calls it out as excluded."""
    offered_kinds_line = next(line for line in SYSTEM_PROMPT.splitlines() if "Pick the most useful kind" in line)
    assert "series" not in offered_kinds_line
    assert "Never propose `series`" in SYSTEM_PROMPT


def test_timeline_exercise_proposal_rejects_blank_prompt():
    with pytest.raises(ValidationError):
        TimelineExerciseProposal.model_validate({"kind": "writing", "prompt": ""})
    with pytest.raises(ValidationError):
        TimelineExerciseProposal.model_validate({"kind": "writing", "prompt": "   "})
    # Populated prompt still succeeds.
    assert TimelineExerciseProposal.model_validate({"kind": "writing", "prompt": "Write two sentences."}).prompt


def test_timeline_proposal_rejects_blank_practice_with_mariam_fields():
    base = {
        "timestamp_seconds": 8, "rationale": "A spoken-practice moment fits here.",
        "checkpoint_kind": "practice_with_mariam",
    }
    with pytest.raises(ValidationError):
        TimelineProposal.model_validate({**base, "conversation_goal": "", "opening_line": "Hi!"})
    with pytest.raises(ValidationError):
        TimelineProposal.model_validate({**base, "conversation_goal": "Practice greeting a colleague.", "opening_line": "   "})
    # Fully populated still succeeds.
    proposal = TimelineProposal.model_validate({
        **base, "conversation_goal": "Practice greeting a colleague.", "opening_line": "Hi there!",
    })
    assert proposal.conversation_goal == "Practice greeting a colleague."


def test_timeline_proposals_now_permits_up_to_twelve_checkpoints():
    checkpoints = [
        {
            "timestamp_seconds": i, "rationale": "A meaningful moment.",
            "exercise": {"kind": "multiple_choice", "prompt": f"Q{i}?", "options": ["A", "B"], "correct_option": 0},
        }
        for i in range(8)
    ]
    proposals = TimelineProposals.model_validate({"checkpoints": checkpoints})
    assert len(proposals.checkpoints) == 8
    with pytest.raises(ValidationError):
        TimelineProposals.model_validate({"checkpoints": checkpoints * 2})  # 16 > 12
