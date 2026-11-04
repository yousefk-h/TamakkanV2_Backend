"""Teacher or Administrator. Creating a student, listing students (the
"Awaiting class assignment" queue is just this list filtered client-side by
`plan is None` - no separate endpoint for it), and resetting a student's
password (when they can't provide their current one to self-service it - see
`PasswordResetRequestService`). Kanban card #4 resolved instructor-creates-
outright over invite-then-signup - there's no invite/token flow here.

PROMPT-047: a Teacher sees/manages only the students they created; the
Administrator (Center Admin) sees/manages every student in the center - see
`UserService.get_own_student` for the ownership gate every per-student route
below goes through, and `UserService.search_students_for_admin` for the list.

Also entitlement assignment (Kanban card #14): `assign-plan` and
`raise-session-limit`, both scoped to "a student *you* created" (Administrator
exempted, same as every other per-student action) - see
`UserService.get_own_student`.
"""

from typing import Literal, Optional, Union

from fastapi import APIRouter, Depends, Query

from app.core.security import require_teacher_or_administrator
from app.models.user import User
from app.schema.alerts import StudentAlerts
from app.schema.user import (
    AssignPlan,
    CreateAccountRequest,
    RaiseSessionLimit,
    ResetPasswordRequest,
    StudentCounts,
    UpdateStudentEmail,
    UserPage,
    UserPublic,
)
from app.services.password_reset_request_service import (
    PasswordResetRequestService,
    get_password_reset_request_service,
)
from app.services.progress_service import ProgressService, get_progress_service
from app.services.user_service import UserService, get_user_service

router = APIRouter()


@router.post("/users", response_model=UserPublic)
async def create_student(
    body: CreateAccountRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only. Create a new Student account outright - they can
    verify their email first when enabled, with no content until a plan is assigned.
    """
    user = await service.admin_create_student(body.email, body.password, body.full_name, created_by=admin)
    return UserPublic.from_user(user)


@router.get("/users", response_model=Union[UserPage, list[UserPublic]])
async def list_students(
    search: Optional[str] = Query(None, max_length=100, description="Matches the student's name or email."),
    status: Optional[Literal["active", "inactive"]] = Query(None, description="Omit for both."),
    page: Optional[int] = Query(None, ge=1, description="1-based. Giving `page` or `limit` switches the response to a paged envelope."),
    limit: Optional[int] = Query(None, ge=1, le=100),
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> Union[UserPage, list[UserPublic]]:
    """Administrator only. List the Students you created - never another
    Administrator's. Filter this list by `plan: null` client-side for the
    "awaiting class assignment" queue; there's no separate endpoint for it.

    With no `page`/`limit` the response is the plain array it always was (other pages rely on that); `search`/`status`
    still apply. With `page` and/or `limit` it is `{items, total, page, limit}` (the Users dashboard's Students tab) -
    one page, newest first, and `total` = everything that matched the filters (not the page size), computed in the
    database.
    """
    paged = page is not None or limit is not None
    current_page, page_size = (page or 1), (limit or 20)
    is_active = {"active": True, "inactive": False}.get(status) if status else None
    students, total = await service.search_students_for_admin(
        admin, search=search, is_active=is_active, page=current_page, limit=page_size if paged else None
    )
    items = [UserPublic.from_user(student) for student in students]
    return UserPage(items=items, total=total, page=current_page, limit=page_size) if paged else items


@router.get("/users/counts", response_model=StudentCounts)
async def get_student_counts(
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> StudentCounts:
    """Administrator only. Total and active counts for your own students - the
    Users dashboard's KPI cards. Computed in the database, not by listing everyone.
    """
    total, active = await service.get_student_counts_for_admin(admin)
    return StudentCounts(total_students=total, active_students=active)


@router.get("/users/alerts", response_model=dict[str, StudentAlerts])
async def get_alerts_for_roster(
    admin: User = Depends(require_teacher_or_administrator),
    user_service: UserService = Depends(get_user_service),
    progress_service: ProgressService = Depends(get_progress_service),
) -> dict[str, StudentAlerts]:
    """Administrator only. Rollover/missed-submission alerts for every
    Student you created, keyed by student id - the roster's warning badge.
    """
    students = await user_service.list_students_for_admin(admin)
    return await progress_service.get_alerts_for_students(students)


@router.get("/users/{student_id}/alerts", response_model=StudentAlerts)
async def get_alerts_for_student(
    student_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    user_service: UserService = Depends(get_user_service),
    progress_service: ProgressService = Depends(get_progress_service),
) -> StudentAlerts:
    """Administrator only, and only for a Student you created. The student
    detail page's own "Alerts" section.
    """
    student = await user_service.get_own_student(student_id, admin)
    return await progress_service.get_alerts(student)


@router.post("/users/{student_id}/reset-password", response_model=UserPublic)
async def reset_student_password(
    student_id: str,
    body: ResetPasswordRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: PasswordResetRequestService = Depends(get_password_reset_request_service),
) -> UserPublic:
    """Administrator only, and only for a Student you created. Directly set a
    new password and resolve any pending reset request they filed.
    """
    user = await service.resolve(student_id, body.new_password, resolved_by=admin)
    return UserPublic.from_user(user)


@router.patch("/users/{student_id}/email", response_model=UserPublic)
async def update_student_email(
    student_id: str,
    body: UpdateStudentEmail,
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only, and only for a Student you created. Just the
    email - a duplicate (anyone else's, including a soft-deleted account)
    is a 409, never silently accepted.
    """
    student = await service.update_student_email(student_id, body.email, admin)
    return UserPublic.from_user(student)


@router.delete("/users/{student_id}", status_code=204)
async def delete_student(
    student_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> None:
    """Administrator only, and only for a Student you created. Soft-delete -
    the account disappears from the roster and can no longer sign in or
    start a session; every session/report/submission it already has stays
    intact for your records.
    """
    await service.delete_student(student_id, admin)


@router.post("/users/{student_id}/assign-plan", response_model=UserPublic)
async def assign_plan(
    student_id: str,
    body: AssignPlan,
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only, and only for a Student you created. Standard sets
    one `assigned_level` (their group's whole catalogue at that level); Pro
    sets a hand-picked `assigned_topic_ids` list spanning any level.
    """
    student = await service.assign_plan(student_id, admin, body)
    return UserPublic.from_user(student)


@router.post("/users/{student_id}/raise-session-limit", response_model=UserPublic)
async def raise_session_limit(
    student_id: str,
    body: RaiseSessionLimit,
    admin: User = Depends(require_teacher_or_administrator),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Administrator only, and only for a Student you created. Pro only -
    Standard's 15-minute cap is fixed, not per-student. Always writes an
    `AuditLog` entry.
    """
    student = await service.raise_session_limit(student_id, admin, body.duration_override_minutes)
    return UserPublic.from_user(student)
