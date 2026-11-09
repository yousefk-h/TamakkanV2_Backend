from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import UnprocessableException
from app.core.security import get_current_user, require_administrator, require_teacher_or_administrator
from app.models.program_topic import ProgramTopicStatus
from app.models.user import Role, User
from app.schema.cohort import CohortCreate, CohortPublic, CohortUpdate
from app.schema.enrollment import (
    BulkEnrollmentResult, CohortStudentCandidatePage, EnrollmentPage,
    EnrollmentPublic, EnrollStudentsRequest,
)
from app.schema.program_topic import ProgramTopicPublic, ProgramTopicWrite
from app.services.cohort_service import CohortService, accessible_stage_ids, get_cohort_service
from app.services.program_service import ProgramService, get_program_service

router = APIRouter()


async def _public_cohort(cohort, student_count: int = 0, stages: list[str] | None = None) -> CohortPublic:
    teacher = await User.find_one({"_id": cohort.lead_teacher_id, "is_deleted": False})
    return CohortPublic.from_cohort(
        cohort, student_count, stages,
        lead_teacher_name=teacher.full_name if teacher else None,
    )


@router.post("/cohorts", response_model=CohortPublic, status_code=201)
async def create_cohort(body: CohortCreate, admin: User = Depends(require_administrator),
                        service: CohortService = Depends(get_cohort_service)) -> CohortPublic:
    cohort = await service.create(body, admin)
    return await _public_cohort(cohort)


@router.get("/cohorts", response_model=list[CohortPublic])
async def list_cohorts(caller: User = Depends(require_teacher_or_administrator),
                       service: CohortService = Depends(get_cohort_service)) -> list[CohortPublic]:
    """The Center Admin sees every Cohort in the center; a Teacher sees only
    the Cohorts they lead or are assigned to - same reach pattern as every
    other roster-scoped list in this API."""
    cohorts = await service.list_for_admin() if caller.role == Role.ADMINISTRATOR else await service.list_for_teacher(caller)
    roster_counts = {}
    for cohort in cohorts:
        roster = await service.get_roster(cohort.id, caller)
        roster_counts[cohort.id] = len(roster)
    return [
        await _public_cohort(c, roster_counts.get(c.id, 0), accessible_stage_ids(c, caller))
        for c in cohorts
    ]


@router.get("/cohorts/{cohort_id}", response_model=CohortPublic)
async def get_cohort(cohort_id: str, caller: User = Depends(require_teacher_or_administrator),
                     service: CohortService = Depends(get_cohort_service)) -> CohortPublic:
    cohort = await service.get_for_user(cohort_id, caller)
    roster = await service.get_roster(cohort_id, caller)
    return await _public_cohort(cohort, len(roster), accessible_stage_ids(cohort, caller))


@router.patch("/cohorts/{cohort_id}", response_model=CohortPublic)
async def update_cohort(cohort_id: str, body: CohortUpdate, admin: User = Depends(require_teacher_or_administrator),
                        service: CohortService = Depends(get_cohort_service)) -> CohortPublic:
    cohort = await service.update(cohort_id, body, admin)
    return await _public_cohort(cohort)


@router.delete("/cohorts/{cohort_id}", status_code=204)
async def archive_cohort(cohort_id: str, _admin: User = Depends(require_administrator),
                         service: CohortService = Depends(get_cohort_service)) -> None:
    await service.archive(cohort_id)


@router.get("/cohorts/{cohort_id}/roster", response_model=list[EnrollmentPublic])
async def get_cohort_roster(cohort_id: str, caller: User = Depends(require_teacher_or_administrator),
                            service: CohortService = Depends(get_cohort_service)) -> list[EnrollmentPublic]:
    roster = await service.get_roster(cohort_id, caller)
    return [
        EnrollmentPublic.from_enrollment(
            enrollment,
            student_name=student.full_name if student else None,
            student_email=student.email if student else None,
        )
        for enrollment, student in roster
    ]


@router.get("/cohorts/{cohort_id}/roster/page", response_model=EnrollmentPage)
async def get_cohort_roster_page(
    cohort_id: str, search: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
    caller: User = Depends(require_teacher_or_administrator),
    service: CohortService = Depends(get_cohort_service),
) -> EnrollmentPage:
    roster, total = await service.get_roster_page(cohort_id, caller, search=search, page=page, limit=limit)
    return EnrollmentPage(items=[
        EnrollmentPublic.from_enrollment(item, student_name=student.full_name if student else None,
                                         student_email=student.email if student else None)
        for item, student in roster
    ], total=total, page=page, limit=limit)


@router.get("/cohorts/{cohort_id}/student-candidates", response_model=CohortStudentCandidatePage)
async def get_cohort_student_candidates(
    cohort_id: str, search: Optional[str] = Query(None, max_length=100),
    academic_level_id: Optional[str] = Query(None), unassigned_only: bool = Query(False),
    page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
    caller: User = Depends(require_teacher_or_administrator), service: CohortService = Depends(get_cohort_service),
) -> CohortStudentCandidatePage:
    items, total = await service.list_student_candidates(
        cohort_id, caller, search=search, academic_level_id=academic_level_id,
        unassigned_only=unassigned_only, page=page, limit=limit,
    )
    return CohortStudentCandidatePage(items=items, total=total, page=page, limit=limit)


@router.post("/cohorts/{cohort_id}/students", response_model=list[EnrollmentPublic], status_code=201)
async def enroll_students(cohort_id: str, body: EnrollStudentsRequest, admin: User = Depends(require_teacher_or_administrator),
                          service: CohortService = Depends(get_cohort_service)) -> list[EnrollmentPublic]:
    _created, _already_enrolled, enrollments = await service.enroll_students(cohort_id, body, admin)
    # Historical route: keep its plain-list response exactly as older players
    # and integrations expect.  New Batch UX uses /students/bulk below to
    # report skipped duplicate selections explicitly.
    return [EnrollmentPublic.from_enrollment(e) for e in enrollments]


@router.post("/cohorts/{cohort_id}/students/bulk", response_model=BulkEnrollmentResult, status_code=201)
async def enroll_students_bulk(cohort_id: str, body: EnrollStudentsRequest, admin: User = Depends(require_teacher_or_administrator),
                               service: CohortService = Depends(get_cohort_service)) -> BulkEnrollmentResult:
    enrollments, already_enrolled, _processed = await service.enroll_students(cohort_id, body, admin)
    return BulkEnrollmentResult(created=[EnrollmentPublic.from_enrollment(e) for e in enrollments],
                                already_enrolled_student_ids=already_enrolled)


@router.delete("/cohorts/{cohort_id}/students/{student_id}", status_code=204)
async def remove_student(cohort_id: str, student_id: str, _admin: User = Depends(require_administrator),
                         service: CohortService = Depends(get_cohort_service)) -> None:
    await service.remove_student(cohort_id, student_id)


@router.get("/cohorts/{cohort_id}/topics", response_model=list[ProgramTopicPublic])
async def list_cohort_topics(
    cohort_id: str,
    caller: User = Depends(require_teacher_or_administrator),
    cohort_service: CohortService = Depends(get_cohort_service),
    program_service: ProgramService = Depends(get_program_service),
) -> list[ProgramTopicPublic]:
    """Published lessons this caller may teach through this Cohort - scoped
    to the Cohort's stages this caller is actually assigned to, and to
    PUBLISHED placements only. This is read access for teaching, not
    authoring: it never grants edit rights on the underlying Topic."""
    cohort = await cohort_service.get_for_user(cohort_id, caller)
    stage_ids = set(accessible_stage_ids(cohort, caller))
    items = await program_service.list_topics(cohort.program_id)
    return [
        ProgramTopicPublic.from_assignment(placement, topic.title)
        for placement, topic in items
        if placement.status == ProgramTopicStatus.PUBLISHED and placement.program_level_id in stage_ids
    ]


@router.post("/cohorts/{cohort_id}/topics", response_model=ProgramTopicPublic, status_code=201)
async def assign_cohort_topic(
    cohort_id: str,
    body: ProgramTopicWrite,
    caller: User = Depends(require_teacher_or_administrator),
    cohort_service: CohortService = Depends(get_cohort_service),
    program_service: ProgramService = Depends(get_program_service),
) -> ProgramTopicPublic:
    """Reuse a library Topic in one of this caller's assigned Batch stages.

    Scheduling remains a separate Calendar operation. The endpoint is
    Cohort-scoped so a Teacher cannot use the generic Program API to modify a
    stage they do not teach.
    """
    cohort = await cohort_service.get_for_user(cohort_id, caller)
    if body.program_level_id not in accessible_stage_ids(cohort, caller):
        raise UnprocessableException("Choose a Program stage assigned to you in this Cohort")

    existing_items = await program_service.list_topics(cohort.program_id)
    existing = next((item for item, _topic in existing_items if item.topic_id == body.topic_id), None)
    if existing is not None and existing.program_level_id != body.program_level_id:
        raise UnprocessableException(
            "This Topic is already used in another stage of the Program; move it from the Program workspace"
        )

    placement = await program_service.assign_topic(cohort.program_id, body, caller)
    topic = next((topic for item, topic in await program_service.list_topics(cohort.program_id) if item.id == placement.id), None)
    if topic is None:
        raise UnprocessableException("The Topic assignment could not be loaded")
    return ProgramTopicPublic.from_assignment(placement, topic.title)
