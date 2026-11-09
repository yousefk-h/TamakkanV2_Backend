"""A teacher-assigned task shown to selected students with a real due date."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed
from pydantic import Field

from app.models.base import BaseDocument


class HomeworkKind(str, Enum):
    LESSON = "lesson"
    WRITING = "writing"
    AUDIO = "audio"
    LINK = "link"


class Homework(BaseDocument):
    created_by_id: Annotated[str, Indexed()]
    title: str
    instructions: str = ""
    kind: HomeworkKind = HomeworkKind.LESSON
    due_at: Annotated[datetime, Indexed()]
    participant_ids: list[str] = Field(default_factory=list)
    # Enrollment snapshot prevents later roster moves from obscuring which
    # Program/stage this assignment targeted. Legacy rows default empty.
    participant_program_context: dict[str, str] = Field(default_factory=dict)
    # participant student_id -> the specific Enrollment this assignment was
    # targeted through, when it was Cohort-targeted. Empty for legacy/
    # individually-picked assignments - never guessed after the fact.
    participant_enrollment_ids: dict[str, str] = Field(default_factory=dict)
    # Set only when this assignment was targeted at a Cohort (optionally one
    # of its stages) rather than hand-picked/legacy Plan-or-level students.
    # `None` for every pre-existing row and every individually-targeted one.
    cohort_id: Optional[str] = None
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    topic_id: Optional[str] = None
    external_url: str = ""

    class Settings:
        name = "homework"
        indexes = [
            [("created_by_id", 1), ("due_at", 1)],
            [("participant_ids", 1), ("due_at", 1)],
        ]
