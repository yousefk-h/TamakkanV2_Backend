"""Center-defined curriculum level layered on a stable agent proficiency band."""

from typing import Annotated

from beanie import Indexed

from app.models.base import BaseDocument
from app.models.topic_content import Level


class LevelProfile(BaseDocument):
    code: Annotated[str, Indexed(unique=True)]
    name: str
    base_level: Level
    avatar_guidance: str
    order_index: int = 0
    is_active: bool = True

    class Settings:
        name = "level_profiles"
        indexes = [[("is_deleted", 1), ("order_index", 1), ("created_at", 1)]]
