"""Scheduling rules for Group classes and 1:1 coaching (Phase 3 of the
platform plan). A class is either:
- Group: any number of the admin's own Standard-plan students.
- 1:1: exactly one of the admin's own Pro-plan students.

No self-enrollment and no capacity limit - the admin picks exactly who's in
a class when scheduling it, mirroring how plan/topic assignment already
works everywhere else in this app.
"""

from datetime import datetime
from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.live_class import ClassKind, LiveClass
from app.models.user import PlanType, Role, User
from app.repositories.live_class_repository import LiveClassRepository
from app.repositories.user_repository import UserRepository
from app.utils.time import utcnow

_KIND_LABEL = {ClassKind.GROUP: "Group", ClassKind.ONE_TO_ONE: "1:1"}
_REQUIRED_PLAN = {ClassKind.GROUP: PlanType.STANDARD, ClassKind.ONE_TO_ONE: PlanType.PRO}


class LiveClassService:
    def __init__(self, repository: LiveClassRepository, user_repository: UserRepository) -> None:
        self.repository = repository
        self.user_repository = user_repository

    async def create(
        self,
        admin: User,
        kind: ClassKind,
        title: str,
        scheduled_at: datetime,
        duration_minutes: int,
        meet_link: str,
        participant_ids: list[str],
    ) -> LiveClass:
        if scheduled_at <= utcnow():
            raise UnprocessableException("Class time must be in the future")

        participants = await self._validate_participants(admin, kind, participant_ids)
        live_class = LiveClass(
            created_by_id=admin.id,
            kind=kind,
            title=title,
            scheduled_at=scheduled_at,
            duration_minutes=duration_minutes,
            meet_link=meet_link,
            participant_ids=[p.id for p in participants],
        )
        return await self.repository.save(live_class)

    async def update(
        self,
        class_id: str,
        admin: User,
        title: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        duration_minutes: Optional[int] = None,
        meet_link: Optional[str] = None,
    ) -> LiveClass:
        live_class = await self._get_owned(class_id, admin)
        if scheduled_at is not None and scheduled_at <= utcnow():
            raise UnprocessableException("Class time must be in the future")

        if title is not None:
            live_class.title = title
        if scheduled_at is not None:
            live_class.scheduled_at = scheduled_at
        if duration_minutes is not None:
            live_class.duration_minutes = duration_minutes
        if meet_link is not None:
            live_class.meet_link = meet_link
        return await self.repository.save(live_class)

    async def cancel(self, class_id: str, admin: User) -> None:
        live_class = await self._get_owned(class_id, admin)
        await self.repository.soft_delete(live_class)

    async def list_for_admin(self, admin: User) -> list[LiveClass]:
        """The Center Admin (`Role.ADMINISTRATOR`) sees every class in the center; a Teacher sees only their
        own."""
        owner_id = None if admin.role == Role.ADMINISTRATOR else admin.id
        return await self.repository.get_all_for_admin(owner_id)

    async def list_for_student(self, student: User) -> list[LiveClass]:
        return await self.repository.get_all_for_participant(student.id)

    async def _get_owned(self, class_id: str, admin: User) -> LiveClass:
        """The Teacher write-path ownership gate - the Center Admin bypasses it entirely."""
        live_class = await self.repository.get_by_id(class_id)
        if live_class is None:
            raise NotFoundException("Class not found")
        if admin.role != Role.ADMINISTRATOR and live_class.created_by_id != admin.id:
            raise NotFoundException("Class not found")
        return live_class

    async def _validate_participants(self, admin: User, kind: ClassKind, participant_ids: list[str]) -> list[User]:
        unique_ids = list(dict.fromkeys(participant_ids))  # de-dup, keep first-seen order
        if kind == ClassKind.ONE_TO_ONE and len(unique_ids) != 1:
            raise UnprocessableException("A 1:1 class needs exactly one student")

        # The Center Admin may schedule a class for any student in the center; a Teacher only their own roster.
        roster = (
            await self.user_repository.get_all_students()
            if admin.role == Role.ADMINISTRATOR
            else await self.user_repository.get_students_created_by(admin.id)
        )
        roster_by_id = {student.id: student for student in roster}

        required_plan = _REQUIRED_PLAN[kind]
        participants: list[User] = []
        for student_id in unique_ids:
            student = roster_by_id.get(student_id)
            if student is None:
                raise ForbiddenException("This student is not assigned to you")
            if student.plan != required_plan:
                raise UnprocessableException(
                    f"{student.full_name} is on the {student.plan.value if student.plan else 'no'} plan - "
                    f"a {_KIND_LABEL[kind]} class needs {required_plan.value} students"
                )
            participants.append(student)
        return participants


def get_live_class_service() -> LiveClassService:
    return LiveClassService(LiveClassRepository(), UserRepository())
