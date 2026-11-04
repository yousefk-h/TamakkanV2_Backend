"""Student-initiated, instructor-resolved password resets - students don't
self-manage their password at all (see the platform plan §3.5 and
`app/api/v1/me.py`), they ask their instructor. Distinct from `AuthService`'s
emailed-link self-service flow, which stays available to Administrators and
Root but is now refused for Student accounts.
"""

from app.core.email import EmailSender, get_email_sender
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.security import hash_password_async
from app.models.user import Role, User
from app.repositories.password_reset_request_repository import PasswordResetRequestRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository


class PasswordResetRequestService:
    def __init__(
        self,
        user_repository: UserRepository,
        request_repository: PasswordResetRequestRepository,
        email_sender: EmailSender,
        refresh_token_repository: RefreshTokenRepository,
    ) -> None:
        self.user_repository = user_repository
        self.request_repository = request_repository
        self.email_sender = email_sender
        self.refresh_token_repository = refresh_token_repository

    async def request_reset(self, student: User) -> None:
        await self.request_repository.create_for_student(student)
        for admin in await self.user_repository.get_recipients_for_student(student):
            await self.email_sender.send(
                to=admin.email,
                subject="Password reset requested",
                body=f"{student.full_name} ({student.email}) has requested a password reset.",
            )

    async def resolve(self, student_id: str, new_password: str, resolved_by: User) -> User:
        student = await self.user_repository.get_by_id(student_id)
        if student is None or student.role != Role.STUDENT:
            raise NotFoundException("Student not found")

        # A Teacher may only reset a student they created - without this,
        # `require_teacher_or_administrator` on the route is a class-level check
        # with no object-level check behind it, and any teacher could take over
        # any student on the platform (student ids are handed out in
        # `UserPublic`). The Center Admin (`Role.ADMINISTRATOR`) bypasses this,
        # same reach as every other student-management action.
        #
        # A student with no `created_by_id` is refused outright for a Teacher
        # rather than treated as "everyone's": `admin_create_student` always
        # sets it, so in practice this only affects accounts seeded outside the
        # normal flow. Refusing keeps the rule "exactly one teacher can reset a
        # student" true with no exceptions - the alternative (letting any
        # teacher reset an unowned student) is the same hole in a smaller form.
        if resolved_by.role != Role.ADMINISTRATOR and (
            student.created_by_id is None or student.created_by_id != resolved_by.id
        ):
            raise ForbiddenException("This student is not assigned to you")

        student.password_hash = await hash_password_async(new_password)
        await self.user_repository.save(student)
        # Ending the student's existing sessions is the point of the reset -
        # otherwise whoever prompted it keeps a working refresh token.
        await self.refresh_token_repository.revoke_all_for_user(student.id)
        await self.request_repository.resolve_all_for_student(student_id, resolved_by)
        return student


def get_password_reset_request_service() -> PasswordResetRequestService:
    return PasswordResetRequestService(
        UserRepository(),
        PasswordResetRequestRepository(),
        get_email_sender(),
        RefreshTokenRepository(),
    )
