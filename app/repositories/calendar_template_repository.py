"""Calendar template queries and the one write primitive `activate()` needs
beyond plain `save()` - deactivating every other Standard template for the
same `(owner_admin_id, level)` in one bulk `$set`, so activating a new one is
"deactivate the rest, then activate this one" rather than a fetch-modify-save
loop over however many templates that admin has authored for the level.
"""

from typing import Any, Optional

from beanie.odm.operators.update.general import Set

from app.models.calendar_template import CalendarTemplate, TemplateKind
from app.models.topic_content import Level
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow


class CalendarTemplateRepository(BaseRepository[CalendarTemplate]):
    def __init__(self) -> None:
        super().__init__(CalendarTemplate)

    async def list_for_admin(self, owner_admin_id: Optional[str], kind: Optional[TemplateKind] = None) -> list[CalendarTemplate]:
        """`owner_admin_id=None` (the Center Admin's center-wide view) returns every template, any Teacher's."""
        query: dict = {"is_deleted": False}
        if owner_admin_id is not None:
            query["owner_admin_id"] = owner_admin_id
        if kind is not None:
            query["kind"] = kind.value
        return await CalendarTemplate.find(query).sort("-created_at").to_list()

    async def get_active_standard(self, owner_admin_id: str, level: Level) -> Optional[CalendarTemplate]:
        """The one calendar every Standard student at this level, under this
        admin, currently follows - `None` if the admin hasn't activated one
        yet (`ProgressService` treats that as "no calendar assigned").
        """
        return await CalendarTemplate.find_one(
            CalendarTemplate.owner_admin_id == owner_admin_id,
            CalendarTemplate.kind == TemplateKind.STANDARD,
            CalendarTemplate.level == level,
            CalendarTemplate.is_active == True,  # noqa: E712
            CalendarTemplate.is_deleted == False,  # noqa: E712
        )

    async def deactivate_all_standard(self, owner_admin_id: str, level: Level) -> None:
        await CalendarTemplate.find(
            CalendarTemplate.owner_admin_id == owner_admin_id,
            CalendarTemplate.kind == TemplateKind.STANDARD,
            CalendarTemplate.level == level,
            CalendarTemplate.is_active == True,  # noqa: E712
        ).update(Set({"is_active": False, "updated_at": utcnow()}))

    async def get_many_by_ids(self, template_ids: list[str]) -> list[CalendarTemplate]:
        if not template_ids:
            return []
        return await CalendarTemplate.find({"_id": {"$in": template_ids}, "is_deleted": False}).to_list()

    async def update_fields(self, template_id: str, values: dict[str, Any]) -> bool:
        """`$set` exactly `values` - same partial-write primitive as
        `TopicContentRepository.update_fields`, for the same reason: a whole-
        document `save()` would overwrite `days` with whatever snapshot the
        caller loaded, discarding any edit made in between.
        """
        result = await CalendarTemplate.find_one(
            CalendarTemplate.id == template_id,
            CalendarTemplate.is_deleted == False,  # noqa: E712
        ).update(Set({**values, "updated_at": utcnow()}))
        return result.matched_count == 1
