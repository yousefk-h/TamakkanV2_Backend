"""Internal operations data derived from persisted sessions, scoped to the caller's roster."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Response

from app.core.security import require_teacher_or_administrator
from app.models.live_session import LiveSession, PracticeMode, SessionState
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schema.operations import OperationsSession, OperationsSnapshot
from app.utils.time import utcnow

router = APIRouter()


@router.get("/operations/sessions", response_model=OperationsSnapshot)
async def session_operations(response: Response, caller: User = Depends(require_teacher_or_administrator)) -> OperationsSnapshot:
    """Database-observed state, not a LiveKit room heartbeat or a provider billing estimate."""
    response.headers["Cache-Control"] = "no-store"
    users = UserRepository()
    roster = (await users.get_all_students() if caller.role == Role.ADMINISTRATOR
              else await users.get_students_created_by(caller.id))
    now = utcnow()
    if not roster:
        return OperationsSnapshot(generated_at=now, connected_by_mode={mode: 0 for mode in PracticeMode},
                                  connecting=0, disconnected=0, connection_failures_24h=0,
                                  ended_24h=0, handoffs_24h=0, recent_sessions=[])
    base = {"student_id": {"$in": [student.id for student in roster]}, "is_deleted": False}
    window = {**base, "started_at": {"$gte": now - timedelta(hours=24), "$lte": now}}

    async def count(extra: dict, *, recent: bool = False) -> int:
        return await LiveSession.find({**(window if recent else base), **extra}).count()

    connected = {mode: await count({"state": SessionState.CONNECTED.value, "mode": mode.value}) for mode in PracticeMode}
    recent = await LiveSession.find(base).sort(-LiveSession.started_at).limit(20).to_list()
    return OperationsSnapshot(
        generated_at=now,
        connected_by_mode=connected,
        connecting=await count({"state": SessionState.CONNECTING.value}),
        disconnected=await count({"state": SessionState.DISCONNECTED.value}),
        connection_failures_24h=await count({"state": SessionState.CONNECTION_FAILED.value}, recent=True),
        ended_24h=await count({"state": SessionState.ENDED.value,
                               "ended_at": {"$gte": now - timedelta(hours=24), "$lte": now}}),
        handoffs_24h=await count({"handoff_source_session_id": {"$ne": None}}, recent=True),
        recent_sessions=[OperationsSession(id=s.id, mode=s.mode, state=s.state, started_at=s.started_at,
                                           ended_at=s.ended_at, end_reason=s.end_reason) for s in recent],
    )
