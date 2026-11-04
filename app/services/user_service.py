"""Profile self-service (display name; password change is Root/Administrator
only now - see `app/api/v1/me.py`) plus the account-creation/reset methods
each privileged tier uses on the tier below it: Root creates/resets
Administrators, Administrators create Students (resetting a Student's
password is a separate flow - see `PasswordResetRequestService`, since it also
has to resolve any pending reset request). The full instructor invite flow
(Kanban #4) is still separate from all of this.

Also entitlement assignment (Kanban card #14): `assign_plan` and
`raise_session_limit` are both scoped to "a student *you* created," same
ownership rule as the password-reset flow - an Administrator acting on a
student they don't own is exactly the cross-tenant hole closed there.
"""

from datetime import datetime, time
from typing import Optional

from app.core.exceptions import ConflictException, ForbiddenException, NotFoundException, UnauthorizedException, UnprocessableException
from app.core.config import get_settings
from app.core.security import hash_password_async, verify_password_async
from app.models.audit_log import AuditLog
from app.models.calendar_template import TemplateKind
from app.models.user import PlanType, Role, User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.schema.user import AssignPlan
from app.utils.time import utcnow
from app.services.email_verification_service import get_email_verification_service


class UserService:
    def __init__(
        self,
        user_repository: UserRepository,
        refresh_token_repository: RefreshTokenRepository,
        calendar_template_repository: CalendarTemplateRepository,
        audit_log_repository: AuditLogRepository,
        topic_repository: Optional[TopicContentRepository] = None,
    ) -> None:
        self.user_repository = user_repository
        self.refresh_token_repository = refresh_token_repository
        self.calendar_template_repository = calendar_template_repository
        self.audit_log_repository = audit_log_repository
        self.topic_repository = topic_repository or TopicContentRepository()

    async def update_profile(self, user: User, full_name: Optional[str]) -> User:
        if full_name is not None:
            user.full_name = full_name
            await self.user_repository.save(user)
        return user

    async def change_password(self, user: User, current_password: str, new_password: str) -> None:
        if not await verify_password_async(current_password, user.password_hash):
            raise UnauthorizedException("Current password is incorrect")
        user.password_hash = await hash_password_async(new_password)
        await self.user_repository.save(user)
        # Every other session for this account dies with the old password.
        await self.refresh_token_repository.revoke_all_for_user(user.id)

    async def admin_create_student(self, email: str, password: str, full_name: str, created_by: User) -> User:
        if await self.user_repository.get_by_email(email) is not None:
            raise ConflictException("A user with this email already exists")
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            full_name=full_name,
            role=Role.STUDENT,
            created_by_id=created_by.id,
            email_verification_required=get_settings().EMAIL_VERIFICATION_ENABLED,
        )
        saved = await self.user_repository.save(user)
        return await get_email_verification_service().send_for_user(saved)

    async def list_students_for_admin(self, admin: User) -> list[User]:
        if admin.role == Role.ADMINISTRATOR:
            return await self.user_repository.get_all_students()
        return await self.user_repository.get_students_created_by(admin.id)

    async def search_students_for_admin(
        self,
        admin: User,
        *,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        page: int = 1,
        limit: Optional[int] = None,
    ) -> tuple[list[User], int]:
        """The Users dashboard's Students tab: a server-side page of students, filtered and searched in the
        database. The Center Admin (`Role.ADMINISTRATOR`) sees every Student in the center; a Teacher sees only
        their own roster (`created_by_id`), same restriction as every other per-student action. `limit=None`
        returns everything matched (no page param was given)."""
        skip = (page - 1) * limit if limit is not None else 0
        owner_id = None if admin.role == Role.ADMINISTRATOR else admin.id
        return await self.user_repository.search_students_created_by(
            owner_id, search=search, is_active=is_active, skip=skip, limit=limit
        )

    async def get_student_counts_for_admin(self, admin: User) -> tuple[int, int]:
        """(total, active) students - the Users dashboard's KPI cards. Center-wide for the Center Admin, own
        roster for a Teacher."""
        owner_id = None if admin.role == Role.ADMINISTRATOR else admin.id
        return await self.user_repository.count_students_created_by(owner_id)

    # --- Teachers (Administrator only - the Users dashboard's Teachers tab) ---------------------------------

    async def admin_create_teacher(self, email: str, password: str, full_name: str, created_by: User) -> User:
        if await self.user_repository.get_by_email(email) is not None:
            raise ConflictException("A user with this email already exists")
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            full_name=full_name,
            role=Role.TEACHER,
            created_by_id=created_by.id,
            email_verification_required=get_settings().EMAIL_VERIFICATION_ENABLED,
        )
        saved = await self.user_repository.save(user)
        return await get_email_verification_service().send_for_user(saved)

    async def search_teachers(
        self, *, search: Optional[str] = None, is_active: Optional[bool] = None, page: int = 1, limit: Optional[int] = None,
    ) -> tuple[list[User], int]:
        skip = (page - 1) * limit if limit is not None else 0
        return await self.user_repository.search_teachers(search=search, is_active=is_active, skip=skip, limit=limit)

    async def get_teacher_counts(self) -> tuple[int, int]:
        """(total, active) Teachers - the Users dashboard's KPI cards."""
        return await self.user_repository.count_teachers()

    async def get_teacher(self, teacher_id: str) -> User:
        teacher = await self.user_repository.get_by_id(teacher_id)
        if teacher is None or teacher.role != Role.TEACHER:
            raise NotFoundException("Teacher not found")
        return teacher

    async def get_teacher_detail_counts(self, teacher_id: str) -> tuple[int, int]:
        """(student_count, topic_count) for one Teacher - the Teacher Details drawer."""
        students = await self.user_repository.count_students_for_teacher(teacher_id)
        topics = await self.topic_repository.count_created_by(teacher_id)
        return students, topics

    async def update_teacher_email(self, teacher_id: str, new_email: str) -> User:
        teacher = await self.get_teacher(teacher_id)
        existing = await self.user_repository.get_by_email(new_email)
        if existing is not None and existing.id != teacher.id:
            raise ConflictException("A user with this email already exists")
        teacher.email = new_email
        return await self.user_repository.save(teacher)

    async def reset_teacher_password(self, teacher_id: str, new_password: str) -> User:
        teacher = await self.get_teacher(teacher_id)
        teacher.password_hash = await hash_password_async(new_password)
        saved = await self.user_repository.save(teacher)
        await self.refresh_token_repository.revoke_all_for_user(teacher.id)
        return saved

    async def set_teacher_active(self, teacher_id: str, is_active: bool) -> User:
        teacher = await self.get_teacher(teacher_id)
        teacher.is_active = is_active
        saved = await self.user_repository.save(teacher)
        if not is_active:
            await self.refresh_token_repository.revoke_all_for_user(teacher.id)
        return saved

    async def delete_teacher(self, teacher_id: str) -> None:
        """Soft delete, same convention as `delete_student` - a Teacher's own students/topics keep their
        `created_by_id` and stay intact (now manageable only by the Center Admin, since no other Teacher owns
        them); the account itself just disappears and can no longer sign in."""
        teacher = await self.get_teacher(teacher_id)
        await self.user_repository.soft_delete(teacher)
        await self.refresh_token_repository.revoke_all_for_user(teacher.id)

    async def assign_plan(self, student_id: str, admin: User, body: AssignPlan) -> User:
        student = await self.get_own_student(student_id, admin)

        if body.plan == PlanType.STANDARD:
            student.plan = PlanType.STANDARD
            student.assigned_level = body.assigned_level
            student.assigned_topic_ids = []
            student.assigned_plan_template_id = None
        else:
            assert body.assigned_plan_template_id is not None  # enforced by AssignPlan's own validator
            template = await self.calendar_template_repository.get_by_id(body.assigned_plan_template_id)
            if template is None:
                raise NotFoundException("Calendar template not found")
            if admin.role != Role.ADMINISTRATOR and template.owner_admin_id != admin.id:
                raise ForbiddenException("This calendar template belongs to a different administrator")
            if template.kind != TemplateKind.CUSTOM:
                raise UnprocessableException("Only a custom template can be assigned to a Pro student directly")
            student.plan = PlanType.PRO
            student.assigned_plan_template_id = template.id
            # Entitlement follows the template's own topic set - one source
            # of truth instead of a hand-picked list that could drift from it.
            student.assigned_topic_ids = sorted({tid for entry in template.days for tid in entry.topic_ids})
            student.assigned_level = None

        # Assigning a plan (re)starts the program - "day 1" is whichever date
        # the admin gives, or now if they don't bother backdating/scheduling
        # it. This intentionally overwrites any previous start date: a new
        # assignment is a new program, not a continuation of the old one.
        student.program_start_date = datetime.combine(body.start_date, time.min) if body.start_date else utcnow()

        return await self.user_repository.save(student)

    async def raise_session_limit(self, student_id: str, admin: User, new_limit_minutes: int) -> User:
        """Pro only, and always audited - the card is explicit this is "an
        explicit admin action," not a side effect of anything else.
        """
        student = await self.get_own_student(student_id, admin)
        if student.plan != PlanType.PRO:
            raise ConflictException("Only a Pro student's session limit can be raised")

        before = student.duration_override_minutes
        student.duration_override_minutes = new_limit_minutes
        saved = await self.user_repository.save(student)

        await self.audit_log_repository.save(
            AuditLog(
                actor=admin,
                action="raise_session_limit",
                target_type="User",
                target_id=student.id,
                before={"duration_override_minutes": before},
                after={"duration_override_minutes": new_limit_minutes},
            )
        )
        return saved

    async def get_own_student(self, student_id: str, admin: User) -> User:
        """The Teacher write-path ownership gate (teacher isolation) - an Administrator (Center Admin) bypasses
        it entirely and may manage any Student in the center, same reach as Root everywhere else."""
        student = await self.user_repository.get_by_id(student_id)
        if student is None or student.role != Role.STUDENT:
            raise NotFoundException("Student not found")
        if admin.role != Role.ADMINISTRATOR and student.created_by_id != admin.id:
            raise ForbiddenException("This student is not assigned to you")
        return student

    async def update_student_email(self, student_id: str, new_email: str, admin: User) -> User:
        """Just the email - deliberately narrow, same ownership rule as every
        other per-student admin action. `UserRepository.save` writes exactly
        the fields on this in-memory document, so nothing else changes.
        """
        student = await self.get_own_student(student_id, admin)
        existing = await self.user_repository.get_by_email(new_email)
        if existing is not None and existing.id != student.id:
            raise ConflictException("A user with this email already exists")
        student.email = new_email
        return await self.user_repository.save(student)

    async def delete_student(self, student_id: str, admin: User) -> None:
        """Soft-delete only, matching every other deletion in this codebase -
        the student's sessions/reports/submissions keep their `student_id`
        and stay intact for the owning admin's historical analytics; the
        account itself just disappears from the roster and can no longer
        sign in or hold a live session.
        """
        student = await self.get_own_student(student_id, admin)
        await self.user_repository.soft_delete(student)
        await self.refresh_token_repository.revoke_all_for_user(student.id)

    async def root_create_administrator(
        self, email: str, password: str, full_name: str, created_by: User
    ) -> User:
        if await self.user_repository.get_by_email(email) is not None:
            raise ConflictException("A user with this email already exists")
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            full_name=full_name,
            role=Role.ADMINISTRATOR,
            created_by_id=created_by.id,
            email_verification_required=get_settings().EMAIL_VERIFICATION_ENABLED,
        )
        saved = await self.user_repository.save(user)
        return await get_email_verification_service().send_for_user(saved)

    async def root_reset_administrator_password(self, admin_id: str, new_password: str) -> User:
        user = await self.user_repository.get_by_id(admin_id)
        if user is None or user.role != Role.ADMINISTRATOR:
            raise NotFoundException("Administrator not found")
        user.password_hash = await hash_password_async(new_password)
        saved = await self.user_repository.save(user)
        await self.refresh_token_repository.revoke_all_for_user(user.id)
        return saved


def get_user_service() -> UserService:
    return UserService(
        UserRepository(), RefreshTokenRepository(), CalendarTemplateRepository(), AuditLogRepository(), TopicContentRepository()
    )
