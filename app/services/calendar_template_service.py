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
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.user import Role, User
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.schema.calendar_template import CalendarDayEntryIn, CalendarTemplateCreate, CalendarTemplateUpdate, _validate_schedule_shape


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
            duration_days=body.duration_days,
            rest_days=body.rest_days,
            days=[CalendarDayEntry(day=entry.day, topic_ids=entry.topic_ids, custom_minutes=entry.custom_minutes) for entry in body.days],
        )
        return await self.template_repository.save(template)

    async def list_for_admin(self, admin: User, kind: Optional[TemplateKind] = None) -> list[CalendarTemplate]:
        """The Center Admin sees every template. Teachers see templates they
        authored plus the Cohort calendars for stages they are assigned to.

        A Cohort calendar is owned by the Center Admin who created the Cohort,
        so owner-only filtering made the Batch Schedule UI appear but left it
        unusable for the actual teacher.
        """
        if admin.role == Role.ADMINISTRATOR:
            return await self.template_repository.list_for_admin(None, kind=kind)
        own = await self.template_repository.list_for_admin(admin.id, kind=kind)
        if admin.role != Role.TEACHER or (kind is not None and kind != TemplateKind.COHORT):
            return own

        from app.models.cohort import Cohort
        from app.services.cohort_service import accessible_stage_ids, has_cohort_access

        cohorts = await Cohort.find({"is_deleted": False}).to_list()
        accessible = {
            (cohort.id, stage_id)
            for cohort in cohorts
            if has_cohort_access(cohort, admin)
            for stage_id in accessible_stage_ids(cohort, admin)
        }
        cohort_templates = await self.template_repository.list_for_admin(None, kind=TemplateKind.COHORT)
        seen = {template.id for template in own}
        return own + [
            template for template in cohort_templates
            if template.id not in seen and (template.cohort_id, template.program_level_id) in accessible
        ]

    async def get_one_for_admin(self, template_id: str, admin: User) -> CalendarTemplate:
        """The Teacher write-path ownership gate - the Center Admin bypasses it entirely, same reach as every
        other center-wide action."""
        template = await self.template_repository.get_by_id(template_id)
        if template is None:
            raise NotFoundException("Calendar template not found")
        if admin.role != Role.ADMINISTRATOR and template.owner_admin_id != admin.id:
            allowed = False
            if admin.role == Role.TEACHER and template.kind == TemplateKind.COHORT and template.cohort_id:
                from app.models.cohort import Cohort
                from app.services.cohort_service import accessible_stage_ids, has_cohort_access

                cohort = await Cohort.find_one({"_id": template.cohort_id, "is_deleted": False})
                allowed = bool(
                    cohort
                    and has_cohort_access(cohort, admin)
                    and template.program_level_id in accessible_stage_ids(cohort, admin)
                )
            if not allowed:
                raise ForbiddenException("This calendar template is outside your assigned Cohort stages")
        return template

    async def update(self, template_id: str, body: CalendarTemplateUpdate, admin: User) -> CalendarTemplate:
        template = await self.get_one_for_admin(template_id, admin)
        changes = body.model_dump(exclude_unset=True)
        duration_days = body.duration_days if body.duration_days is not None else template.duration_days
        rest_days = body.rest_days if body.rest_days is not None else template.rest_days
        days = body.days if body.days is not None else template.days
        try:
            _validate_schedule_shape(duration_days, rest_days, days)
        except ValueError as exc:
            raise UnprocessableException(str(exc)) from exc
        if "days" in changes:
            await self._validate_topics(
                body.days or [], template.level, template.program_id, template.program_level_id
            )
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
        await self.template_repository.deactivate_all_standard(
            template.owner_admin_id,
            template.level,
            template.program_id,
            template.program_level_id,
        )
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

    async def _validate_topics(
        self,
        days: list[CalendarDayEntryIn],
        level: Optional[Level],
        program_id: Optional[str] = None,
        program_level_id: Optional[str] = None,
    ) -> None:
        topic_ids = {tid for entry in days for tid in entry.topic_ids}
        if not topic_ids:
            return
        topics = await self.topic_repository.get_many_by_ids(list(topic_ids))
        found_ids = {t.id for t in topics}
        missing = topic_ids - found_ids
        if missing:
            raise UnprocessableException(f"Unknown topic id(s): {', '.join(sorted(missing))}")
        if program_id:
            placements = await ProgramTopic.find({
                "program_id": program_id,
                "topic_id": {"$in": list(topic_ids)},
                "status": ProgramTopicStatus.PUBLISHED.value,
                "is_deleted": False,
            }).to_list()
            allowed = {
                placement.topic_id for placement in placements
                if program_level_id is None or placement.program_level_id == program_level_id
            }
            unavailable = topic_ids - allowed
            if unavailable:
                raise UnprocessableException(
                    "A Program calendar can schedule only published Topics assigned to its Program level"
                )
        elif level is not None:
            wrong_level = [t.title for t in topics if t.level != level]
            if wrong_level:
                raise UnprocessableException(
                    f"These topics aren't in the {level.value} level: {', '.join(wrong_level)}"
                )


def get_calendar_template_service() -> CalendarTemplateService:
    return CalendarTemplateService(CalendarTemplateRepository(), TopicContentRepository(), UserRepository())
