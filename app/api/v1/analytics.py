from enum import IntEnum

from fastapi import APIRouter, Depends, Response

from app.core.security import require_teacher_or_administrator, require_role
from app.models.user import Role, User
from app.schema.analytics import ActivityAnalytics
from app.services.analytics_service import AnalyticsService, get_analytics_service

router = APIRouter()


class AnalyticsDays(IntEnum):
    WEEK = 7
    MONTH = 30


@router.get("/analytics", response_model=ActivityAnalytics)
async def get_analytics(response: Response, days: AnalyticsDays = AnalyticsDays.MONTH, admin: User = Depends(require_teacher_or_administrator),
                        service: AnalyticsService = Depends(get_analytics_service)) -> ActivityAnalytics:
    """A Teacher's own current roster; the Administrator (Center Admin) gets center-wide analytics across every
    Teacher's roster. Includes inactive, non-deleted accounts."""
    response.headers["Cache-Control"] = "no-store"
    return await service.for_admin(admin, days)


@router.get("/me/analytics", response_model=ActivityAnalytics)
async def get_my_analytics(response: Response, days: AnalyticsDays = AnalyticsDays.WEEK, student: User = Depends(require_role(Role.STUDENT)),
                           service: AnalyticsService = Depends(get_analytics_service)) -> ActivityAnalytics:
    """Authenticated student's own activity; caller cannot select another user."""
    response.headers["Cache-Control"] = "no-store"
    return await service.for_students([student.id], days)
