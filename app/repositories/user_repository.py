import re
from typing import Optional

from loguru import logger
from pymongo import ReturnDocument

from app.models.user import Role, User
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow


class UserRepository(BaseRepository[User]):
    def __init__(self) -> None:
        super().__init__(User)

    async def save(self, document: User) -> User:
        # Stale profile/plan/password saves must not restore consumed email
        # tokens or overwrite a concurrently verified state.
        data = document.model_dump(by_alias=True, exclude={"id", "revision_id"})
        verification = {key: data.pop(key) for key in list(data)
                        if key.startswith("email_verification_") or key == "email_verified_at"}
        data["updated_at"] = utcnow()
        saved = await User.get_pymongo_collection().find_one_and_update(
            {"_id": document.id}, {"$set": data, "$setOnInsert": verification},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        return User.model_validate(saved)

    async def get_by_email(self, email: str) -> Optional[User]:
        user = await User.find_one(User.email == email)
        if user is None or user.is_deleted:
            return None
        return user

    async def get_administrators(self) -> list[User]:
        return await User.find(User.role == Role.ADMINISTRATOR, User.is_deleted == False).to_list()  # noqa: E712

    async def get_teachers(self) -> list[User]:
        return await User.find(User.role == Role.TEACHER, User.is_deleted == False).to_list()  # noqa: E712

    async def get_students_created_by(self, admin_id: str) -> list[User]:
        return await User.find(
            User.created_by_id == admin_id, User.role == Role.STUDENT, User.is_deleted == False  # noqa: E712
        ).to_list()

    async def get_all_students(self) -> list[User]:
        """Every Student in the center, any Teacher's - the Center Admin's own view (`Role.ADMINISTRATOR`
        bypasses the per-Teacher `created_by_id` scoping everywhere else in this file)."""
        return await User.find(User.role == Role.STUDENT, User.is_deleted == False).to_list()  # noqa: E712

    async def search_students_created_by(
        self,
        admin_id: Optional[str],
        *,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> tuple[list[User], int]:
        """Filtered, newest-first page of Students plus the TOTAL number matching - all in the database, for the
        Users dashboard. `admin_id=None` means every Student in the center (the Center Admin's view); a real id
        scopes to one Teacher's own roster. `is_active` is the Status filter (omit for both)."""
        query: dict = {"role": Role.STUDENT.value, "is_deleted": False}
        if admin_id is not None:
            query["created_by_id"] = admin_id
        if is_active is not None:
            query["is_active"] = is_active
        text = (search or "").strip()
        if text:
            pattern = re.escape(text)
            query["$or"] = [
                {"full_name": {"$regex": pattern, "$options": "i"}},
                {"email": {"$regex": pattern, "$options": "i"}},
            ]
        total = await User.find(query).count()
        cursor = User.find(query).sort(-User.created_at).skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        return await cursor.to_list(), total

    async def count_students_created_by(self, admin_id: Optional[str]) -> tuple[int, int]:
        """(total, active) - the Users dashboard's KPI cards, computed in the database rather than by loading
        every student into the app just to count them. `admin_id=None` counts the whole center."""
        base: dict = {"role": Role.STUDENT.value, "is_deleted": False}
        if admin_id is not None:
            base["created_by_id"] = admin_id
        total = await User.find(base).count()
        active = await User.find({**base, "is_active": True}).count()
        return total, active

    async def search_teachers(
        self,
        *,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> tuple[list[User], int]:
        """Filtered, newest-first page of every Teacher in the center plus the TOTAL matching - the Users
        dashboard's Teachers tab. Center-wide by design: unlike Students, there is no owner to scope by."""
        query: dict = {"role": Role.TEACHER.value, "is_deleted": False}
        if is_active is not None:
            query["is_active"] = is_active
        text = (search or "").strip()
        if text:
            pattern = re.escape(text)
            query["$or"] = [
                {"full_name": {"$regex": pattern, "$options": "i"}},
                {"email": {"$regex": pattern, "$options": "i"}},
            ]
        total = await User.find(query).count()
        cursor = User.find(query).sort(-User.created_at).skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        return await cursor.to_list(), total

    async def count_teachers(self) -> tuple[int, int]:
        """(total, active) Teachers - the Users dashboard's KPI cards."""
        base = {"role": Role.TEACHER.value, "is_deleted": False}
        total = await User.find(base).count()
        active = await User.find({**base, "is_active": True}).count()
        return total, active

    async def count_students_for_teacher(self, teacher_id: str) -> int:
        """One Teacher's own student count - the Teacher Details drawer."""
        return await User.find(
            User.created_by_id == teacher_id, User.role == Role.STUDENT, User.is_deleted == False  # noqa: E712
        ).count()

    async def get_students_assigned_to_template(self, template_id: str) -> list[User]:
        """Every student currently following this calendar template (a Pro
        student's `assigned_plan_template_id`) - `CalendarTemplateService`
        uses this to refuse deleting a template still in use.
        """
        return await User.find(
            User.assigned_plan_template_id == template_id, User.is_deleted == False  # noqa: E712
        ).to_list()

    async def get_students_assigned_to_topic(self, topic_id: str) -> list[User]:
        """Every student with `topic_id` in their `assigned_topic_ids` (Pro
        students, hand-picked plans) - the admin Topics page's "who's
        enrolled" view for a single topic. Standard students are assigned by
        `assigned_level` instead and never appear here.
        """
        return await User.find(
            User.assigned_topic_ids == topic_id, User.role == Role.STUDENT, User.is_deleted == False  # noqa: E712
        ).to_list()

    async def get_recipients_for_student(self, student: User) -> list[User]:
        """Who to notify about `student`: their owning Administrator, or nobody.

        This used to fall back to *every* Administrator when no owner resolved,
        which meant a student with no `created_by_id` (or whose owner was
        soft-deleted, or is a Root) had their name and email address emailed to
        every instructor on the platform - see
        `PasswordResetRequestService.request_reset`, which interpolates both into
        the message body.

        Fail closed instead, matching `PasswordResetRequestService.resolve`,
        which already refuses an unowned student outright. The two halves of one
        ownership rule disagreeing is what produced the leak: resolve treated
        "unowned" as nobody's, request treated it as everybody's.
        """
        if student.created_by_id is None:
            logger.warning(
                "No reset-request recipient for student {} - it has no created_by_id", student.id
            )
            return []
        owner = await self.get_by_id(student.created_by_id)
        if owner is None or owner.role not in (Role.TEACHER, Role.ADMINISTRATOR):
            logger.warning(
                "No reset-request recipient for student {} - owner {} is missing or not a teacher/administrator",
                student.id,
                student.created_by_id,
            )
            return []
        return [owner]
