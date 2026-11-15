import json

from app.core.interactive_video_drafting import TimelineExerciseProposal, TimelineProposals


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
