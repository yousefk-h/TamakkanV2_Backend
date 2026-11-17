"""Response shape for `GET /me/progress` - a student's streak, badges, and
30-day challenge position. Nothing here is persisted: every field is computed
fresh from `LiveSession`/`RecordingSubmission`/`TopicContent` on each request
by `ProgressService`, the same "derive, don't store" choice already made for
`LiveSessionPublic.report` (computed once at session-end and stored there,
whereas this recomputes every time since streak/badge state depends on *all*
history, not one document).
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.models.program import UnlockPolicy


class BadgeKey(str, Enum):
    STREAK = "streak"
    VOCAB_MASTER = "vocab_master"
    FIRST_CLASS = "first_class"
    TOP_SCORER = "top_scorer"
    FAST_IMPROVER = "fast_improver"
    LEVEL_COMPLETE = "level_complete"


class Badge(BaseModel):
    key: BadgeKey
    earned: bool
    # When the badge's condition was first met - `None` while `earned` is
    # False. Copy (icon/title/desc) is a frontend i18n concern, keyed by
    # `key`, not carried here.
    earned_at: Optional[datetime] = None


class ChallengeMilestone(BaseModel):
    day: int
    reached: bool


class StudentProgress(BaseModel):
    current_streak: int
    longest_streak: int
    total_active_days: int
    # 1-based, capped to the challenge length, `0` if the program hasn't
    # started yet - see `ProgressService` for how day 1 is anchored to
    # `program_start_date`, the date the assigning admin chose.
    challenge_day: int
    challenge_total_days: int
    # The date `challenge_day` counts from - `None` if no plan has ever been
    # assigned. Lets the frontend show "starts {date}" when `challenge_day`
    # is `0` instead of a confusing "Day 0."
    program_start_date: Optional[date] = None
    # The real Program this challenge_day/calendar belongs to - `None` only
    # when genuinely nothing resolved (no plan, no Enrollment at all). Lets
    # the frontend say "{program_name} Learning Journey" instead of a
    # generic, program-agnostic "30-Day Challenge" label.
    program_name: Optional[str] = None
    # True when this student holds 2+ active Enrollments - `_resolve_template`
    # deliberately never guesses which one this single-calendar page should
    # show in that case (every other field above is then `None`/`0`, the
    # same as "no plan"). The frontend uses this to point the student to
    # "My Learning" (which lists every enrollment separately) instead of
    # implying they have no plan at all.
    has_multiple_enrollments: bool = False
    # SCHEDULE (default): `challenge_day`/`challenge_total_days` are real
    # calendar days anchored to `program_start_date`. COMPLETION:
    # `challenge_day` is how many lesson nodes are done/current and
    # `challenge_total_days` is the real node count - neither is a calendar
    # date, so the frontend must never print "Day N of 30" for this policy.
    unlock_policy: UnlockPolicy = UnlockPolicy.SCHEDULE
    # Oldest to newest, always exactly 7 entries, the 7th being today - the
    # weekly dot tracker's data, not a display-ready label set (the frontend
    # already owns day-of-week formatting via the locale).
    last_7_days_active: list[bool]
    last_7_days_dates: list[date]
    badges: list[Badge]
    milestones: list[ChallengeMilestone]
