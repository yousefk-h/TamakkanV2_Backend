"""A "Discuss with Mariam" session. Full 7-state machine from the LiveKit
Avatar requirements spec, plus an `end_reason` the spec treats as separate
from the state itself (e.g. `ENDED` can be reached via a normal user end, an
idle timeout, or an admin force-end - the state alone doesn't say which).

The live call itself is never recorded (confirmed with the stakeholder) -
`transcript_history` is a structured text log only, not a media artifact.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed, Link
from pydantic import BaseModel, Field

from app.models.base import BaseDocument
from app.models.learning_analysis import LearningAnalysis
from app.models.topic_content import TopicContent
from app.models.user import User
from app.utils.time import utcnow


class SessionState(str, Enum):
    NOT_STARTED = "not_started"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    PAUSED = "paused"
    DISCONNECTED = "disconnected"
    ENDED = "ended"
    CONNECTION_FAILED = "connection_failed"


SESSION_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.NOT_STARTED: {SessionState.CONNECTING},
    SessionState.CONNECTING: {SessionState.CONNECTED, SessionState.CONNECTION_FAILED},
    # CONNECTION_FAILED is reachable from CONNECTED, not only from CONNECTING:
    # a call that was genuinely live can still fail (the agent worker crashes,
    # the media server drops it), and the `livekit_agent` worker's abnormal-exit
    # finalizer reports exactly that. Without this edge, a mid-call failure
    # left the session stranded in `connected` forever.
    SessionState.CONNECTED: {SessionState.PAUSED, SessionState.DISCONNECTED, SessionState.ENDED, SessionState.CONNECTION_FAILED},
    SessionState.PAUSED: {SessionState.CONNECTED, SessionState.ENDED},
    SessionState.DISCONNECTED: {SessionState.CONNECTING, SessionState.CONNECTION_FAILED},
    SessionState.CONNECTION_FAILED: {SessionState.NOT_STARTED},
    SessionState.ENDED: {SessionState.NOT_STARTED},
}


class EndReason(str, Enum):
    USER_ENDED = "user_ended"
    IDLE_TIMEOUT = "idle_timeout"
    ADMIN_TERMINATED = "administrator_terminated"
    CONNECTION_FAILURE = "connection_failure"
    MAX_DURATION = "max_duration_reached"
    # Voice Practice only: the hosted agent left the room on its own (e.g. it
    # ended the call) - reported by the student's browser, see
    # `LiveSessionService.end_voice_session`.
    AGENT_ENDED = "agent_ended"


class PracticeMode(str, Enum):
    """Which agent a session talks to. `TOPIC_DISCUSSION` is the original
    Mariam Avatar Practice (the value already stored on every existing
    record); `VOICE_PRACTICE` is the hosted voice-only LiveKit Builder agent.
    """

    TOPIC_DISCUSSION = "topic_discussion"
    VOICE_PRACTICE = "voice_practice"


class InteractionTurn(BaseModel):
    speaker: str  # "student" or "mariam"
    text: str
    timestamp: datetime


class SpeakingReport(BaseModel):
    """An AI-generated read on one finished session's transcript (Kanban Lane
    G) - see `app.core.session_scoring` for how it's produced and
    `LiveSessionService.generate_report` for when. Scores the student's
    English only, never Mariam's, and only from the text transcript - there
    is no audio to score, so nothing here claims to judge pronunciation.
    """

    fluency_score: int | None = Field(ge=0, le=100)
    vocabulary_score: int | None = Field(ge=0, le=100)
    grammar_score: int | None = Field(ge=0, le=100)
    complexity_score: int | None = Field(ge=0, le=100)
    overall_score: int | None = Field(ge=0, le=100)
    summary: str
    generated_at: datetime
    # An Administrator's own read-state on this report, same shape as
    # `RecordingSubmission.reviewed` - starts unseen, flippable either way.
    reviewed: bool = False
    analysis: LearningAnalysis | None = None


class LiveSession(BaseDocument):
    student: Link[User]
    # Denormalized, same reasoning as `RecordingSubmission.student_id`/
    # `LiveClass.created_by_id`: a Beanie `Link` can't be queried by
    # equality, and this is exactly what `get_all_for_student`/ownership
    # checks filter on.
    student_id: Annotated[str, Indexed()]
    topic: Optional[Link[TopicContent]] = None
    topic_id: Annotated[Optional[str], Indexed()] = None
    # Enrollment context at session start. Optional preserves legacy rows.
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    # Which specific Enrollment this session was opened under, when one was
    # resolved - see `EnrollmentService.resolve_context`. `None` for every
    # pre-existing session and for any student with no Enrollment at all
    # (program_id/program_level_id above remain the legacy fallback either
    # way). Lets two enrollments that happen to share the same
    # program_id/program_level_id (e.g. two Cohorts of one Program) still
    # keep independent progress/session history.
    enrollment_id: Annotated[Optional[str], Indexed()] = None
    livekit_room_name: str = ""
    mode: PracticeMode = PracticeMode.TOPIC_DISCUSSION
    # Snapshot the level for later comparisons even if the lesson is edited.
    assessment_level: str | None = None

    # --- Avatar -> Cartoon handoff (additive; absent/None on every pre-existing record) ---
    # Set on a TARGET (Cartoon/Voice Practice) session created via
    # `LiveSessionService.initiate_handoff`: the Avatar session it continues
    # from. `None` for every normal, cold-started session.
    handoff_source_session_id: Optional[str] = None
    # Set on the SOURCE (Avatar) session once its handoff's target session is
    # confirmed created - see `initiate_handoff`'s own docstring for why this
    # is set only after that, never before. `None` means "never handed off"
    # (every pre-existing record, and most sessions in general).
    handed_off_to_session_id: Optional[str] = None
    # The bounded context payload `initiate_handoff` built for the TARGET
    # session (recent turns, last question, etc. - see that method). Stored
    # on the target itself so a retried/idempotent handoff call can re-mint a
    # LiveKit token for the SAME target without rebuilding or re-trusting
    # anything from the client. `None` for a session that isn't a handoff
    # target.
    continuation_context: Optional[dict] = None
    # TRUSTED, bounded "recent turns" window for a still-LIVE Avatar session - the same trust boundary as
    # `transcript_history` (written only by the `livekit_agent` worker's own internal route, never a browser), but
    # refreshed periodically DURING the call rather than once at `mark_ended`. See `LiveSessionService
    # .record_live_progress` / the worker's periodic flush in `livekit_agent/agent.py`. A separate field rather than
    # incremental writes into `transcript_history` itself: this is a small, best-effort, overwritten-in-place snapshot
    # (the worker's last ~`HANDOFF_CONTEXT_MAX_TURNS` turns at the time of the flush), while `transcript_history`
    # stays exactly what it always was - written once, in full, at `mark_ended` - so every existing reader of
    # `transcript_history` (scoring, reports, completed-session display) is completely unaffected by this field
    # existing. `initiate_handoff` is the only reader: full `transcript_history` once the session has ended, this
    # field while it is still live. Empty for every session that hasn't had a flush yet (brand-new call, or a
    # pre-existing record from before this feature) - never fabricated.
    live_recent_turns: list[InteractionTurn] = Field(default_factory=list)

    state: SessionState = SessionState.NOT_STARTED
    end_reason: Optional[EndReason] = None
    # TRUSTED transcript only: written exclusively by an agent-side reporter
    # over the internal `mark-ended` route (today, only Mariam's worker). It
    # is the only transcript that scoring, reports and lesson completion read.
    transcript_history: list[InteractionTurn] = Field(default_factory=list)
    # Provenance stamp, set only by `LiveSessionService.record_voice_transcript` (the Voice agent's internal route). A
    # Voice session is scored only when this says "voice-agent", so a Voice record holding a transcript-looking
    # `transcript_history` from any other source (hand-edited data, a bug) is still refused. Avatar sessions do not need it.
    transcript_source: Optional[str] = None
    # UNVERIFIED, Voice Practice only: final `lk.transcription` segments the
    # student's own browser collected and submitted when ending the session.
    # Kept for display/provisional archive only - never scored, never copied
    # into `transcript_history`, never used for lesson completion.
    browser_transcript: list[InteractionTurn] = Field(default_factory=list)
    avatar_vendor: Optional[str] = None
    # Set once, by `LiveSessionService.generate_report` - `None` until then
    # (no report yet drafted, no report possible for an empty transcript).
    report: Optional[SpeakingReport] = None

    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None

    class Settings:
        name = "live_sessions"
        indexes = [
            # A student's own history, chronological - the primary read path.
            [("student_id", 1), ("started_at", -1)],
        ]
