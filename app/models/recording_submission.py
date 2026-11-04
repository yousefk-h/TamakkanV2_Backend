"""A student's self-recorded audio (per-task) or video (weekly check-in).

No `state`/state-machine here on purpose: the 5-state recording UI (Idle ->
Requesting Permission -> Recording -> Recorded Preview -> Submitted) from the
recording spec is a *client-side capture* state machine - a `RecordingSubmission`
document is only ever created once, at the moment of submission, so every
document that exists would forever sit in a single "Submitted" state. A field
that never has more than one real value isn't a state machine; it's dead
weight. What the backend actually needs to track after submission - review
status, replacement, and expiry - is plain booleans/fields below, not a guarded
transition table.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed, Link
from pydantic import Field

from app.models.base import BaseDocument
from app.models.topic_content import TopicContent
from app.models.user import User
from app.utils.time import utcnow

RECORDING_TTL_DAYS = 7
MAX_AUDIO_TASK_DURATION_SECONDS = 120
MAX_END_OF_WEEK_VIDEO_DURATION_SECONDS = 120


class SubmissionKind(str, Enum):
    AUDIO_TASK = "audio_task"
    # Value deliberately unchanged from the original "Friday" naming - this
    # persists on every existing submission document, and there's no
    # user-facing reason to force a data migration just to rename a wire
    # value nothing external reads. Only the Python identifier and every
    # user-facing string changed to "end of week" (Phase F+ generalized
    # check-in days away from a literal Friday - see `app.core.challenge`).
    END_OF_WEEK_VIDEO = "friday_video"


class RecordingSubmission(BaseDocument):
    student: Link[User]
    # Denormalized, for the same reason as `User.created_by_id`: Beanie Link
    # fields don't support equality queries, and this is what
    # `get_latest_for_student_and_topic` actually filters on. Indexed via the
    # compound indexes in `Settings` rather than on its own - it is the leading
    # field of both, so a standalone index would be redundant.
    student_id: str

    kind: SubmissionKind
    # Both `None` for an end-of-week video (day-tied, not topic-tied) - both
    # required for an audio task. The service methods are what actually
    # enforce which fields go with which `kind`; the model just has to allow
    # either shape.
    topic: Optional[Link[TopicContent]] = None
    topic_id: Annotated[Optional[str], Indexed()] = None
    # Which day (1-30) of the challenge this end-of-week video answers -
    # `None` for an audio task. Structured rather than left to `label`
    # alone: a free-text label isn't queryable, and resubmission/"did they
    # submit for day N" both need to filter on this directly.
    day: Optional[int] = None
    label: str  # topic title at submission time, or "End-of-week check-in - Day 12"

    media_url: str  # Cloudinary URL at launch (never a client-side blob: URL)
    # Cloudinary's `public_id` for this asset - the cleanup job needs it to
    # issue a delete; `media_url` alone isn't a valid API argument for that.
    cloudinary_public_id: str
    duration_seconds: float

    reviewed: bool = False
    replaces: Optional[Link["RecordingSubmission"]] = None

    submitted_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(days=RECORDING_TTL_DAYS))
    media_deleted: bool = False

    class Settings:
        name = "recording_submissions"
        # All three read paths sort by `submitted_at` descending, which was in
        # no index - so every one ended in a blocking in-memory SORT. The admin
        # list is `{student_id: {$in: [...roster]}}` + that sort, which at a
        # couple of hundred students and a year of history meant sorting tens of
        # thousands of documents per request, near Mongo's 32 MB sort limit.
        # These make each read an index walk instead. `student_id` no longer
        # needs its own index - it's the prefix of both compounds.
        indexes = [
            [("student_id", 1), ("submitted_at", -1)],
            [("student_id", 1), ("topic_id", 1), ("submitted_at", -1)],
            # The end-of-week-video equivalent of the topic compound above -
            # same "find the current one for this (student, key)" query,
            # keyed by `day` instead of `topic_id`.
            [("student_id", 1), ("day", 1), ("submitted_at", -1)],
            # The cleanup job's scan (`expires_at < now`, `media_deleted` false).
            [("media_deleted", 1), ("expires_at", 1)],
        ]
