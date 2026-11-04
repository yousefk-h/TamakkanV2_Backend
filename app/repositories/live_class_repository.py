from typing import Optional

from app.models.live_class import LiveClass
from app.repositories.base_repository import BaseRepository


class LiveClassRepository(BaseRepository[LiveClass]):
    def __init__(self) -> None:
        super().__init__(LiveClass)

    async def get_all_for_admin(self, admin_id: Optional[str]) -> list[LiveClass]:
        """`admin_id=None` (the Center Admin's center-wide view) returns every class, any Teacher's."""
        query: dict = {"is_deleted": False}
        if admin_id is not None:
            query["created_by_id"] = admin_id
        return await LiveClass.find(query).sort(+LiveClass.scheduled_at).to_list()

    async def get_all_for_participant(self, student_id: str) -> list[LiveClass]:
        # Plain dict form: Mongo's equality operator on an array field
        # implicitly matches "the array contains this value" - exactly the
        # "classes I'm enrolled in" query, no `$elemMatch` needed.
        return (
            await LiveClass.find({"participant_ids": student_id, "is_deleted": False})
            .sort(+LiveClass.scheduled_at)
            .to_list()
        )
