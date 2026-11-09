"""Registers every API version's routers onto one top-level router.

`app.main` mounts `api_router` under `settings.API_PREFIX`; each version adds
its own prefix here (e.g. "/v1"), so a new `v2/` package is one more
`include_router()` call, not a restructure.
"""

from fastapi import APIRouter

from app.api.v1.analytics import router as analytics_router

from app.api.v1.auth import router as auth_router
from app.api.v1.calendar_templates import router as calendar_templates_router
from app.api.v1.cohorts import router as cohorts_router
from app.api.v1.enrollments import router as enrollments_router
from app.api.v1.exercise_attempts import router as exercise_attempts_router
from app.api.v1.classes import router as classes_router
from app.api.v1.health import router as health_router
from app.api.v1.homework import router as homework_router
from app.api.v1.level_profiles import router as level_profiles_router
from app.api.v1.me import router as me_router
from app.api.v1.operations import router as operations_router
from app.api.v1.programs import router as programs_router
from app.api.v1.root import router as root_router
from app.api.v1.sessions import router as sessions_router
from app.api.v1.submissions import router as submissions_router
from app.api.v1.teachers import router as teachers_router
from app.api.v1.topics import router as topics_router
from app.api.v1.units import router as units_router
from app.api.v1.users import router as users_router

api_router = APIRouter()

# The `tags` here are what section every route in that router under in Swagger
# - see `TAGS_METADATA` in `app.core.application` for the description each
# section shows. One line per router; no per-route tagging needed.
api_router.include_router(health_router, prefix="/v1", tags=["Health"])
api_router.include_router(auth_router, prefix="/v1", tags=["Auth"])
api_router.include_router(me_router, prefix="/v1", tags=["Me"])
api_router.include_router(users_router, prefix="/v1", tags=["Students"])
api_router.include_router(teachers_router, prefix="/v1", tags=["Teachers"])
api_router.include_router(root_router, prefix="/v1", tags=["Root"])
api_router.include_router(topics_router, prefix="/v1", tags=["Topics"])
api_router.include_router(units_router, prefix="/v1", tags=["Units"])
api_router.include_router(submissions_router, prefix="/v1", tags=["Submissions"])
api_router.include_router(exercise_attempts_router, prefix="/v1", tags=["Exercise Answers"])
api_router.include_router(homework_router, prefix="/v1", tags=["Homework"])
api_router.include_router(level_profiles_router, prefix="/v1", tags=["Level Profiles"])
api_router.include_router(programs_router, prefix="/v1", tags=["Programs"])
api_router.include_router(cohorts_router, prefix="/v1", tags=["Cohorts"])
api_router.include_router(enrollments_router, prefix="/v1", tags=["Enrollments"])
api_router.include_router(classes_router, prefix="/v1", tags=["Classes"])
api_router.include_router(sessions_router, prefix="/v1", tags=["Sessions"])
api_router.include_router(calendar_templates_router, prefix="/v1", tags=["Calendar Templates"])
api_router.include_router(analytics_router, prefix="/v1", tags=["Analytics"])
api_router.include_router(operations_router, prefix="/v1", tags=["Operations"])
