from typing import Optional

from app.models.topic_content import Level
from app.models.unit import Unit
from app.repositories.base_repository import BaseRepository


class UnitRepository(BaseRepository[Unit]):
    def __init__(self) -> None:
        super().__init__(Unit)

    async def list_all(self, level: Optional[Level] = None, owner_id: Optional[str] = None) -> list[Unit]:
        """`owner_id=None` (Root/internal use) returns every unit, unfiltered
        by author. Given, it scopes to that Administrator's own units plus
        legacy/shared ones (`created_by_id is None`) - same rule as
        `TopicContentRepository.list_all`.
        """
        query: dict = {"is_deleted": False}
        if level is not None:
            query["level"] = level.value
        if owner_id is not None:
            query["created_by_id"] = {"$in": [owner_id, None]}
        # Deterministic: explicit order, never Mongo's natural order - ties
        # (every unit defaults to 0) fall back to creation time.
        return await Unit.find(query).sort("order_index", "created_at").to_list()
