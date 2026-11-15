"""Phase 4: real "Discuss with Mariam" live AI sessions. See
`app.services.live_session_service` for what this backend actually does
(session record + LiveKit token minting) versus what the separate
`livekit_agent` worker process does (the conversation itself).
"""

from datetime import datetime
from typing import Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response

from app.core.security import get_current_user, require_teacher_or_administrator, require_internal_agent, require_role
from app.models.live_session import InteractionTurn, PracticeMode, SessionState
from app.models.user import Role, User
from app.schema.live_session import (
    AvatarTokenResponse,
    LiveSessionListItem,
    LiveSessionPublic,
    MarkSessionReportReviewedRequest,
    ReportStatusFilter,
    SessionEndRequest,
    SessionPage,
    SessionProgressFlushRequest,
    SessionStartRequest,
    SessionStartResponse,
    VoiceSessionEndRequest,
)
from app.services.live_session_service import LiveSessionService, get_live_session_service, log_session_end, report_schedule_decision
from app.services.spatialreal_token_service import SpatialRealTokenService, get_spatialreal_token_service

router = APIRouter()


@router.post("/sessions/start", response_model=SessionStartResponse)
async def start_session(
    body: SessionStartRequest,
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
) -> SessionStartResponse:
    """Student only. 403s if the topic is outside their assigned plan, same
    entitlement rule as submitting a recording for it. `mode` selects Avatar
    Practice (Mariam, the default) or Voice Practice (the hosted voice agent).
    `enrollment_id` is required only when the student has more than one
    active enrollment (422 otherwise naming the ambiguity). `checkpoint_id`
    starts this session for a specific Interactive Video Practice with
    Mariam checkpoint instead of an ordinary Avatar Practice session -
    see `LiveSessionService.start_session`."""
    return await service.start_session(
        student, body.topic_id, body.mode, enrollment_id=body.enrollment_id, checkpoint_id=body.checkpoint_id
    )


@router.post("/sessions/{session_id}/handoff", response_model=SessionStartResponse)
async def handoff_session(
    session_id: str,
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
) -> SessionStartResponse:
    """Student only, own Avatar Practice session only. Starts a new Cartoon (Voice Practice) session on the same
    topic that continues the conversation instead of restarting cold - see `LiveSessionService.initiate_handoff` for
    the mechanism, what context is carried, and its documented limitation for a still-live Avatar call. Idempotent:
    calling this again for a session already handed off returns a fresh token for the SAME existing target rather
    than creating a second one. Does not end or otherwise touch the source Avatar session beyond recording the link
    once the target exists.

    One generic entry point by design (`session_id` + auth only) so a later, automatic trigger (Stage 4, not built
    here) can call it exactly the same way a student's own "Switch to Cartoon" click does today."""
    return await service.initiate_handoff(student, session_id)


@router.post("/sessions/{session_id}/avatar-token", response_model=AvatarTokenResponse)
async def issue_avatar_token(
    session_id: str,
    response: Response,
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
    tokens: SpatialRealTokenService = Depends(get_spatialreal_token_service),
) -> AvatarTokenResponse:
    """Student only, and only for their OWN open Avatar Practice session. Exchanges the server-held SpatialReal API key
    for a short-lived session token the browser uses to load the avatar (SpatialReal's `credential`). The API key never
    leaves the server; the answer is never cached."""
    await service.get_open_avatar_session(session_id, student)
    credential = await tokens.issue()
    response.headers["Cache-Control"] = "no-store"
    return AvatarTokenResponse(
        session_token=credential.session_token,
        expires_at=credential.expires_at,
        app_id=credential.app_id,
        avatar_id=credential.avatar_id,
    )


@router.get("/sessions/me", response_model=list[LiveSessionListItem])
async def list_my_sessions(
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
) -> list[LiveSessionListItem]:
    """Student only. Your own session history, newest first."""
    sessions = await service.list_for_student(student)
    return [LiveSessionListItem.from_session(s) for s in sessions]


@router.get("/sessions", response_model=Union[SessionPage, list[LiveSessionListItem]])
async def list_sessions_for_admin(
    search: Optional[str] = Query(None, max_length=100, description="Matches student name, student email or topic title."),
    student_id: Optional[str] = Query(None, description="One of your students."),
    topic_id: Optional[str] = Query(None),
    mode: Optional[PracticeMode] = Query(None, description="topic_discussion (Avatar) or voice_practice."),
    report_status: Optional[ReportStatusFilter] = Query(None, description="any | scored | not_scored | needs_review | reviewed. Omit to include sessions without a report."),
    started_from: Optional[datetime] = Query(None, description="Sessions that started at or after this instant (ISO 8601; a missing offset means UTC)."),
    started_before: Optional[datetime] = Query(None, description="Sessions that started strictly before this instant (ISO 8601; a missing offset means UTC)."),
    page: Optional[int] = Query(None, ge=1, description="1-based. Giving `page` or `limit` switches the response to a paged envelope."),
    limit: Optional[int] = Query(None, ge=1, le=100),
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveSessionService = Depends(get_live_session_service),
) -> Union[SessionPage, list[LiveSessionListItem]]:
    """Administrator only. Your own students' sessions only - no filter can widen that.

    With no `page`/`limit` the response is the plain array it always was (other pages rely on that); filters still apply.
    With `page` and/or `limit` it is `{items, total, page, limit}`: one page, newest first, and `total` = everything that
    matched the filters (not the page size), computed in the database.

    Dates are explicit instants, not calendar days: the caller turns "today in my timezone" into a half-open UTC range
    [`started_from`, `started_before`). Stored timestamps are UTC and are compared as-is."""
    paged = page is not None or limit is not None
    current_page, page_size = (page or 1), (limit or 10)
    sessions, total = await service.search_for_admin(
        admin,
        search=search,
        student_id=student_id,
        topic_id=topic_id,
        mode=mode,
        report_status=report_status.value if report_status else None,
        started_from=started_from,
        started_before=started_before,
        page=current_page,
        limit=page_size if paged else None,
    )
    items = [LiveSessionListItem.from_session(s) for s in sessions]
    return SessionPage(items=items, total=total, page=current_page, limit=page_size) if paged else items


@router.get("/sessions/{session_id}", response_model=LiveSessionPublic)
async def get_session(
    session_id: str,
    requester: User = Depends(get_current_user),
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Fetch one session, including its full transcript. Only the owning
    Student, or that Student's own owning Administrator, may access it."""
    session = await service.get_one(session_id, requester)
    return LiveSessionPublic.from_session(session)


@router.post("/sessions/{session_id}/force-end", response_model=LiveSessionPublic)
async def force_end_session(
    session_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Administrator only, and only your own student's session. Ends it
    immediately and records "administrator_terminated" as the reason."""
    session = await service.force_end(session_id, admin)
    return LiveSessionPublic.from_session(session)


@router.post("/sessions/{session_id}/mark-connected", response_model=LiveSessionPublic, dependencies=[Depends(require_internal_agent)])
async def mark_session_connected(
    session_id: str,
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Internal only, called by the `livekit_agent` worker (never a browser)
    once the room and avatar are actually live - see `require_internal_agent`."""
    session = await service.mark_connected(session_id)
    return LiveSessionPublic.from_session(session)


@router.post("/sessions/{session_id}/flush-progress", response_model=LiveSessionPublic, dependencies=[Depends(require_internal_agent)])
async def flush_session_progress(
    session_id: str,
    body: SessionProgressFlushRequest,
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Internal only, called periodically by the `livekit_agent` worker WHILE an Avatar Practice call is still live
    (never a browser) - see `require_internal_agent`. A best-effort, fire-and-forget progress update: a bounded
    window of the worker's most recent turns, overwriting `LiveSession.live_recent_turns` in place. Never the
    authoritative final transcript (that stays `mark-ended`'s job, unchanged) - this exists so `POST /sessions/{id}
    /handoff` has something real to carry when triggered mid-call; see `LiveSessionService.record_live_progress`."""
    session = await service.record_live_progress(session_id, body.recent_turns)
    return LiveSessionPublic.from_session(session)


@router.post("/sessions/{session_id}/mark-ended", response_model=LiveSessionPublic, dependencies=[Depends(require_internal_agent)])
async def mark_session_ended(
    session_id: str,
    body: SessionEndRequest,
    background_tasks: BackgroundTasks,
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Internal only, called by the `livekit_agent` worker as its job
    finishes, however the call ended - see `require_internal_agent`.

    Kicks off AI scoring in the background - a real Gemini call, so not run
    inline on this request - but only for a session that (a) actually just
    transitioned (not a duplicate call the worker's own finalizer and
    abnormal-exit fallback can both send), (b) ended normally rather than
    failing to connect, and (c) has a real transcript to score at all.
    """
    before = await service.session_repository.get_by_id(session_id)
    state_before = before.state.value if before else "unknown"
    result = await service.mark_ended(session_id, body.end_reason, body.transcript_history)
    scheduled, reason = report_schedule_decision(result)
    log_session_end("agent", result, state_before, scheduled, reason)
    if scheduled:
        background_tasks.add_task(service.generate_report, session_id)
    return LiveSessionPublic.from_session(result.session)


@router.post("/sessions/{session_id}/voice-transcript", response_model=LiveSessionPublic, dependencies=[Depends(require_internal_agent)])
async def record_voice_transcript(
    session_id: str,
    body: SessionEndRequest,
    background_tasks: BackgroundTasks,
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Internal only, called by the Voice Practice AGENT with its own authoritative, ordered transcript (never a
    browser). Same body as `mark-ended`. Works whether the student's own `/end` already ran or not, never touches the
    unverified `browser_transcript`, and schedules the same one-shot report as an Avatar session. A Voice id is the only
    kind this route accepts; Mariam's routes keep ignoring Voice sessions."""
    before = await service.session_repository.get_by_id(session_id)
    state_before = before.state.value if before else "unknown"
    result = await service.record_voice_transcript(session_id, body.end_reason, body.transcript_history)
    scheduled, reason = report_schedule_decision(result)
    log_session_end("voice-agent", result, state_before, scheduled, reason)
    if scheduled:
        background_tasks.add_task(service.generate_report, session_id)
    return LiveSessionPublic.from_session(result.session)


@router.post("/sessions/{session_id}/connected", response_model=LiveSessionPublic)
async def confirm_voice_session_connected(
    session_id: str,
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Student only, own Voice Practice session only. Marks it connected once
    LiveKit confirms the voice agent is in the session's room (409 until
    then). Avatar Practice sessions are marked by their worker instead (422)."""
    session = await service.confirm_voice_connected(session_id, student)
    return LiveSessionPublic.from_session(session)


@router.post("/sessions/{session_id}/end", response_model=LiveSessionPublic)
async def end_voice_session(
    session_id: str,
    body: VoiceSessionEndRequest,
    student: User = Depends(require_role(Role.STUDENT)),
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Student only, own Voice Practice session only. Idempotent; always
    releases the LiveKit room. The optional browser transcript is stored as
    unverified and never scored - no report is scheduled here."""
    turns = [InteractionTurn(**turn.model_dump()) for turn in body.browser_transcript]
    session = await service.end_voice_session(session_id, student, body.end_reason, turns)
    return LiveSessionPublic.from_session(session)


@router.patch("/sessions/{session_id}/report-reviewed", response_model=LiveSessionPublic)
async def mark_session_report_reviewed(
    session_id: str,
    body: MarkSessionReportReviewedRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: LiveSessionService = Depends(get_live_session_service),
) -> LiveSessionPublic:
    """Administrator only. Marks (or unmarks) one of your own students'
    session reports as reviewed - same ownership rule as `get_session`.
    422 if the session has no report yet to review.
    """
    session = await service.mark_report_reviewed(session_id, admin, body.reviewed)
    return LiveSessionPublic.from_session(session)
