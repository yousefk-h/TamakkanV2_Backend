"""Observed session state for the internal Tamakkan operations view."""

from datetime import datetime

from pydantic import BaseModel

from app.models.live_session import EndReason, PracticeMode, SessionState


class OperationsSession(BaseModel):
    id: str
    mode: PracticeMode
    state: SessionState
    started_at: datetime
    ended_at: datetime | None
    end_reason: EndReason | None


class OperationsSnapshot(BaseModel):
    generated_at: datetime
    connected_by_mode: dict[PracticeMode, int]
    connecting: int
    disconnected: int
    connection_failures_24h: int
    ended_24h: int
    handoffs_24h: int
    recent_sessions: list[OperationsSession]
