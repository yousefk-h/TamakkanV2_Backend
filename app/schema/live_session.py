from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from app.models.live_session import EndReason, InteractionTurn, LiveSession, PracticeMode, SessionState, SpeakingReport
from app.schema.base import BaseSchema
from app.schema.limits import MAX_LINE_LENGTH

# Generous for a 15-60 minute voice call made of final STT/TTS segments.
MAX_BROWSER_TRANSCRIPT_TURNS = 600


class SessionStartRequest(BaseModel):
    topic_id: str
    # Only picks WHICH agent to talk to. Prompt, level and every other lesson
    # setting are always read server-side from the saved lesson.
    mode: PracticeMode = PracticeMode.TOPIC_DISCUSSION


class BrowserTranscriptTurn(BaseModel):
    """One final `lk.transcription` segment as the student's browser saw it.
    Speaker is derived client-side from the participant that published the
    segment - unverified, see `LiveSession.browser_transcript`."""

    speaker: Literal["student", "agent"]
    text: Annotated[str, Field(min_length=1, max_length=MAX_LINE_LENGTH)]
    timestamp: datetime


class VoiceSessionEndRequest(BaseModel):
    """Body for the student-authenticated `POST /sessions/{id}/end` (Voice
    Practice only). Never reaches `transcript_history` or scoring."""

    end_reason: EndReason
    browser_transcript: Annotated[list[BrowserTranscriptTurn], Field(max_length=MAX_BROWSER_TRANSCRIPT_TURNS)] = Field(
        default_factory=list
    )


class SessionEndRequest(BaseModel):
    """Body for the internal `POST /sessions/{id}/mark-ended` call - see
    `app.core.security.require_internal_agent`. `transcript_history` is the
    whole call's worth of turns the worker buffered in memory, sent once
    here rather than incrementally - see `LiveSessionService.mark_ended`'s
    own docstring for why. Optional/empty for the connection-failure path,
    where there may be nothing to send at all.
    """

    end_reason: EndReason
    transcript_history: list[InteractionTurn] = Field(default_factory=list)


class SessionProgressFlushRequest(BaseModel):
    """Body for the internal `POST /sessions/{id}/flush-progress` call - see `app.core.security
    .require_internal_agent`. Same trust boundary and auth as `mark-ended`'s `transcript_history`, just sent
    periodically, DURING a still-live Avatar call, instead of once at the end. Small and bounded by design (the
    worker only ever sends its own last few turns - see `livekit_agent.agent`'s periodic flush) - this is not a second
    transcript-delivery mechanism, it overwrites `LiveSession.live_recent_turns` in place with whatever the worker
    currently has."""

    recent_turns: list[InteractionTurn] = Field(default_factory=list)


class MarkSessionReportReviewedRequest(BaseModel):
    """Body for `PATCH /sessions/{id}/report-reviewed` - same shape as
    `app.schema.submission.MarkReviewedRequest`, kept separate rather than
    shared since the two resources' review states aren't otherwise related.
    """

    reviewed: bool


class SessionStartResponse(BaseModel):
    """What the student's browser needs to connect - never a stored value,
    minted fresh per `POST /sessions/start` call."""

    server_url: str
    participant_token: str
    session_id: str
    room_name: str
    # What the LiveKit agent worker was actually dispatched with as this
    # course's time budget - see `ProgressService.get_topic_session_minutes`.
    # The frontend's countdown timer counts down from this, not the
    # student's flat daily cap.
    max_duration_minutes: int


class AvatarTokenResponse(BaseModel):
    """What the browser needs to draw Mariam's avatar. `session_token` is SpatialReal's short-lived credential,
    minted per request - never the API key. The two ids are public and come from the same server settings the hosted
    agent is configured from, so the browser and the agent cannot disagree about the app or the avatar."""

    session_token: str
    expires_at: int
    app_id: str
    avatar_id: str


class LiveSessionPublic(BaseSchema):
    student_id: str
    topic_id: Optional[str]
    mode: PracticeMode
    state: SessionState
    end_reason: Optional[EndReason]
    started_at: datetime
    ended_at: Optional[datetime]
    # Omitted from list responses (see `LiveSessionListItem`) - only the
    # single-session detail fetch needs the full turn-by-turn log, same
    # "detail vs list" size discipline as `TopicListItem`/`TopicPublic`.
    transcript_history: list[InteractionTurn]
    # Voice Practice only, UNVERIFIED (browser-captured) - see
    # `LiveSession.browser_transcript`. Never scored.
    browser_transcript: list[InteractionTurn] = Field(default_factory=list)
    # Unlike the transcript, small enough (a handful of scores + a few
    # sentences) to carry on both shapes - the reports grid (list) and a
    # single session's own page (detail) both need it directly.
    report: Optional[SpeakingReport]

    @classmethod
    def from_session(cls, session: LiveSession) -> "LiveSessionPublic":
        return cls(
            id=session.id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            student_id=session.student_id,
            topic_id=session.topic_id,
            mode=session.mode,
            state=session.state,
            end_reason=session.end_reason,
            started_at=session.started_at,
            ended_at=session.ended_at,
            transcript_history=session.transcript_history,
            browser_transcript=session.browser_transcript,
            report=session.report,
        )


class ReportStatusFilter(str, Enum):
    """Server-side filter for the admin's AI Reports list. Every value maps to a real field of the stored report - nothing is
    derived on the client. `any` = has a report at all (the Reports page's "All reports")."""

    ANY = "any"
    SCORED = "scored"  # report.overall_score is set (an assessed report)
    NOT_SCORED = "not_scored"  # a report exists but has no score (insufficient evidence)
    NEEDS_REVIEW = "needs_review"  # report.reviewed is false
    REVIEWED = "reviewed"  # report.reviewed is true


class LiveSessionListItem(BaseSchema):
    student_id: str
    topic_id: Optional[str]
    mode: PracticeMode
    state: SessionState
    end_reason: Optional[EndReason]
    started_at: datetime
    ended_at: Optional[datetime]
    report: Optional[SpeakingReport]

    @classmethod
    def from_session(cls, session: LiveSession) -> "LiveSessionListItem":
        return cls(
            id=session.id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            student_id=session.student_id,
            topic_id=session.topic_id,
            mode=session.mode,
            state=session.state,
            end_reason=session.end_reason,
            started_at=session.started_at,
            ended_at=session.ended_at,
            report=session.report,
        )


class SessionPage(BaseModel):
    """One page of the admin's sessions plus the total that matched the filters (not the page size)."""

    items: list[LiveSessionListItem]
    total: int
    page: int
    limit: int
