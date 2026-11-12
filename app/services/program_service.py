from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.calendar_template import CalendarTemplate, TemplateKind
from app.models.level_profile import LevelProfile
from app.models.program import Program, ProgramLevelDefinition
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.core.interactive_video import validate_interactive_video_ready
from app.models.topic_content import LessonFlow, Level, TopicContent
from app.models.user import Role, User
from app.schema.program import ProgramWrite
from app.schema.program_topic import ProgramTopicUpdate, ProgramTopicWrite


class ProgramService:
    async def list_active(self) -> list[Program]:
        return await Program.find({"is_deleted": False, "is_active": True}).sort("name").to_list()

    async def get_active(self, program_id: str) -> Program:
        item = await Program.find_one({"_id": program_id, "is_deleted": False, "is_active": True})
        if item is None:
            raise NotFoundException("Program not found")
        return item

    async def _validate_profiles(self, ids: list[str]) -> list[str]:
        unique = list(dict.fromkeys(ids))
        if not unique:
            return []
        count = await LevelProfile.find({"_id": {"$in": unique}, "is_deleted": False, "is_active": True}).count()
        if count != len(unique):
            raise UnprocessableException("One or more center levels are unavailable")
        return unique

    async def create(self, body: ProgramWrite, admin: User) -> Program:
        data = body.model_dump()
        data["base_levels"] = list(dict.fromkeys(data["base_levels"]))
        data["level_profile_ids"] = await self._validate_profiles(data["level_profile_ids"])
        data["levels"] = await self._normalize_levels(body)
        item = Program(**data, created_by_id=admin.id)
        await item.insert()
        return item

    async def update(self, program_id: str, body: ProgramWrite) -> Program:
        item = await self.get_active(program_id)
        data = body.model_dump()
        data["base_levels"] = list(dict.fromkeys(data["base_levels"]))
        data["level_profile_ids"] = await self._validate_profiles(data["level_profile_ids"])
        data["levels"] = await self._normalize_levels(body)
        next_level_ids = {level.id for level in data["levels"]}
        used = await ProgramTopic.find({
            "program_id": program_id,
            "is_deleted": False,
            "program_level_id": {"$ne": None, "$nin": list(next_level_ids)},
        }).count()
        if used:
            raise UnprocessableException(
                "Move or remove Topics from Program levels before deleting those levels"
            )
        for key, value in data.items():
            setattr(item, key, value)
        await item.save()
        return item

    async def archive(self, program_id: str) -> None:
        item = await self.get_active(program_id)
        item.is_active = False
        item.is_deleted = True
        await item.save()

    async def _topic_for_write(self, topic_id: str, caller: User) -> TopicContent:
        topic = await TopicContent.find_one({"_id": topic_id, "is_deleted": False})
        if topic is None:
            raise NotFoundException("Topic not found")
        if caller.role == Role.TEACHER and topic.created_by_id not in (None, caller.id):
            raise ForbiddenException("You can only assign topics you authored")
        return topic

    async def _normalize_levels(self, body: ProgramWrite) -> list[ProgramLevelDefinition]:
        levels = list(body.levels)
        if not levels and body.level_profile_ids:
            profiles = await LevelProfile.find({
                "_id": {"$in": body.level_profile_ids}, "is_deleted": False, "is_active": True
            }).sort("order_index").to_list()
            levels = [
                ProgramLevelDefinition(
                    id=profile.id,
                    name=profile.name,
                    order_index=index,
                    difficulty_level=profile.base_level,
                    level_profile_id=profile.id,
                )
                for index, profile in enumerate(profiles)
            ]
        if not levels:
            levels = [
                ProgramLevelDefinition(id=level.value, name=level.value.title(), order_index=index, difficulty_level=level)
                for index, level in enumerate(dict.fromkeys(body.base_levels))
            ]
        if len({level.id for level in levels}) != len(levels):
            raise UnprocessableException("Program level ids must be unique")
        for level in levels:
            if level.level_profile_id:
                if level.level_profile_id not in body.level_profile_ids:
                    raise UnprocessableException("A Program level references a center level that is not enabled")
                profile = await LevelProfile.find_one({"_id": level.level_profile_id, "is_deleted": False, "is_active": True})
                if profile is None or profile.base_level != level.difficulty_level:
                    raise UnprocessableException("Program level difficulty does not match its center level mapping")
        return sorted(levels, key=lambda value: value.order_index)

    @staticmethod
    def effective_levels(program: Program) -> list[ProgramLevelDefinition]:
        return program.levels or [
            ProgramLevelDefinition(id=level.value, name=level.value.title(), order_index=index, difficulty_level=level)
            for index, level in enumerate(program.base_levels)
        ]

    def _resolve_program_level(self, program: Program, program_level_id: str | None, legacy_level: Level | None) -> ProgramLevelDefinition:
        levels = self.effective_levels(program)
        if program_level_id:
            match = next((level for level in levels if level.id == program_level_id), None)
        else:
            match = next((level for level in levels if level.difficulty_level == legacy_level), None)
        if match is None:
            raise UnprocessableException("The selected Program level is unavailable")
        return match

    async def list_topics(self, program_id: str) -> list[tuple[ProgramTopic, TopicContent]]:
        await self.get_active(program_id)
        items = await ProgramTopic.find({"program_id": program_id, "is_deleted": False}).sort(
            [("program_level_id", 1), ("created_at", 1)]
        ).to_list()
        topics = await TopicContent.find({"_id": {"$in": [item.topic_id for item in items]}, "is_deleted": False}).to_list()
        by_id = {topic.id: topic for topic in topics}
        return [(item, by_id[item.topic_id]) for item in items if item.topic_id in by_id]

    async def list_assignments_for_topic(self, topic_id: str, caller: User) -> list[ProgramTopic]:
        await self._topic_for_write(topic_id, caller)
        return await ProgramTopic.find({"topic_id": topic_id, "is_deleted": False}).sort("created_at").to_list()

    async def assign_topic(self, program_id: str, body: ProgramTopicWrite, caller: User) -> ProgramTopic:
        program = await self.get_active(program_id)
        topic = await self._topic_for_write(body.topic_id, caller)
        program_level = self._resolve_program_level(program, body.program_level_id, body.level)
        existing = await ProgramTopic.find_one({"program_id": program_id, "topic_id": body.topic_id})
        if existing and not existing.is_deleted:
            return await self.update_topic(program_id, body.topic_id, ProgramTopicUpdate(program_level_id=program_level.id, status=body.status), caller)
        if existing:
            existing.program_level_id = program_level.id
            existing.level = program_level.difficulty_level
            existing.level_profile_id = program_level.level_profile_id
            existing.status = body.status
            existing.is_deleted = False
            existing.assigned_by_id = caller.id
            if body.status == ProgramTopicStatus.PUBLISHED:
                existing.published_revision = await self._pin_current_revision(topic)
            await existing.save()
            await self._ensure_program_calendar(program, program_level)
            return existing
        item = ProgramTopic(
            program_id=program_id,
            topic_id=body.topic_id,
            program_level_id=program_level.id,
            level=program_level.difficulty_level,
            level_profile_id=program_level.level_profile_id,
            status=body.status,
            assigned_by_id=caller.id,
            published_revision=await self._pin_current_revision(topic) if body.status == ProgramTopicStatus.PUBLISHED else None,
        )
        await item.insert()
        await self._ensure_program_calendar(program, program_level)
        return item

    async def update_topic(self, program_id: str, topic_id: str, body: ProgramTopicUpdate, caller: User) -> ProgramTopic:
        program = await self.get_active(program_id)
        topic = await self._topic_for_write(topic_id, caller)
        item = await ProgramTopic.find_one({"program_id": program_id, "topic_id": topic_id, "is_deleted": False})
        if item is None:
            raise NotFoundException("Topic is not assigned to this program")
        changes = body.model_dump(exclude_unset=True)
        previous_program_level_id = item.program_level_id
        program_level = self._resolve_program_level(
            program,
            changes.get("program_level_id", item.program_level_id),
            changes.get("level", item.level),
        )
        item.program_level_id = program_level.id
        item.level = program_level.difficulty_level
        item.level_profile_id = program_level.level_profile_id
        if "status" in changes:
            item.status = changes["status"]
            item.published_revision = await self._pin_current_revision(topic) if item.status == ProgramTopicStatus.PUBLISHED else None
        await item.save()
        if previous_program_level_id != item.program_level_id or item.status != ProgramTopicStatus.PUBLISHED:
            # A moved or unpublished Topic must not remain reachable through
            # a stale day in the old stage's authoritative Calendar.
            await self._remove_from_program_calendars(program.id, topic_id)
        await self._ensure_program_calendar(program, program_level)
        return item

    async def remove_topic(self, program_id: str, topic_id: str, caller: User) -> None:
        program = await self.get_active(program_id)
        await self._topic_for_write(topic_id, caller)
        item = await ProgramTopic.find_one({"program_id": program_id, "topic_id": topic_id, "is_deleted": False})
        if item is None:
            raise NotFoundException("Topic is not assigned to this program")
        item.is_deleted = True
        await item.save()
        await self._remove_from_program_calendars(program.id, topic_id)

    async def _pin_current_revision(self, topic: TopicContent) -> int:
        if topic.lesson_flow == LessonFlow.INTERACTIVE_VIDEO:
            validate_interactive_video_ready(topic)
        if not any(snapshot.revision == topic.content_revision for snapshot in topic.revision_snapshots):
            topic.revision_snapshots.append(topic.make_revision_snapshot())
            await topic.save()
        return topic.content_revision

    async def _ensure_program_calendar(self, program: Program, program_level: ProgramLevelDefinition) -> None:
        template = await CalendarTemplate.find_one({
            "program_id": program.id,
            "program_level_id": program_level.id,
            "kind": TemplateKind.STANDARD.value,
            "is_deleted": False,
        })
        # Adopt a calendar created before Program levels had stable ids.
        if template is None:
            legacy = await CalendarTemplate.find({
                "program_id": program.id,
                "program_level_id": None,
                "level": program_level.difficulty_level.value,
                "kind": TemplateKind.STANDARD.value,
                "is_deleted": False,
            }).to_list()
            if len(legacy) == 1:
                template = legacy[0]
                template.program_level_id = program_level.id
                template.name = f"{program.name} · {program_level.name}"
                await template.save()
        if template is None:
            await CalendarTemplate(
                name=f"{program.name} · {program_level.name}",
                kind=TemplateKind.STANDARD,
                owner_admin_id=program.created_by_id,
                program_id=program.id,
                program_level_id=program_level.id,
                level=program_level.difficulty_level,
                is_active=True,
                duration_days=30,
                rest_days=[],
                days=[],
            ).insert()

    async def _remove_from_program_calendars(self, program_id: str, topic_id: str) -> None:
        """Detaching reusable content removes only this Program's schedule
        references. The Topic and every other Program remain untouched."""
        templates = await CalendarTemplate.find({
            "program_id": program_id,
            "is_deleted": False,
        }).to_list()
        for template in templates:
            changed = False
            for entry in template.days:
                if topic_id in entry.topic_ids:
                    entry.topic_ids = [value for value in entry.topic_ids if value != topic_id]
                    entry.custom_minutes.pop(topic_id, None)
                    changed = True
            if changed:
                await template.save()


def get_program_service() -> ProgramService:
    return ProgramService()
