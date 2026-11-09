from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.models.cohort import Cohort, CohortStatus, CohortTeacherAssignment
from app.schema.base import BaseSchema
from app.schema.limits import MAX_TITLE_LENGTH


class CohortTeacherAssignmentIn(BaseModel):
    teacher_id: str
    program_level_ids: list[str] = Field(default_factory=list)


class CohortCreate(BaseModel):
    name: str = Field(min_length=2, max_length=MAX_TITLE_LENGTH)
    program_id: str
    program_level_ids: list[str] = Field(min_length=1, max_length=50)
    lead_teacher_id: str
    teacher_assignments: list[CohortTeacherAssignmentIn] = Field(default_factory=list)
    start_date: datetime
    end_date: Optional[datetime] = None

    @model_validator(mode="after")
    def _no_duplicate_stages(self) -> "CohortCreate":
        if len(set(self.program_level_ids)) != len(self.program_level_ids):
            raise ValueError("Program stages must be unique")
        return self


class CohortUpdate(BaseModel):
    """Every field optional. `program_id`/`program_level_ids` are
    deliberately not editable here - changing them would orphan the Cohort's
    per-stage calendars; recreate the Cohort instead if the curriculum scope
    needs to change."""

    name: Optional[str] = Field(default=None, min_length=2, max_length=MAX_TITLE_LENGTH)
    lead_teacher_id: Optional[str] = None
    teacher_assignments: Optional[list[CohortTeacherAssignmentIn]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[CohortStatus] = None


class CohortPublic(BaseSchema):
    name: str
    program_id: str
    program_level_ids: list[str]
    lead_teacher_id: str
    teacher_assignments: list[CohortTeacherAssignment]
    start_date: datetime
    end_date: Optional[datetime]
    status: CohortStatus
    created_by_id: str
    student_count: int = 0
    accessible_program_level_ids: list[str] = Field(default_factory=list)
    # Display-only denormalization.  The identifier remains the permission
    # source; callers never have to render it as a teacher's name.
    lead_teacher_name: Optional[str] = None

    @classmethod
    def from_cohort(
        cls,
        item: Cohort,
        student_count: int = 0,
        accessible_program_level_ids: Optional[list[str]] = None,
        lead_teacher_name: Optional[str] = None,
    ) -> "CohortPublic":
        data = item.model_dump()
        return cls.model_validate({
            **data,
            "student_count": student_count,
            "accessible_program_level_ids": accessible_program_level_ids or list(item.program_level_ids),
            "lead_teacher_name": lead_teacher_name,
        })
