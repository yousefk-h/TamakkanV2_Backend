"""Response shape for `GET /me/challenge-calendar` - which lesson topics
occupy each non-check-in day of the 30-day challenge, and whether each one is
done, current, missed, or locked. See `ProgressService.get_lesson_schedule`
for how a missed course rolls forward into the next day instead of being
silently skipped, and `resolve_topic_statuses` for how a topic that appears
on more than one day (while backlogged) resolves to one overall status.
"""

from enum import Enum

from pydantic import BaseModel, Field

from app.models.program import UnlockPolicy


class LessonDayState(str, Enum):
    DONE = "done"
    CURRENT = "current"
    # Past its day, no ended session for its topic yet - the same course
    # occupies the following day too (see `ProgressService`), which is what
    # actually drives the frontend's "you still owe N lessons" reminder banner.
    MISSED = "missed"
    LOCKED = "locked"


class TopicSlot(BaseModel):
    topic_id: str
    state: LessonDayState


class LessonDaySlot(BaseModel):
    day: int
    # One day can hold any number of courses, each tracked/rolled over
    # independently - empty once every entitled topic has been scheduled and
    # completed, or before anything has ever been reached for that day.
    topics: list[TopicSlot]


class ChallengeCalendar(BaseModel):
    lesson_days: list[LessonDaySlot]
    total_days: int = 30
    rest_days: list[int] = Field(default_factory=list)
    # Count of individual `MISSED` course-occurrences as of today - 0 means
    # nothing to remind about.
    pending_count: int
    # False when the student has no calendar template assigned yet (a
    # Standard level with no active template, or a Pro student not yet given
    # one) - the frontend uses this to show "your teacher hasn't set up your
    # calendar yet" instead of implying there's simply nothing scheduled. A
    # template that exists but hasn't started yet (a future
    # `User.program_start_date`) still returns `True` here - every day just
    # comes back `locked`, which is its own correct signal.
    has_template: bool = True
    # SCHEDULE (the long-standing default): `total_days`/day numbers are real
    # calendar days anchored to `program_start_date`. COMPLETION: `day`
    # numbers are pure ordering keys (no calendar date), `total_days` is the
    # real count of lesson nodes in the template, and there is never a
    # MISSED state - the frontend must label/group accordingly, never
    # printing a manufactured calendar day for a COMPLETION-policy program.
    unlock_policy: UnlockPolicy = UnlockPolicy.SCHEDULE
