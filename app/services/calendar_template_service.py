"""Calendar template CRUD, ownership, and the invariants a database round
trip is needed to check: every referenced topic actually exists (and, for a
Standard template, belongs to that template's own level - a Beginner
calendar referencing an Advanced topic would silently 403 the student the
moment they tried to open it), and a template can't be deleted while still
in active use (an active Standard template, or a Custom one a Pro student is
currently following).
"""

from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.topic_content import Level
from app.models.user import Role, User
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.schema.calendar_template import CalendarDayEntryIn, CalendarTemplateCreate, CalendarTemplateUpdate


class CalendarTemplateService:
    def __init__(
        self,
        template_repository: CalendarTemplateRepository,
        topic_repository: TopicContentRepository,
        user_repository: UserRepository,
    ) -> None:
        self.template_repository = template_repository
        self.topic_repository = topic_repository
        self.user_repository = user_repository

    async def create(self, body: CalendarTemplateCreate, admin: User) -> CalendarTemplate:
        await self._validate_topics(body.days, body.level)
        template = CalendarTemplate(
            name=body.name,
            kind=body.kind,
            owner_admin_id=admin.id,
            level=body.level,
            days=[CalendarDayEntry(day=entry.day, topic_ids=entry.topic_ids, custom_minutes=entry.custom_minutes) for entry in body.days],
        )
        return await self.template_repository.save(template)

    async def list_for_admin(self, admin: User, kind: Optional[TemplateKind] = None) -> list[CalendarTemplate]:
        """The Center Admin (`Role.ADMINISTRATOR`) sees every template in the center; a Teacher sees only their
        own, same restriction as everywhere else."""
        owner_id = None if admin.role == Role.ADMINISTRATOR else admin.id
        return await self.template_repository.list_for_admin(owner_id, kind=kind)

    async def get_one_for_admin(self, template_id: str, admin: User) -> CalendarTemplate:
        """The Teacher write-path ownership gate - the Center Admin bypasses it entirely, same reach as every
        other center-wide action."""
        template = await self.template_repository.get_by_id(template_id)
        if template is None:
            raise NotFoundException("Calendar template not found")
        if admin.role != Role.ADMINISTRATOR and template.owner_admin_id != admin.id:
            raise ForbiddenException("This calendar template belongs to a different teacher")
        return template

    async def update(self, template_id: str, body: CalendarTemplateUpdate, admin: User) -> CalendarTemplate:
        template = await self.get_one_for_admin(template_id, admin)
        changes = body.model_dump(exclude_unset=True)
        if "days" in changes:
            await self._validate_topics(body.days or [], template.level)
        if changes:
            await self.template_repository.update_fields(template_id, changes)
        return await self.get_one_for_admin(template_id, admin)

    async def activate(self, template_id: str, admin: User) -> CalendarTemplate:
        """Standard only - deactivates every other template for the same
        `(owner_admin_id, level)` first, so at most one is ever active.

        Scoped by the template's own `owner_admin_id`, not the caller's - when the Center Admin activates a
        Teacher's template, it's that Teacher's other standard templates at this level that must yield, not the
        Center Admin's own (nonexistent) ones.
        """
        template = await self.get_one_for_admin(template_id, admin)
        if template.kind != TemplateKind.STANDARD:
            raise UnprocessableException("Only a standard template can be activated - a custom one is assigned directly by id")
        assert template.level is not None  # enforced at creation
        await self.template_repository.deactivate_all_standard(template.owner_admin_id, template.level)
        await self.template_repository.update_fields(template_id, {"is_active": True})
        return await self.get_one_for_admin(template_id, admin)

    async def delete(self, template_id: str, admin: User) -> None:
        template = await self.get_one_for_admin(template_id, admin)
        if template.kind == TemplateKind.STANDARD and template.is_active:
            raise UnprocessableException("Deactivate this template before deleting it - students are still following it")
        if template.kind == TemplateKind.CUSTOM:
            students = await self.user_repository.get_students_assigned_to_template(template_id)
            if students:
                raise UnprocessableException(f"This template is still assigned to {len(students)} student(s) - reassign them first")
        await self.template_repository.soft_delete(template)

    async def _validate_topics(self, days: list[CalendarDayEntryIn], level: Optional[Level]) -> None:
        topic_ids = {tid for entry in days for tid in entry.topic_ids}
        if not topic_ids:
            return
        topics = await self.topic_repository.get_many_by_ids(list(topic_ids))
        found_ids = {t.id for t in topics}
        missing = topic_ids - found_ids
        if missing:
            raise UnprocessableException(f"Unknown topic id(s): {', '.join(sorted(missing))}")
        if level is not None:
            wrong_level = [t.title for t in topics if t.level != level]
            if wrong_level:
                raise UnprocessableException(
                    f"These topics aren't in the {level.value} level: {', '.join(wrong_level)}"
                )


def get_calendar_template_service() -> CalendarTemplateService:
    return CalendarTemplateService(CalendarTemplateRepository(), TopicContentRepository(), UserRepository())
