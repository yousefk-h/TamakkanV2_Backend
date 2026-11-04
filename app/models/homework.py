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
    topic_id: Optional[str] = None
    external_url: str = ""

    class Settings:
        name = "homework"
        indexes = [
            [("created_by_id", 1), ("due_at", 1)],
            [("participant_ids", 1), ("due_at", 1)],
        ]
