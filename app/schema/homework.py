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
    participant_ids: list[str] = Field(min_length=1, max_length=MAX_LIST_ITEMS)
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
        return self


class HomeworkPublic(BaseSchema):
    created_by_id: str
    title: str
    instructions: str
    kind: HomeworkKind
    due_at: datetime
    participant_ids: list[str]
    topic_id: Optional[str]
    topic_title: Optional[str] = None
    external_url: str

    @classmethod
    def from_homework(cls, item: Homework, topic_title: Optional[str] = None) -> "HomeworkPublic":
        return cls.model_validate({**item.model_dump(), "topic_title": topic_title})
