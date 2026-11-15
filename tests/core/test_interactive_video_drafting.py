import json

from app.core.interactive_video_drafting import TimelineExerciseProposal, TimelineProposal, TimelineProposals
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
