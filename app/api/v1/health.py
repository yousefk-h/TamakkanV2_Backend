"""Liveness check - confirms the API process is up and responding.

Reference example for the `vN/` route-module convention: one router per file,
registered onto `api_router` in `app.api.api`.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Returns 200 if the process is up. No dependency checks (Mongo, ...) -
    that's deliberate, so this can't itself hang or fail on a DB outage.
    """
    return {"status": "ok"}
