from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enrollment import Enrollment, EnrollmentSource, EnrollmentStatus, LateJoinMode
from app.schema.base import BaseSchema
from app.schema.challenge import LessonDayState


class EnrollStudentsRequest(BaseModel):
    student_ids: list[str] = Field(min_length=1, max_length=500)
    program_level_id: str
    late_join_mode: LateJoinMode = LateJoinMode.CURRENT_DAY
    joined_at: Optional[datetime] = None


class EnrollmentPage(BaseModel):
    """A bounded Cohort roster.  Keeping this separate from the historical
    list response lets old callers retain their contract while the Batch
    workspace no longer has to download an entire roster."""
    items: list["EnrollmentPublic"]
    total: int
    page: int
    limit: int


class CohortStudentCandidate(BaseModel):
    """A selectable Student account, not an Enrollment.  `academic_level_*`
    describe the student's current academic mapping only; Standard/Pro are
    access plans and are intentionally absent from this contract."""
    id: str
    full_name: str
    email: str
    academic_level_id: Optional[str] = None
    academic_level_name: Optional[str] = None
    is_unassigned: bool = False


class CohortStudentCandidatePage(BaseModel):
    items: list[CohortStudentCandidate]
    total: int
    page: int
    limit: int


class BulkEnrollmentResult(BaseModel):
    created: list["EnrollmentPublic"]
    already_enrolled_student_ids: list[str] = Field(default_factory=list)


class EnrollmentPublic(BaseSchema):
    student_id: str
    program_id: str
    program_level_id: str
    cohort_id: Optional[str]
    status: EnrollmentStatus
    source: EnrollmentSource
    joined_at: datetime
    late_join_mode: Optional[LateJoinMode]
    created_by_id: str

    # Denormalized display fields, populated by the service layer (student's
    # own "My Learning" list, or a Cohort roster) - never stored on the
    # Enrollment itself.
    program_name: Optional[str] = None
    program_level_name: Optional[str] = None
    cohort_name: Optional[str] = None
    student_name: Optional[str] = None
    student_email: Optional[str] = None

    @classmethod
    def from_enrollment(
        cls,
        item: Enrollment,
        program_name: Optional[str] = None,
        program_level_name: Optional[str] = None,
        cohort_name: Optional[str] = None,
        student_name: Optional[str] = None,
        student_email: Optional[str] = None,
    ) -> "EnrollmentPublic":
        data = item.model_dump()
        return cls.model_validate({
            **data,
            "program_name": program_name,
            "program_level_name": program_level_name,
            "cohort_name": cohort_name,
            "student_name": student_name,
            "student_email": student_email,
        })


class TaughtStudent(BaseModel):
    """One row of a teacher's "My Students" page - a real Student account
    deduplicated across every Cohort the caller teaches, not a guess that
    every center student belongs to them. `enrollments` lists only the
    enrollments the caller is actually authorized to see (their own
    Cohorts, and only the stages they're assigned to within a multi-stage
    Cohort) - never every enrollment this student happens to hold."""

    id: str
    full_name: str
    email: str
    is_active: bool
    enrollments: list[EnrollmentPublic] = Field(default_factory=list)


class TaughtStudentPage(BaseModel):
    items: list[TaughtStudent]
    total: int
    page: int
    limit: int


class EnrollmentScheduleDay(BaseModel):
    day: int
    topic_ids: list[str]
    locked: bool
    # Kept alongside topic_ids for backwards-compatible clients.  The state
    # is computed from the same progress service that protects lesson access,
    # never guessed by the UI from a calendar date.
    topic_states: dict[str, LessonDayState] = Field(default_factory=dict)
    topic_titles: dict[str, str] = Field(default_factory=dict)


class EnrollmentSchedule(BaseModel):
    """A read-only day/topic preview for one Enrollment - locked/unlocked
    only, deliberately no completion status. See `EnrollmentService`'s
    module docstring for why."""

    has_template: bool
    total_days: int = 0
    rest_days: list[int] = Field(default_factory=list)
    current_day: int = 0
    days: list[EnrollmentScheduleDay] = Field(default_factory=list)
