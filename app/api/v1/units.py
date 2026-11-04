"""Unit CRUD (card #17 - Topic organization by unit + order). Any
authenticated user may list (a Student needs this to group their own
entitled topics the same way a teacher's catalogue is grouped); every write
is Administrator-only and ownership-gated exactly like `app.api.v1.topics`.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user, require_teacher_or_administrator
from app.models.topic_content import Level
from app.models.user import User
from app.schema.unit import UnitCreate, UnitPublic, UnitUpdate
from app.services.unit_service import UnitService, get_unit_service

router = APIRouter()


def _to_public(unit) -> UnitPublic:
    return UnitPublic(
        id=unit.id,
        created_at=unit.created_at,
        updated_at=unit.updated_at,
        level=unit.level,
        name=unit.name,
        order_index=unit.order_index,
        created_by_id=unit.created_by_id,
    )


@router.post("/units", response_model=UnitPublic)
async def create_unit(
    body: UnitCreate,
    admin: User = Depends(require_teacher_or_administrator),
    service: UnitService = Depends(get_unit_service),
) -> UnitPublic:
    """Administrator only. Owned by the authenticated Administrator - never
    a client-supplied owner, same rule as `POST /topics`.
    """
    unit = await service.create(body, created_by=admin)
    return _to_public(unit)


@router.get("/units", response_model=list[UnitPublic])
async def list_units(
    level: Optional[Level] = Query(None, description="Filter to one level's units."),
    requester: User = Depends(get_current_user),
    service: UnitService = Depends(get_unit_service),
) -> list[UnitPublic]:
    """Root sees every unit; an Administrator sees their own plus
    legacy/shared ones (teacher isolation, same as `GET /topics`).
    """
    units = await service.list_for_requester(requester, level=level)
    return [_to_public(unit) for unit in units]


@router.patch("/units/{unit_id}", response_model=UnitPublic)
async def update_unit(
    unit_id: str,
    body: UnitUpdate,
    admin: User = Depends(require_teacher_or_administrator),
    service: UnitService = Depends(get_unit_service),
) -> UnitPublic:
    """Administrator only, and only a unit you authored (teacher isolation;
    Root may edit any).
    """
    unit = await service.update(unit_id, body, admin)
    return _to_public(unit)


@router.delete("/units/{unit_id}", status_code=204)
async def delete_unit(
    unit_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: UnitService = Depends(get_unit_service),
) -> None:
    """Administrator only, and only a unit you authored (teacher isolation;
    Root may delete any). Soft-delete - any topic still pointing at this
    unit keeps its `unit_id` (an orphaned reference, same as a deleted
    Topic staying referenced by a `CalendarTemplate`); the Teacher UI groups
    it under "Unassigned" rather than hiding it.
    """
    await service.delete(unit_id, admin)
