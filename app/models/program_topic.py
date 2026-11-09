"""Program-specific placement of one reusable TopicContent document."""

from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed
from pydantic import Field
from pymongo import IndexModel

from app.models.base import BaseDocument
from app.models.topic_content import Level


class ProgramTopicStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class ProgramTopic(BaseDocument):
    program_id: Annotated[str, Indexed()]
    topic_id: Annotated[str, Indexed()]
    # `program_level_id` is the authoritative curricular placement. `level`
    # and `level_profile_id` remain as denormalized legacy compatibility and
    # explicit difficulty mappings; new writes derive both from Program.levels.
    program_level_id: Optional[str] = None
    level: Level
    level_profile_id: Optional[str] = None
    # Legacy only. CalendarTemplate.days is the sole scheduling authority.
    order_index: int = 0
    day: Optional[int] = None
    status: ProgramTopicStatus = ProgramTopicStatus.DRAFT
    published_revision: Optional[int] = None
    assigned_by_id: str

    class Settings:
        name = "program_topics"
        indexes = [
            IndexModel([("program_id", 1), ("topic_id", 1)], unique=True),
            [("program_id", 1), ("program_level_id", 1), ("status", 1)],
        ]
