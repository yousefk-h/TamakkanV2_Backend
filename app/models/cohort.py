"""A Cohort is one real running batch of students against an existing
Program (optionally a subset of its stages) - for example "General English -
October Batch". It never copies Program curriculum or TopicContent; it only
scopes who teaches it, who's enrolled (via `Enrollment.cohort_id`), when it
runs, and - per stage - a `CalendarTemplate` (kind=COHORT, see that model)
that starts as a copy of the Program stage's own schedule and can diverge
from it without mutating the Program's calendar or any other Cohort's.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed
from pydantic import BaseModel, Field

from app.models.base import BaseDocument


class CohortStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CohortTeacherAssignment(BaseModel):
    """One additional teacher's reach inside this Cohort. An empty
    `program_level_ids` means every stage the Cohort runs; a non-empty list
    scopes that teacher to only those stages (e.g. one teacher per level in a
    multi-level Cohort)."""

    teacher_id: str
    program_level_ids: list[str] = Field(default_factory=list)


class Cohort(BaseDocument):
    name: str
    program_id: Annotated[str, Indexed()]
    # Subset of `Program.levels` ids this Cohort actually runs - a Cohort
    # need not offer every stage its Program defines.
    program_level_ids: list[str]
    # Assigned by Center Admin. Always has full reach across every stage
    # this Cohort runs, unlike an additional teacher who can be stage-scoped.
    lead_teacher_id: Annotated[str, Indexed()]
    teacher_assignments: list[CohortTeacherAssignment] = Field(default_factory=list)
    start_date: datetime
    end_date: Optional[datetime] = None
    status: CohortStatus = CohortStatus.ACTIVE
    created_by_id: str

    class Settings:
        name = "cohorts"
        indexes = [
            [("program_id", 1), ("is_deleted", 1), ("status", 1)],
            [("lead_teacher_id", 1), ("is_deleted", 1)],
        ]
