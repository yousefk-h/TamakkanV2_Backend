from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.cohort import Cohort
from app.models.homework import Homework
from app.models.topic_content import TopicContent
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.user import PlanType
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schema.homework import HomeworkCreate
from app.services.cohort_service import CohortService, get_cohort_service, has_cohort_access
from app.services.topic_content_service import TopicContentService, get_topic_content_service
from app.utils.time import utcnow


class HomeworkService:
    def __init__(self, users: UserRepository, topics: TopicContentService, cohorts: CohortService) -> None:
        self.users = users
        self.topics = topics
        self.cohorts = cohorts

    async def _resolve_targets(self, staff: User, body: HomeworkCreate):
        """Returns `(participants, participant_program_context,
        participant_enrollment_ids, program_id, program_level_id)` for either
        targeting mode. Cohort targeting is scoped to the stages the CALLER
        themselves can reach in that Cohort (`CohortService.get_roster`
        already enforces this - a Lead Teacher/Admin gets every stage, a
        stage-scoped additional teacher only theirs), never the whole Cohort
        regardless of the caller's own assignment."""
        if body.cohort_id:
            cohort = await self.cohorts.get_for_user(body.cohort_id, staff)
            roster = await self.cohorts.get_roster(body.cohort_id, staff)
            if body.program_level_id:
                roster = [(e, s) for e, s in roster if e.program_level_id == body.program_level_id]
            targeted = [(e, s) for e, s in roster if s is not None]
            if not targeted:
                raise UnprocessableException("No students you can reach are currently enrolled in that Cohort/stage")
            participants = [s for _, s in targeted]
            participant_program_context = {s.id: f"{cohort.program_id}:{e.program_level_id}" for e, s in targeted}
            participant_enrollment_ids = {s.id: e.id for e, s in targeted}
            return participants, participant_program_context, participant_enrollment_ids, cohort.program_id, body.program_level_id

        roster = await self.users.get_all_students() if staff.role == Role.ADMINISTRATOR else await self.users.get_students_created_by(staff.id)
        roster_ids = {student.id for student in roster}
        participant_ids = list(dict.fromkeys(body.participant_ids))
        if any(student_id not in roster_ids for student_id in participant_ids):
            raise ForbiddenException("One or more students are outside your roster")
        participants = [student for student in roster if student.id in participant_ids]
        participant_program_context = {
            student.id: f"{student.program_id or ''}:{student.program_level_id or ''}" for student in participants
        }
        return participants, participant_program_context, {}, None, None

    async def create(self, staff: User, body: HomeworkCreate) -> Homework:
        if body.due_at <= utcnow():
            raise UnprocessableException("Homework due date must be in the future")
        participants, participant_program_context, participant_enrollment_ids, program_id, program_level_id = (
            await self._resolve_targets(staff, body)
        )
        participant_ids = [student.id for student in participants]
        if body.topic_id:
            await self.topics.get_own_topic(body.topic_id, staff)
            for student in participants:
                # Cohort-targeted students resolve their Program/stage from the
                # Enrollment just used (`participant_program_context`), never
                # the legacy `User` fields, which a Cohort-only student may
                # never have had set at all.
                target_program_id, _, target_level_id = participant_program_context.get(student.id, "").partition(":")
                if target_program_id:
                    placement = await ProgramTopic.find_one({
                        "program_id": target_program_id,
                        "topic_id": body.topic_id,
                        "status": ProgramTopicStatus.PUBLISHED.value,
                        "is_deleted": False,
                    })
                    if placement is None or (target_level_id and placement.program_level_id != target_level_id):
                        raise UnprocessableException(
                            "The selected lesson is not published in every recipient's Program level"
                        )
                elif student.plan == PlanType.PRO and body.topic_id not in student.assigned_topic_ids:
                    raise UnprocessableException(
                        "The selected lesson is not in every recipient's assigned schedule"
                    )
        item = Homework(
            created_by_id=staff.id,
            **body.model_dump(exclude={"participant_ids", "cohort_id", "program_level_id"}),
            participant_ids=participant_ids,
            participant_program_context=participant_program_context,
            participant_enrollment_ids=participant_enrollment_ids,
            cohort_id=body.cohort_id,
            program_id=program_id,
            program_level_id=program_level_id,
        )
        await item.insert()
        return item

    async def list_for_staff(self, staff: User) -> list[Homework]:
        if staff.role == Role.ADMINISTRATOR:
            return await Homework.find({"is_deleted": False}).sort("due_at").to_list()
        own = await Homework.find({"is_deleted": False, "created_by_id": staff.id}).to_list()
        cohort_ids = [c.id for c in await self.cohorts.list_for_teacher(staff)]
        shared = (
            await Homework.find({"is_deleted": False, "cohort_id": {"$in": cohort_ids}}).to_list()
            if cohort_ids else []
        )
        combined = {item.id: item for item in [*own, *shared]}
        return sorted(combined.values(), key=lambda item: item.due_at)

    async def list_for_student(self, student: User) -> list[Homework]:
        return await Homework.find({"participant_ids": student.id, "is_deleted": False}).sort("due_at").to_list()

    async def cancel(self, staff: User, homework_id: str) -> None:
        item = await Homework.find_one({"_id": homework_id, "is_deleted": False})
        if item is None:
            raise NotFoundException("Homework not found")
        if staff.role != Role.ADMINISTRATOR and item.created_by_id != staff.id:
            cohort = await Cohort.find_one({"_id": item.cohort_id, "is_deleted": False}) if item.cohort_id else None
            if cohort is None or not has_cohort_access(cohort, staff):
                raise NotFoundException("Homework not found")
        item.is_deleted = True
        await item.save()

    @staticmethod
    async def topic_titles(items: list[Homework]) -> dict[str, str]:
        ids = list({item.topic_id for item in items if item.topic_id})
        if not ids:
            return {}
        topics = await TopicContent.find({"_id": {"$in": ids}, "is_deleted": False}).to_list()
        return {topic.id: topic.title for topic in topics}


def get_homework_service() -> HomeworkService:
    return HomeworkService(UserRepository(), get_topic_content_service(), get_cohort_service())
