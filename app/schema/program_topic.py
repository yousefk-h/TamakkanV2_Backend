from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.topic_content import Level
from app.schema.base import BaseSchema


class ProgramTopicWrite(BaseModel):
    topic_id: str
    program_level_id: Optional[str] = None
    # Legacy clients may still send the mapped difficulty. New clients use
    # program_level_id; scheduling fields deliberately are not accepted here.
    level: Optional[Level] = None
    status: ProgramTopicStatus = ProgramTopicStatus.DRAFT

    @model_validator(mode="after")
    def placement_required(self) -> "ProgramTopicWrite":
        if self.program_level_id is None and self.level is None:
            raise ValueError("Choose a Program level")
        return self


class ProgramTopicUpdate(BaseModel):
    program_level_id: Optional[str] = None
    level: Optional[Level] = None  # legacy compatibility
    status: Optional[ProgramTopicStatus] = None


class ProgramTopicPublic(BaseSchema):
    program_id: str
    topic_id: str
    topic_title: str
    program_level_id: Optional[str]
    level: Level
    level_profile_id: Optional[str]
    order_index: int
    day: Optional[int]
    status: ProgramTopicStatus
    published_revision: Optional[int]
    assigned_by_id: str

    @classmethod
    def from_assignment(cls, item: ProgramTopic, topic_title: str) -> "ProgramTopicPublic":
        return cls(
            id=item.id,
            created_at=item.created_at,
            updated_at=item.updated_at,
            program_id=item.program_id,
            topic_id=item.topic_id,
            topic_title=topic_title,
            program_level_id=item.program_level_id,
            level=item.level,
            level_profile_id=item.level_profile_id,
            order_index=item.order_index,
            day=item.day,
            status=item.status,
            published_revision=item.published_revision,
            assigned_by_id=item.assigned_by_id,
        )
