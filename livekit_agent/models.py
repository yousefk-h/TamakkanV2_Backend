"""Plain enums shared with the main backend's own model definitions
(`app/models/live_session.py`, `app/models/topic_content.py`) - duplicated,
not imported, for the same reason as the rest of this package: it's a
separate deployable that can't rely on the sibling `app/` package being
present at runtime (see `agent.py`'s module docstring).

This worker has no database of its own and no `Document` models: the values
here are just the vocabulary it shares with the backend over the job's
dispatch metadata (`Level`) and the internal HTTP calls it makes back
(`EndReason`) - see `agent.py`.
"""

from enum import Enum


class Level(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ArabicInterventionLevel(str, Enum):
    """Duplicated from `app.models.topic_content.ArabicInterventionLevel` for
    the same reason as `Level` above - how often/deeply the optional
    Jordanian-Arabic clarification support (see `prompts.py`) kicks in, only
    consulted when the dispatch metadata's `arabic_support_enabled` is true.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EndReason(str, Enum):
    USER_ENDED = "user_ended"
    IDLE_TIMEOUT = "idle_timeout"
    ADMIN_TERMINATED = "administrator_terminated"
    CONNECTION_FAILURE = "connection_failure"
    MAX_DURATION = "max_duration_reached"
