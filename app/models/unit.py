"""A named, ordered grouping of Topics within one Level (card #17 - "Unit 1 -
Introductions", "Unit 2 - Everyday Life", ...), replacing what was otherwise
just creation-time ordering with no intentional structure.

Teacher-owned, exactly like `TopicContent` (see `app.models.topic_content
.TopicContent.created_by_id`'s comment for why this is a plain indexed id,
not a `Link[User]`) - a legacy/unowned Unit (`created_by_id is None`) is
shared read-only across every Administrator, same compatibility rule as a
legacy Topic, enforced the same way (`UnitService.get_own_unit`).
"""

from typing import Annotated, Optional

from beanie import Indexed

from app.models.base import BaseDocument
from app.models.topic_content import Level


class Unit(BaseDocument):
    level: Level
    name: str
    # This unit's position among its level's units - ties broken by
    # `created_at`, same convention as `TopicContent.order_index`.
    order_index: int = 0
    created_by_id: Annotated[Optional[str], Indexed()] = None

    class Settings:
        name = "units"
        indexes = [
            [("is_deleted", 1), ("level", 1), ("order_index", 1), ("created_at", 1)],
            [("is_deleted", 1), ("created_by_id", 1)],
        ]
