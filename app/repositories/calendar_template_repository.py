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

    async def get_active_standard_for_program(self, program_id: str, level: Level) -> Optional[CalendarTemplate]:
        return await CalendarTemplate.find_one(
            CalendarTemplate.program_id == program_id,
            CalendarTemplate.kind == TemplateKind.STANDARD,
            CalendarTemplate.level == level,
            CalendarTemplate.is_active == True,  # noqa: E712
            CalendarTemplate.is_deleted == False,  # noqa: E712
        )

    async def get_active_standard_for_program_level(
        self, program_id: str, program_level_id: str
    ) -> Optional[CalendarTemplate]:
        return await CalendarTemplate.find_one(
            CalendarTemplate.program_id == program_id,
            CalendarTemplate.program_level_id == program_level_id,
            CalendarTemplate.kind == TemplateKind.STANDARD,
            CalendarTemplate.is_active == True,  # noqa: E712
            CalendarTemplate.is_deleted == False,  # noqa: E712
        )

    async def deactivate_all_standard(
        self,
        owner_admin_id: str,
        level: Level,
        program_id: str | None = None,
        program_level_id: str | None = None,
    ) -> None:
        query: dict = {
            "owner_admin_id": owner_admin_id,
            "kind": TemplateKind.STANDARD.value,
            "level": level.value,
            "is_active": True,
        }
        if program_id:
            query.update(program_id=program_id, program_level_id=program_level_id)
        else:
            query["program_id"] = None
        await CalendarTemplate.find(query).update(Set({"is_active": False, "updated_at": utcnow()}))

    async def get_active_cohort_template(self, cohort_id: str, program_level_id: str) -> Optional[CalendarTemplate]:
        return await CalendarTemplate.find_one(
            CalendarTemplate.cohort_id == cohort_id,
            CalendarTemplate.program_level_id == program_level_id,
            CalendarTemplate.kind == TemplateKind.COHORT,
            CalendarTemplate.is_active == True,  # noqa: E712
            CalendarTemplate.is_deleted == False,  # noqa: E712
        )

    async def get_effective_schedule(
        self, program_id: str, program_level_id: str, cohort_id: Optional[str]
    ) -> Optional[CalendarTemplate]:
        """The ONE deterministic answer to "what schedule does this
        enrollment actually follow": a Cohort's own stage template if it has
        one, otherwise the Program stage's shared STANDARD template. Every
        enrollment-aware read path must call this rather than re-deriving the
        same choice independently.
        """
        if cohort_id:
            cohort_template = await self.get_active_cohort_template(cohort_id, program_level_id)
            if cohort_template is not None:
                return cohort_template
        return await self.get_active_standard_for_program_level(program_id, program_level_id)

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
