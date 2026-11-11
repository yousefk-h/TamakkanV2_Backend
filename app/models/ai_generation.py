"""Tracks every paid AI generation attempt (preparation-time, teacher-
triggered content drafting - never a student lesson-page read) so spend is
visible per lesson and a genuinely concurrent duplicate request is never
billed twice.

Two collections, two different jobs:

- `AIGenerationRecord` is a permanent, append-only usage/audit log - one row
  per attempt, kept forever for analytics. It never blocks anything.
- `AIGenerationLock` is an ephemeral mutex, held only for the duration of
  one in-flight provider call and released (deleted) the moment it
  completes or fails - see `app.core.ai_cost_control.claim_generation`. Its
  unique index on `generation_key` is what resolves a race between two
  truly concurrent identical requests to exactly one winner (the same
  atomic-compare-and-set principle `TopicContentRepository.transition_state`
  already uses elsewhere in this codebase, generalized for generation jobs
  rather than duplicated as a second mechanism) - a SEQUENTIAL second
  request, made after the first one already finished, is always free to
  proceed: several teacher workflows explicitly rely on being able to
  re-request the same drafting operation (e.g. manually re-running content
  drafting to fill fields an earlier pass missed), and a lock that
  outlived its own request would silently break them.

Neither collection ever stores prompt text, transcript text, or any other
lesson/student content - only identifiers, a model name, token counts, and
a derived dollar estimate. `generation_key` is a one-way hash, not
reversible to the content it was computed from.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pymongo import IndexModel

from app.models.base import BaseDocument


class AIGenerationType(str, Enum):
    TRANSCRIPT_EXTRACTION = "transcript_extraction"
    CONTENT_DRAFT = "content_draft"
    CONTENT_REGENERATE = "content_regenerate"
    VOCABULARY_ARABIC_BACKFILL = "vocabulary_arabic_backfill"
    EXERCISE_DRAFT = "exercise_draft"
    OBJECTIVES_DRAFT = "objectives_draft"
    TRANSCRIPT_ENGLISH_REVIEW = "transcript_english_review"
    SESSION_SCORING = "session_scoring"


class AIGenerationStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class AIGenerationRecord(BaseDocument):
    generation_type: AIGenerationType
    # sha256 hex digest of (generation_type, topic/session id, a content
    # fingerprint, the generation's own parameters, and the model) - see
    # `app.core.ai_cost_control.compute_generation_key`. Deliberately NOT
    # unique here (see this module's own docstring) - several rows can
    # legitimately share one key over time, one per actual attempt.
    generation_key: str
    topic_id: Optional[str] = None
    session_id: Optional[str] = None
    requested_by_id: Optional[str] = None
    model: str
    status: AIGenerationStatus
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # None when pricing isn't configured for this model (see
    # app.core.ai_cost_control) - never a guessed number.
    estimated_cost_usd: Optional[float] = None
    # True whenever the dollar figure is derived from a configured $/1K-token
    # rate rather than a provider-reported invoice line - i.e. always, today.
    # Kept as its own field (rather than inferred from estimated_cost_usd
    # being set) so a future direct-from-provider cost figure can be marked
    # measured without changing every existing row's meaning.
    cost_is_estimate: bool = True
    started_at: datetime
    completed_at: Optional[datetime] = None
    # Exception type name only (e.g. "ExerciseDraftingError") - never the
    # exception message, which could echo back lesson/source content.
    error_type: Optional[str] = None

    class Settings:
        name = "ai_generation_records"
        indexes = [
            [("generation_key", 1)],
            [("topic_id", 1), ("started_at", -1)],
        ]


class AIGenerationLock(BaseDocument):
    """An ephemeral mutex, not a log - see this module's own docstring.
    `claimed_at` is what lets a crashed/abandoned claim be reclaimed after
    `Settings.AI_GENERATION_STALE_CLAIM_MINUTES`, the same reclaim-after-
    timeout reasoning `TranscriptPipelineService._extraction_is_abandoned`
    already uses for the transcript state machine.
    """

    generation_key: str
    claimed_at: datetime

    class Settings:
        name = "ai_generation_locks"
        indexes = [IndexModel([("generation_key", 1)], unique=True)]
