import re

from pydantic import BaseModel, Field, field_validator

from app.models.level_profile import LevelProfile
from app.models.topic_content import Level
from app.schema.base import BaseSchema


class LevelProfileCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=80)
    base_level: Level
    avatar_guidance: str = Field(min_length=10, max_length=2000)
    order_index: int = Field(default=0, ge=0, le=1000)

    @field_validator("code")
    @classmethod
    def slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            raise ValueError("Level code may contain lowercase letters, numbers, hyphens and underscores")
        return value


class LevelProfilePublic(BaseSchema):
    code: str
    name: str
    base_level: Level
    avatar_guidance: str
    order_index: int
    is_active: bool

    @classmethod
    def from_profile(cls, item: LevelProfile) -> "LevelProfilePublic":
        return cls.model_validate(item.model_dump())
