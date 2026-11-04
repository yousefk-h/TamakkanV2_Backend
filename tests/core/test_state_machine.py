"""Exhaustive against both spec state diagrams: every valid transition
succeeds, and every *other* state pair (i.e. every invalid transition) is
rejected - not just a sampled few.
"""

import pytest

from app.core.state_machine import InvalidTransition, transition
from app.models.live_session import SESSION_TRANSITIONS, SessionState
from app.models.topic_content import TRANSCRIPT_TRANSITIONS, TranscriptState


def _all_pairs(states, allowed_table):
    for current in states:
        for target in states:
            yield current, target, target in allowed_table.get(current, set())


@pytest.mark.parametrize("current,target,is_valid", list(_all_pairs(SessionState, SESSION_TRANSITIONS)))
def test_session_state_transitions(current, target, is_valid):
    if is_valid:
        assert transition(current, target, SESSION_TRANSITIONS) == target
    else:
        with pytest.raises(InvalidTransition):
            transition(current, target, SESSION_TRANSITIONS)


def test_every_session_state_has_an_entry():
    for state in SessionState:
        assert state in SESSION_TRANSITIONS


@pytest.mark.parametrize(
    "current,target,is_valid", list(_all_pairs(TranscriptState, TRANSCRIPT_TRANSITIONS))
)
def test_transcript_state_transitions(current, target, is_valid):
    if is_valid:
        assert transition(current, target, TRANSCRIPT_TRANSITIONS) == target
    else:
        with pytest.raises(InvalidTransition):
            transition(current, target, TRANSCRIPT_TRANSITIONS)


def test_every_transcript_state_has_an_entry():
    for state in TranscriptState:
        assert state in TRANSCRIPT_TRANSITIONS
