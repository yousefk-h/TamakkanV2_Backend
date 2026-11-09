from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.homework import Homework, HomeworkKind
from app.schema.base import BaseSchema
from app.schema.limits import MAX_LIST_ITEMS, MAX_TITLE_LENGTH, MAX_URL_LENGTH


class HomeworkCreate(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    instructions: str = Field(default="", max_length=5000)
    kind: HomeworkKind = HomeworkKind.LESSON
    due_at: datetime
    # Either a hand-picked/legacy roster (existing behavior)...
    participant_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    # ...or a Cohort target: every student currently enrolled in that Cohort
    # (optionally narrowed to one of its stages) that the caller is
    # themselves authorized to reach - see `HomeworkService.create`.
    cohort_id: Optional[str] = None
    program_level_id: Optional[str] = None
    topic_id: Optional[str] = None
    external_url: str = Field(default="", max_length=MAX_URL_LENGTH)

    @field_validator("due_at")
    @classmethod
    def utc_date(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    @model_validator(mode="after")
    def valid_target(self) -> "HomeworkCreate":
        if self.kind == HomeworkKind.LESSON and not self.topic_id:
            raise ValueError("Lesson homework needs a lesson")
        if self.kind == HomeworkKind.LINK and not self.external_url.startswith("https://"):
            raise ValueError("Link homework needs an https:// URL")
        if not self.participant_ids and not self.cohort_id:
            raise ValueError("Choose students, or a Cohort to target")
        if self.program_level_id and not self.cohort_id:
            raise ValueError("A stage only makes sense together with a Cohort")
        return self


class HomeworkPublic(BaseSchema):
    created_by_id: str
    title: str
    instructions: str
    kind: HomeworkKind
    due_at: datetime
    participant_ids: list[str]
    cohort_id: Optional[str] = None
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    topic_id: Optional[str]
    topic_title: Optional[str] = None
    external_url: str

    @classmethod
    def from_homework(cls, item: Homework, topic_title: Optional[str] = None) -> "HomeworkPublic":
        return cls.model_validate({**item.model_dump(), "topic_title": topic_title})
