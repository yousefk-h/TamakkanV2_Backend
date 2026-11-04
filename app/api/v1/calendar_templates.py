"""Calendar template CRUD - admin-only. A Standard template is one active
calendar per `(admin, level)`; a Custom template is a freeform one-off built
for a Pro student and kept in the admin's own reusable library. See
`app.models.calendar_template` and `ProgressService.get_lesson_schedule` for
how a student's assigned template turns into their actual day-by-day
calendar.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.security import require_teacher_or_administrator
from app.models.calendar_template import TemplateKind
from app.models.user import User
from app.schema.calendar_template import CalendarTemplateCreate, CalendarTemplatePublic, CalendarTemplateUpdate
from app.services.calendar_template_service import CalendarTemplateService, get_calendar_template_service

router = APIRouter()


@router.post("/calendar-templates", response_model=CalendarTemplatePublic)
async def create_calendar_template(
    body: CalendarTemplateCreate,
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> CalendarTemplatePublic:
    template = await service.create(body, admin)
    return CalendarTemplatePublic.from_template(template)


@router.get("/calendar-templates", response_model=list[CalendarTemplatePublic])
async def list_calendar_templates(
    kind: Optional[TemplateKind] = Query(None, description="Filter to 'standard' or 'custom' templates."),
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> list[CalendarTemplatePublic]:
    """The caller's own templates only - authoring is per-admin, same as the
    rest of that admin's roster/content management.
    """
    templates = await service.list_for_admin(admin, kind=kind)
    return [CalendarTemplatePublic.from_template(t) for t in templates]


@router.get("/calendar-templates/{template_id}", response_model=CalendarTemplatePublic)
async def get_calendar_template(
    template_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> CalendarTemplatePublic:
    template = await service.get_one_for_admin(template_id, admin)
    return CalendarTemplatePublic.from_template(template)


@router.patch("/calendar-templates/{template_id}", response_model=CalendarTemplatePublic)
async def update_calendar_template(
    template_id: str,
    body: CalendarTemplateUpdate,
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> CalendarTemplatePublic:
    template = await service.update(template_id, body, admin)
    return CalendarTemplatePublic.from_template(template)


@router.post("/calendar-templates/{template_id}/activate", response_model=CalendarTemplatePublic)
async def activate_calendar_template(
    template_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> CalendarTemplatePublic:
    """Standard only. Deactivates every other template for the same level
    under this admin, so exactly one is ever active.
    """
    template = await service.activate(template_id, admin)
    return CalendarTemplatePublic.from_template(template)


@router.delete("/calendar-templates/{template_id}", status_code=204)
async def delete_calendar_template(
    template_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: CalendarTemplateService = Depends(get_calendar_template_service),
) -> None:
    await service.delete(template_id, admin)
