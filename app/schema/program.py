import re

from pydantic import BaseModel, Field, field_validator

from app.models.program import Program, ProgramLevelDefinition
from app.models.topic_content import Level
from app.schema.base import BaseSchema


class ProgramWrite(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=100)
    language: str = Field(default="English", min_length=2, max_length=60)
    description: str = Field(default="", max_length=1000)
    base_levels: list[Level] = Field(min_length=1, max_length=3)
    level_profile_ids: list[str] = Field(default_factory=list, max_length=50)
    levels: list[ProgramLevelDefinition] = Field(default_factory=list, max_length=50)

    @field_validator("code")
    @classmethod
    def slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            raise ValueError("Program code may contain lowercase letters, numbers, hyphens and underscores")
        return value


class ProgramPublic(BaseSchema):
    code: str
    name: str
    language: str
    description: str
    base_levels: list[Level]
    level_profile_ids: list[str]
    levels: list[ProgramLevelDefinition]
    created_by_id: str
    is_active: bool

    @classmethod
    def from_program(cls, item: Program) -> "ProgramPublic":
        data = item.model_dump()
        if not data.get("levels"):
            data["levels"] = [
                ProgramLevelDefinition(id=level.value, name=level.value.title(), order_index=index, difficulty_level=level)
                for index, level in enumerate(item.base_levels)
            ]
        return cls.model_validate(data)
