"""Shared state-machine validator, used by every model with a real, persisted
lifecycle (currently `LiveSession.state` and `TopicContent.transcript_state`).

Deliberately not a library (`python-statemachine`, `transitions`, ...) - two
small, fully-enumerated machines don't justify one. See the platform plan's
engineering-standards section for the reasoning.
"""

from enum import Enum


class InvalidTransition(Exception):
    """Raised when a transition isn't in the model's own allowed-transitions table."""


def transition(current: Enum, target: Enum, allowed: dict[Enum, set[Enum]]) -> Enum:
    if target not in allowed.get(current, set()):
        raise InvalidTransition(f"{current} -> {target} is not a valid transition")
    return target
