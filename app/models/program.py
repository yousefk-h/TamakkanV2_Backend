"""Center-owned curriculum program (for example General English or IELTS)."""

from typing import Annotated

from beanie import Indexed
from pydantic import BaseModel, Field

from app.models.base import BaseDocument
from app.models.topic_content import Level


class ProgramLevelDefinition(BaseModel):
    """A learning stage inside one Program, explicitly mapped to the stable
    difficulty engine and optionally to a center-defined guidance profile."""

    id: str
    name: str
    order_index: int = 0
    difficulty_level: Level
    level_profile_id: str | None = None


class Program(BaseDocument):
    code: Annotated[str, Indexed(unique=True)]
    name: str
    language: str = "English"
    description: str = ""
    base_levels: list[Level] = Field(default_factory=list)
    level_profile_ids: list[str] = Field(default_factory=list)
    levels: list[ProgramLevelDefinition] = Field(default_factory=list)
    created_by_id: Annotated[str, Indexed()]
    is_active: bool = True

    class Settings:
        name = "programs"
        indexes = [[("is_deleted", 1), ("is_active", 1), ("name", 1)]]
