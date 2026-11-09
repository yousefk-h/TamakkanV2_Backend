"""The real many-to-many relationship between a Student and a Program stage,
optionally inside a Cohort. Replaces the scalar `User.program_id`/
`program_level_id` as the authoritative association so a student can hold
several simultaneous enrollments; those `User` fields remain, read-only, as
the legacy single-enrollment fallback every pre-existing path (sessions,
progress, homework, exercise attempts) still reads - see
`backend/scripts/backfill_enrollments.py` and `docs/HANDOFF.md` for the
migration note. Nothing currently deletes or rewrites those `User` fields.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed

from app.models.base import BaseDocument


class EnrollmentStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class EnrollmentSource(str, Enum):
    MANUAL = "manual"
    COHORT = "cohort"
    LEGACY_BACKFILL = "legacy_backfill"


class LateJoinMode(str, Enum):
    CURRENT_DAY = "current_day"
    DAY_ONE = "day_one"


class Enrollment(BaseDocument):
    student_id: Annotated[str, Indexed()]
    program_id: Annotated[str, Indexed()]
    program_level_id: str
    # `None` = a direct Program enrollment outside any Cohort (including
    # every legacy-backfilled row - see `EnrollmentSource.LEGACY_BACKFILL`).
    cohort_id: Annotated[Optional[str], Indexed()] = None
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE
    source: EnrollmentSource = EnrollmentSource.MANUAL
    joined_at: datetime
    # Cohort enrollments only - how this student's day-count anchors against
    # the Cohort's schedule. `None` for a direct Program enrollment.
    late_join_mode: Optional[LateJoinMode] = None
    created_by_id: str

    class Settings:
        name = "enrollments"
        indexes = [
            [("student_id", 1), ("is_deleted", 1), ("status", 1)],
            [("cohort_id", 1), ("is_deleted", 1)],
            [("student_id", 1), ("program_id", 1), ("cohort_id", 1)],
        ]
