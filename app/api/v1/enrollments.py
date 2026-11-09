from fastapi import APIRouter, Depends

from app.core.security import require_role
from app.models.user import Role, User
from app.schema.enrollment import EnrollmentPublic, EnrollmentSchedule
from app.services.enrollment_service import EnrollmentService, get_enrollment_service

router = APIRouter()

require_student = require_role(Role.STUDENT)


@router.get("/enrollments/me", response_model=list[EnrollmentPublic])
async def list_my_enrollments(student: User = Depends(require_student),
                              service: EnrollmentService = Depends(get_enrollment_service)) -> list[EnrollmentPublic]:
    return await service.list_for_student(student)


@router.get("/enrollments/{enrollment_id}/schedule", response_model=EnrollmentSchedule)
async def get_enrollment_schedule(enrollment_id: str, student: User = Depends(require_student),
                                  service: EnrollmentService = Depends(get_enrollment_service)) -> EnrollmentSchedule:
    """Explicit `enrollment_id` by design - a student with more than one
    active enrollment must say which one they mean; nothing here guesses."""
    return await service.get_schedule_preview(enrollment_id, student)
