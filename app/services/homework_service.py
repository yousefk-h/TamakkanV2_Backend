from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.homework import Homework
from app.models.topic_content import TopicContent
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schema.homework import HomeworkCreate
from app.services.topic_content_service import TopicContentService, get_topic_content_service
from app.utils.time import utcnow


class HomeworkService:
    def __init__(self, users: UserRepository, topics: TopicContentService) -> None:
        self.users = users
        self.topics = topics

    async def create(self, staff: User, body: HomeworkCreate) -> Homework:
        if body.due_at <= utcnow():
            raise UnprocessableException("Homework due date must be in the future")
        roster = await self.users.get_all_students() if staff.role == Role.ADMINISTRATOR else await self.users.get_students_created_by(staff.id)
        roster_ids = {student.id for student in roster}
        participant_ids = list(dict.fromkeys(body.participant_ids))
        if any(student_id not in roster_ids for student_id in participant_ids):
            raise ForbiddenException("One or more students are outside your roster")
        if body.topic_id:
            await self.topics.get_own_topic(body.topic_id, staff)
        item = Homework(created_by_id=staff.id, **body.model_dump(exclude={"participant_ids"}), participant_ids=participant_ids)
        await item.insert()
        return item

    async def list_for_staff(self, staff: User) -> list[Homework]:
        query = {"is_deleted": False}
        if staff.role != Role.ADMINISTRATOR:
            query["created_by_id"] = staff.id
        return await Homework.find(query).sort("due_at").to_list()

    async def list_for_student(self, student: User) -> list[Homework]:
        return await Homework.find({"participant_ids": student.id, "is_deleted": False}).sort("due_at").to_list()

    async def cancel(self, staff: User, homework_id: str) -> None:
        item = await Homework.find_one({"_id": homework_id, "is_deleted": False})
        if item is None or (staff.role != Role.ADMINISTRATOR and item.created_by_id != staff.id):
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
    return HomeworkService(UserRepository(), get_topic_content_service())
