from app.models.password_reset_request import PasswordResetRequest
from app.models.user import User
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow


class PasswordResetRequestRepository(BaseRepository[PasswordResetRequest]):
    def __init__(self) -> None:
        super().__init__(PasswordResetRequest)

    async def create_for_student(self, student: User) -> PasswordResetRequest:
        request = PasswordResetRequest(student=student, student_id=student.id)
        return await self.save(request)

    async def get_pending_for_student(self, student_id: str) -> list[PasswordResetRequest]:
        return await PasswordResetRequest.find(
            PasswordResetRequest.student_id == student_id,
            PasswordResetRequest.resolved == False,  # noqa: E712
        ).to_list()

    async def resolve_all_for_student(self, student_id: str, resolved_by: User) -> None:
        for request in await self.get_pending_for_student(student_id):
            request.resolved = True
            request.resolved_by = resolved_by
            request.resolved_at = utcnow()
            await self.save(request)
