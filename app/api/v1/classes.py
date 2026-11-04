"""Phase 3: Group classes & 1:1 coaching - a Teacher-scheduled slot with a pasted Google Meet link. The Center
Admin (`Role.ADMINISTRATOR`) sees/manages every class in the center, not just their own (PROMPT-049); a Teacher
is still restricted to their own. See `app.services.live_class_service` for the Group/1:1 plan-matching rules.
"""

from fastapi import APIRouter, Depends

from app.core.security import require_teacher_or_administrator, require_role
from app.models.user import Role, User
from app.schema.live_class import CreateLiveClassRequest, LiveClassPublic, UpdateLiveClassRequest
from app.services.live_class_service import LiveClassService, get_live_class_service

router = APIRouter()


@router.post("/classes", response_model=LiveClassPublic)
async def create_class(
    body: CreateLiveClassRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveClassService = Depends(get_live_class_service),
) -> LiveClassPublic:
    """Teacher or Administrator. `participant_ids` must all be students the caller may manage (a Teacher's own;
    any student in the center for the Administrator), and must all be on the plan the class's `kind` requires
    (Standard for Group, Pro for 1:1 - exactly one for 1:1).
    """
    live_class = await service.create(
        admin, body.kind, body.title, body.scheduled_at, body.duration_minutes, body.meet_link, body.participant_ids
    )
    return LiveClassPublic.from_class(live_class)


@router.get("/classes", response_model=list[LiveClassPublic])
async def list_classes_for_admin(
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveClassService = Depends(get_live_class_service),
) -> list[LiveClassPublic]:
    """Teacher or Administrator. A Teacher's own scheduled classes; the Administrator (Center Admin) gets every
    class in the center. Chronological, including past ones - split into upcoming/past client-side against
    `scheduled_at` + `duration_minutes`."""
    classes = await service.list_for_admin(admin)
    return [LiveClassPublic.from_class(c) for c in classes]


@router.get("/classes/me", response_model=list[LiveClassPublic])
async def list_my_classes(
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveClassService = Depends(get_live_class_service),
) -> list[LiveClassPublic]:
    """Student only. Every class you're enrolled in, chronological."""
    classes = await service.list_for_student(student)
    return [LiveClassPublic.from_class(c) for c in classes]


@router.patch("/classes/{class_id}", response_model=LiveClassPublic)
async def update_class(
    class_id: str,
    body: UpdateLiveClassRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveClassService = Depends(get_live_class_service),
) -> LiveClassPublic:
    """Teacher or Administrator - a Teacher only their own class, the Administrator any class in the center.
    Reschedule/retitle/re-link - participants aren't editable here, see `UpdateLiveClassRequest`.
    """
    live_class = await service.update(
        class_id,
        admin,
        title=body.title,
        scheduled_at=body.scheduled_at,
        duration_minutes=body.duration_minutes,
        meet_link=body.meet_link,
    )
    return LiveClassPublic.from_class(live_class)


@router.delete("/classes/{class_id}", status_code=204)
async def cancel_class(
    class_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveClassService = Depends(get_live_class_service),
) -> None:
    """Teacher or Administrator - a Teacher only their own class, the Administrator any class in the center.
    Cancels (soft-deletes) it - it stops appearing for both the admin and its participants.
    """
    await service.cancel(class_id, admin)
