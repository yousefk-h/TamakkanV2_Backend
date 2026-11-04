"""Unit CRUD (card #17 - Topic organization by unit + order)."""

from typing import Annotated, Optional

from pydantic import BaseModel, Field

from app.models.topic_content import Level
from app.schema.base import BaseSchema
from app.schema.limits import MAX_TITLE_LENGTH

UnitName = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]


class UnitCreate(BaseModel):
    level: Level
    name: UnitName
    order_index: int = 0


class UnitUpdate(BaseModel):
    """Every field optional - a partial update only touches what's provided,
    same convention as `TopicUpdate`. `level` is deliberately not editable
    here: moving a unit to a different level would silently strand every
    topic already assigned to it (a topic's own `level` would no longer
    match its unit's) - an admin who wants that recreates the unit instead.
    """

    name: Optional[UnitName] = None
    order_index: Optional[int] = None


class UnitPublic(BaseSchema):
    level: Level
    name: str
    order_index: int
    # `None` for a legacy/shared unit - mirrors `TopicListItem.created_by_id`.
    created_by_id: Optional[str] = None
