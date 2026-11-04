from fastapi import APIRouter, Depends

from app.core.security import get_current_user, require_role
from app.models.user import Role, User
from app.schema.challenge import ChallengeCalendar
from app.schema.common import MessageResponse
from app.schema.progress import StudentProgress
from app.schema.user import ChangePassword, UpdateMe, UserPublic
from app.services.password_reset_request_service import (
    PasswordResetRequestService,
    get_password_reset_request_service,
)
from app.services.progress_service import ProgressService, get_progress_service
from app.services.user_service import UserService, get_user_service

router = APIRouter()

# Self-service change-password needs the current password, which is exactly
# what a Student who forgot theirs doesn't have - and per the platform plan
# §3.5, Students don't self-manage their password at all anyway. They use
# `request_password_reset` below instead.
require_non_student = require_role(Role.ROOT, Role.ADMINISTRATOR, Role.TEACHER)


@router.get("/me", response_model=UserPublic)
async def get_me(user: User = Depends(get_current_user)) -> UserPublic:
    """Return the authenticated caller's own profile."""
    return UserPublic.from_user(user)


@router.patch("/me", response_model=UserPublic)
async def update_me(
    body: UpdateMe,
    user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Update the authenticated caller's own display name."""
    updated = await service.update_profile(user, body.full_name)
    return UserPublic.from_user(updated)


@router.post("/me/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePassword,
    user: User = Depends(require_non_student),
    service: UserService = Depends(get_user_service),
) -> MessageResponse:
    """Root/Administrator/Teacher only. Change your own password given the
    current one. Students don't self-manage a password at all - see
    `POST /me/request-password-reset` instead.
    """
    await service.change_password(user, body.current_password, body.new_password)
    return MessageResponse(detail="Password changed.")


@router.get("/me/progress", response_model=StudentProgress)
async def get_my_progress(
    student: User = Depends(require_role(Role.STUDENT)),
    service: ProgressService = Depends(get_progress_service),
) -> StudentProgress:
    """Student only. Real streak/badges/challenge-day, computed from this
    student's own sessions and check-ins - see `ProgressService`.
    """
    return await service.get_progress(student)


@router.get("/me/challenge-calendar", response_model=ChallengeCalendar)
async def get_my_challenge_calendar(
    student: User = Depends(require_role(Role.STUDENT)),
    service: ProgressService = Depends(get_progress_service),
) -> ChallengeCalendar:
    """Student only. Which lesson topic occupies each day of the 30-day
    challenge, with real done/current/missed/locked state - see
    `ProgressService.get_lesson_schedule`.
    """
    return await service.get_lesson_schedule(student)


@router.post("/me/request-password-reset", response_model=MessageResponse)
async def request_password_reset(
    student: User = Depends(require_role(Role.STUDENT)),
    service: PasswordResetRequestService = Depends(get_password_reset_request_service),
) -> MessageResponse:
    """Student only. Notifies your instructor that you need a password
    reset - they resolve it via `POST /users/{student_id}/reset-password`.
    """
    await service.request_reset(student)
    return MessageResponse(detail="Your instructor has been notified.")
