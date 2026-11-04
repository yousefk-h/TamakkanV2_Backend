"""Center Admin only (PROMPT-047): the Users dashboard's Teachers tab. A
Teacher cannot create, list or manage another Teacher - only the
Administrator (Center Admin) reaches any route here, same flat/exact-match
role check as everywhere else in this codebase.

Add Teacher is deliberately as narrow as `POST /root/admins` (full name,
email, password) - no invitation/onboarding flow, same simplification Root's
own Administrator-creation already makes.
"""

from typing import Literal, Optional, Union

from fastapi import APIRouter, Depends, Query

from app.core.security import require_administrator
from app.models.user import User
from app.schema.user import (
    CreateAccountRequest,
    ResetPasswordRequest,
    SetTeacherActive,
    TeacherCounts,
    TeacherDetail,
    UpdateTeacherEmail,
    UserPage,
    UserPublic,
)
from app.services.user_service import UserService, get_user_service

router = APIRouter()


@router.post("/teachers", response_model=UserPublic)
async def create_teacher(
    body: CreateAccountRequest,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only. Create a new Teacher account outright - same shape as every other account-creation
    route in this codebase (full name, email, a password set directly, no invite link)."""
    teacher = await service.admin_create_teacher(body.email, body.password, body.full_name, created_by=admin)
    return UserPublic.from_user(teacher)


@router.get("/teachers", response_model=Union[UserPage, list[UserPublic]])
async def list_teachers(
    search: Optional[str] = Query(None, max_length=100, description="Matches the teacher's name or email."),
    status: Optional[Literal["active", "inactive"]] = Query(None, description="Omit for both."),
    page: Optional[int] = Query(None, ge=1, description="1-based. Giving `page` or `limit` switches the response to a paged envelope."),
    limit: Optional[int] = Query(None, ge=1, le=100),
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> Union[UserPage, list[UserPublic]]:
    """Administrator only. Every Teacher in the center - there is no owner to scope by, unlike Students. Same
    paged-envelope contract as `GET /users`: with no `page`/`limit` the response is the plain array; with either,
    it is `{items, total, page, limit}`, computed in the database."""
    paged = page is not None or limit is not None
    current_page, page_size = (page or 1), (limit or 20)
    is_active = {"active": True, "inactive": False}.get(status) if status else None
    teachers, total = await service.search_teachers(
        search=search, is_active=is_active, page=current_page, limit=page_size if paged else None
    )
    items = [UserPublic.from_user(teacher) for teacher in teachers]
    return UserPage(items=items, total=total, page=current_page, limit=page_size) if paged else items


@router.get("/teachers/counts", response_model=TeacherCounts)
async def get_teacher_counts(
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> TeacherCounts:
    """Administrator only. Total and active counts for every Teacher in the center - the Users dashboard's KPI
    cards. Computed in the database, not by listing everyone."""
    total, active = await service.get_teacher_counts()
    return TeacherCounts(total_teachers=total, active_teachers=active)


@router.get("/teachers/{teacher_id}", response_model=TeacherDetail)
async def get_teacher(
    teacher_id: str,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> TeacherDetail:
    """Administrator only. One Teacher's account plus their own student/topic counts - the Teacher Details
    drawer."""
    teacher = await service.get_teacher(teacher_id)
    student_count, topic_count = await service.get_teacher_detail_counts(teacher_id)
    return TeacherDetail(teacher=UserPublic.from_user(teacher), student_count=student_count, topic_count=topic_count)


@router.patch("/teachers/{teacher_id}/email", response_model=UserPublic)
async def update_teacher_email(
    teacher_id: str,
    body: UpdateTeacherEmail,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only. Just the email - a duplicate (anyone else's, including a soft-deleted account) is a
    409, never silently accepted."""
    teacher = await service.update_teacher_email(teacher_id, body.email)
    return UserPublic.from_user(teacher)


@router.post("/teachers/{teacher_id}/reset-password", response_model=UserPublic)
async def reset_teacher_password(
    teacher_id: str,
    body: ResetPasswordRequest,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only. Directly set a new password for a Teacher - e.g. when they're locked out."""
    teacher = await service.reset_teacher_password(teacher_id, body.new_password)
    return UserPublic.from_user(teacher)


@router.patch("/teachers/{teacher_id}/active", response_model=UserPublic)
async def set_teacher_active(
    teacher_id: str,
    body: SetTeacherActive,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only. Activate/deactivate a Teacher's account - deactivating revokes every existing
    session, same as a password reset, and does not touch their students/topics."""
    teacher = await service.set_teacher_active(teacher_id, body.is_active)
    return UserPublic.from_user(teacher)


@router.delete("/teachers/{teacher_id}", status_code=204)
async def delete_teacher(
    teacher_id: str,
    admin: User = Depends(require_administrator),
    service: UserService = Depends(get_user_service),
) -> None:
    """Administrator only. Soft-delete - the account disappears from the Teachers tab and can no longer sign
    in; their students/topics keep their `created_by_id` and stay intact, manageable only by the Center Admin
    from then on."""
    await service.delete_teacher(teacher_id)
