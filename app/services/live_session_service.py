"""Phase 4: real "Discuss with Mariam" sessions. This service only handles
the session *record* and LiveKit *token minting* - the actual conversation
(Gemini Live voice, SpatialReal avatar) runs in the separate `livekit_agent`
worker process, a fully separate deployable with no direct database access of
its own (see `livekit_agent/agent.py`). That worker gets the
topic content it needs (avatar prompt, opening line, level) embedded directly
in its job dispatch metadata at `start_session` time below, and reports
state transitions back here over HTTP (`mark_connected`/`mark_ended`,
gated by `require_internal_agent`) rather than writing to Mongo itself.

Voice Practice (`PracticeMode.VOICE_PRACTICE`) reuses the same record, start
flow and entitlement gate, but dispatches the hosted LiveKit Agent Builder
agent instead. That agent runs no code of ours, so it cannot call
`mark-connected`/`mark-ended`: its lifecycle is driven by the owning student
(`confirm_voice_connected`/`end_voice_session`), with "connected" verified
against LiveKit's own participant list. Whatever transcript the browser sends
is stored as `browser_transcript` (unverified) and is never scored.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from livekit import api as livekit_api
from loguru import logger

from app.core.config import get_settings
from app.core.learning_analytics import SCORE_FIELDS, comparison_for, has_enough_evidence, transcript_metrics, validate_feedback
from app.core.entitlement import requester_may_open
from app.core.prompt_safety import sanitize_untrusted_text, wrap_untrusted_lesson_content
from app.core.exceptions import ConflictException, ForbiddenException, NotFoundException, ServiceUnavailableException, UnprocessableException
from app.core.session_scoring import SessionScorer, SessionScoringError, SessionScoringNotConfigured, get_session_scorer
from app.core.state_machine import InvalidTransition, transition
from app.models.audit_log import AuditLog
from app.models.level_profile import LevelProfile
from app.models.live_session import (
    SESSION_TRANSITIONS,
    EndReason,
    InteractionTurn,
    LiveSession,
    PracticeMode,
    SessionState,
    SpeakingReport,
)
from app.models.topic_content import CheckpointKind, InteractiveVideoCheckpoint, TopicContent
from app.models.user import Role, User
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.learning_analysis import LearningAnalysis
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.schema.live_session import CheckpointSessionStatus, SessionStartResponse
from app.services.enrollment_service import EnrollmentContext, EnrollmentService, get_enrollment_service
from app.services.progress_service import ProgressService, get_progress_service
from app.utils.time import utcnow

ROOM_PREFIX = {PracticeMode.TOPIC_DISCUSSION: "mariam", PracticeMode.VOICE_PRACTICE: "voice"}
# End reasons a student's browser may report for a Voice Practice session.
# Never `ADMIN_TERMINATED` (that is `force_end`'s) or `IDLE_TIMEOUT` (the
# browser has no way to observe the agent's idle logic).
STUDENT_REPORTABLE_END_REASONS = {
    EndReason.USER_ENDED, EndReason.AGENT_ENDED, EndReason.MAX_DURATION, EndReason.CONNECTION_FAILURE,
}
VOICE_AGENT_TRANSCRIPT_SOURCE = "voice-agent"
# Bounded context for an Avatar -> Cartoon handoff (see `initiate_handoff`):
# the last N trusted transcript turns, never the whole conversation. Small
# enough to stay well under the voice agent's prompt budget alongside the
# lesson prompt it already carries.
HANDOFF_CONTEXT_MAX_TURNS = 8
# A genuine Practice with Mariam checkpoint attempt needs more than reaching
# `ENDED`: a student who connects and immediately hits "end call" (or whose
# agent hangs up before a word is exchanged) reaches `ENDED` in well under a
# second with an empty/one-sided transcript. 12s is short enough not to
# penalize a real, fast exchange (opening line + one short student reply) but
# long enough to exclude that near-instant connect-then-hangup case. See
# `is_checkpoint_practice_completed`.
MIN_CHECKPOINT_PRACTICE_SECONDS = 12.0


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Stored timestamps are naive UTC. An offset-aware instant is converted to UTC; a naive one is already read as UTC."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


OPEN_STATES = {SessionState.CONNECTING, SessionState.CONNECTED, SessionState.PAUSED, SessionState.DISCONNECTED}


def is_checkpoint_practice_completed(session: LiveSession) -> bool:
    """Whether `session` is a genuinely completed Practice with Mariam
    checkpoint attempt - stronger than the bare `state == ENDED` check (see
    `get_checkpoint_session_status`). On top of reaching `ENDED`, requires an
    actual back-and-forth (at least one `transcript_history` turn from each
    speaker, not just Mariam's opening line or an empty transcript) and a
    minimum session duration (`MIN_CHECKPOINT_PRACTICE_SECONDS`). A missing
    `ended_at` is treated as not-completed rather than raising."""
    if session.state != SessionState.ENDED or session.ended_at is None:
        return False
    has_student_turn = any(turn.speaker == "student" for turn in session.transcript_history)
    has_mariam_turn = any(turn.speaker == "mariam" for turn in session.transcript_history)
    if not (has_student_turn and has_mariam_turn):
        return False
    started_at = _as_naive_utc(session.started_at)
    ended_at = _as_naive_utc(session.ended_at)
    duration_seconds = (ended_at - started_at).total_seconds()
    return duration_seconds >= MIN_CHECKPOINT_PRACTICE_SECONDS


def report_schedule_decision(result: "MarkEndedResult") -> tuple[bool, str]:
    """Should a report be generated for this end call, and if not, why. Counts/status only - the reason is logged so a
    session that silently ends up "not scored" always leaves an explanation."""
    session = result.session
    if not result.transitioned:
        return False, "duplicate or unreachable end call (nothing changed)"
    if session.state != SessionState.ENDED:
        return False, f"session did not end normally (state={session.state.value})"
    if not session.transcript_history:
        return False, "no trusted transcript was received from the agent"
    return True, "trusted transcript received"


def log_session_end(source: str, result: "MarkEndedResult", state_before: str, scheduled: bool, reason: str) -> None:
    """One line per end call: identifiers, states and COUNTS only - never transcript text."""
    session = result.session
    logger.info(
        "session end [{}]: id={} mode={} state {} -> {} transitioned={} trusted_transcript_turns={} "
        "browser_transcript_turns={} report_generation_scheduled={} reason={}",
        source, session.id, session.mode.value, state_before, session.state.value, result.transitioned,
        len(session.transcript_history), len(session.browser_transcript), "YES" if scheduled else "NO", reason,
    )


@dataclass
class MarkEndedResult:
    session: LiveSession
    # False for a repeated/unreachable mark-ended call (see `mark_ended`'s
    # own "ignore an unreachable transition" reasoning) - the route uses this
    # to decide whether to schedule `generate_report`, so a duplicate call
    # (the `livekit_agent` worker's own finalizer + its `finally`-block
    # fallback can both fire) never double-schedules scoring.
    transitioned: bool


class LiveSessionService:
    def __init__(
        self,
        session_repository: LiveSessionRepository,
        topic_repository: TopicContentRepository,
        user_repository: UserRepository,
        audit_log_repository: AuditLogRepository,
        session_scorer: SessionScorer,
        progress_service: ProgressService,
        enrollment_service: EnrollmentService,
    ) -> None:
        self.session_repository = session_repository
        self.topic_repository = topic_repository
        self.progress_service = progress_service
        self.user_repository = user_repository
        self.audit_log_repository = audit_log_repository
        self.session_scorer = session_scorer
        self.enrollment_service = enrollment_service

    async def start_session(
        self,
        student: User,
        topic_id: str,
        mode: PracticeMode = PracticeMode.TOPIC_DISCUSSION,
        *,
        continuation_context: Optional[dict] = None,
        handoff_source_session_id: Optional[str] = None,
        enrollment_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ) -> SessionStartResponse:
        if checkpoint_id is not None:
            # Practice with Mariam rides entirely on the existing Avatar
            # pipeline - no new PracticeMode, no new agent.
            mode = PracticeMode.TOPIC_DISCUSSION
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        # Resolves which Enrollment this session opens under - explicit
        # `enrollment_id` (e.g. carried over from the source session on a
        # handoff), the student's one unambiguous active Enrollment, or the
        # legacy `student.program_id`/`program_level_id` fallback when no
        # Enrollment exists at all. Raises if the student has several active
        # enrollments and didn't say which one - never silently picks one.
        context = await self.enrollment_service.resolve_context(student, enrollment_id)
        # Same write-side entitlement gate as `RecordingSubmissionService
        # .submit_audio` - a topic-tied Mariam session is a write into that
        # topic's content the same way a submission is.
        if not await requester_may_open(student, topic_id, self.progress_service, context):
            raise ForbiddenException("This topic is not part of your assigned plan, or hasn't unlocked yet")

        # Program placement, not the Topic Library's legacy/default level,
        # defines delivery. The published revision remains stable until an
        # explicit re-publish action pins the latest library revision.
        delivery_level = topic.level
        delivery_level_name = topic.level_name
        delivery_guidance = topic.level_guidance
        if context.program_id:
            placement = await ProgramTopic.find_one({
                "program_id": context.program_id,
                "topic_id": topic.id,
                "status": ProgramTopicStatus.PUBLISHED.value,
                "is_deleted": False,
            })
            if placement is not None:
                topic = topic.at_revision(placement.published_revision)
                delivery_level = placement.level
                if placement.level_profile_id:
                    profile = await LevelProfile.find_one({
                        "_id": placement.level_profile_id,
                        "is_deleted": False,
                        "is_active": True,
                    })
                    if profile is not None:
                        delivery_level_name = profile.name
                        delivery_guidance = profile.avatar_guidance

        checkpoint: Optional[InteractiveVideoCheckpoint] = None
        if checkpoint_id is not None:
            checkpoint = next((item for item in topic.interactive_video_checkpoints if item.id == checkpoint_id), None)
            if checkpoint is None:
                raise NotFoundException("Checkpoint not found")
            if checkpoint.kind != CheckpointKind.PRACTICE_WITH_MARIAM:
                raise UnprocessableException("This checkpoint is not a Practice with Mariam checkpoint")

        settings = get_settings()
        required = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
        if mode == PracticeMode.TOPIC_DISCUSSION:
            # Only Mariam's worker calls back into this API.
            required.append("AGENT_INTERNAL_API_KEY")
        missing = [name for name in required if not getattr(settings, name).strip()]
        if missing:
            logger.warning("Mariam session configuration missing: {}", ", ".join(missing))
            # No session record should be created when a call cannot even start.
            raise ServiceUnavailableException(
                "Live speaking sessions are not configured yet. Please contact your administrator."
            )

        if mode == PracticeMode.VOICE_PRACTICE:
            # A refresh/second tab must never leave an earlier voice room (and
            # its agent) running alongside the new one.
            await self._close_open_voice_sessions(student)

        session = LiveSession(
            student=student,
            student_id=student.id,
            topic=topic,
            topic_id=topic.id,
            program_id=context.program_id,
            program_level_id=context.program_level_id,
            enrollment_id=context.enrollment.id if context.enrollment else None,
            checkpoint_id=checkpoint_id,
            mode=mode,
            assessment_level=delivery_level.value,
            state=self._advance(SessionState.NOT_STARTED, SessionState.CONNECTING),
            handoff_source_session_id=handoff_source_session_id,
            continuation_context=continuation_context,
        )
        await self.session_repository.save(session)

        room_name = f"{ROOM_PREFIX[mode]}-{session.id}"
        session.livekit_room_name = room_name
        await self.session_repository.save(session)

        max_duration_minutes = await self.progress_service.get_topic_session_minutes_for_context(student, topic_id, context)
        if checkpoint is not None:
            # A checkpoint's own duration must never let a student exceed the
            # remaining topic session budget - only ever tighten it.
            max_duration_minutes = min(checkpoint.session_duration_minutes, max_duration_minutes)
        if mode == PracticeMode.VOICE_PRACTICE:
            agent_name = settings.LIVEKIT_VOICE_AGENT_NAME
            # Flat string/number values only: Agent Builder exposes each
            # top-level key as `{{metadata.<key>}}` in its instructions and
            # greeting. Everything comes from the saved lesson, never the client.
            metadata = {
                # Dedicated field: the hosted voice agent is shared infrastructure and tells projects apart by this.
                "project": "tamakkan",
                "session_id": session.id,
                "topic_id": topic.id,
                "max_duration_minutes": max_duration_minutes,
                "lesson_title": topic.title,
                # Teacher-authored (possibly AI-drafted from an uploaded
                # document) - never trusted as instructions. Wrapped in an
                # explicit "lesson reference material" boundary before it
                # ever reaches the hosted voice agent's template - see
                # app.core.prompt_safety for why this exists alongside
                # (not instead of) that agent's own prompt wording.
                "lesson_prompt": wrap_untrusted_lesson_content(
                    "TEACHER LESSON INSTRUCTIONS",
                    topic.voice_practice_prompt + (f"\n\nCenter level guidance ({delivery_level_name}): {delivery_guidance}" if delivery_guidance else ""),
                ),
                "opening_line": sanitize_untrusted_text(topic.opening_line),
                "level": delivery_level.value,
                # Optional Jordanian-Arabic clarification support (teacher-
                # controlled) - `None` means "legacy topic, predates this
                # field", which the voice agent treats as its own old,
                # unconditional Arabic-on-request line. See `TopicContent
                # .arabic_support_enabled`.
                "arabic_support_enabled": topic.arabic_support_enabled,
                "arabic_intervention_level": topic.arabic_intervention_level.value,
            }
            if continuation_context:
                # Additive, optional template variable for the Voice agent
                # (`voice_agent/src/agent.py`'s `{{continuation.summary}}`) -
                # see `initiate_handoff` for how this is built. Absent for
                # every normal, cold-started Cartoon/Voice Practice session,
                # so that agent's behavior is unchanged when this key is missing.
                metadata["continuation"] = continuation_context
        else:
            agent_name = settings.LIVEKIT_AGENT_NAME
            if checkpoint is not None:
                topic_instructions = self._build_checkpoint_topic_instructions(
                    topic, checkpoint, delivery_level, delivery_level_name, delivery_guidance
                )
                opening_line = sanitize_untrusted_text(checkpoint.opening_line)
            else:
                # Handed to the agent directly so it never needs its own
                # database read to build Mariam's instructions or opening
                # line - see this module's docstring. Teacher-authored
                # (possibly AI-drafted from an uploaded document), so it is
                # wrapped in an explicit "lesson reference material, not
                # instructions" boundary - see app.core.prompt_safety.
                topic_instructions = wrap_untrusted_lesson_content(
                    "TOPIC INSTRUCTIONS",
                    topic.avatar_prompt + (f"\n\nCenter level guidance ({delivery_level_name}): {delivery_guidance}" if delivery_guidance else ""),
                )
                opening_line = sanitize_untrusted_text(topic.opening_line)
            metadata = {
                "session_id": session.id,
                "topic_id": topic.id,
                "max_duration_minutes": max_duration_minutes,
                "topic_instructions": topic_instructions,
                "opening_line": opening_line,
                "level": delivery_level.value,
                # Same optional Jordanian-Arabic support as Voice Practice
                # above - the Avatar agent never had any Arabic behavior
                # before this, so `None`/`False` are equivalent for it.
                "arabic_support_enabled": topic.arabic_support_enabled,
                "arabic_intervention_level": topic.arabic_intervention_level.value,
            }
        dispatch_metadata = json.dumps(metadata)
        logger.info("session started: id={} mode={} dispatched_agent={} room={}", session.id, mode.value, agent_name, room_name)
        token = (
            livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
            .with_identity(f"student-{student.id}")
            .with_name(student.full_name)
            .with_grants(livekit_api.VideoGrants(room_join=True, room=room_name))
            .with_room_config(
                livekit_api.RoomConfiguration(
                    agents=[
                        livekit_api.RoomAgentDispatch(agent_name=agent_name, metadata=dispatch_metadata)
                    ]
                )
            )
        )

        return SessionStartResponse(
            server_url=settings.LIVEKIT_URL,
            participant_token=token.to_jwt(),
            session_id=session.id,
            room_name=room_name,
            max_duration_minutes=max_duration_minutes,
        )

    async def initiate_handoff(self, student: User, source_session_id: str) -> SessionStartResponse:
        """Avatar -> Cartoon continuation handoff (Stage 3). ONE generic entry point, identified only by
        `source_session_id` (+ the caller's own auth context) - today it is called by the student clicking "Switch to
        Cartoon" (see `CartoonSession.tsx`/`PracticeModeSwitch.tsx`), but nothing here is UI-specific, so a later,
        automatic network-fallback trigger (Stage 4, not built here) can call this exact same method/route.

        What it does: starts a brand-new Voice Practice (Cartoon) session on the SAME topic, via the existing
        `start_session(..., mode=VOICE_PRACTICE)` path (no parallel session-creation code path), carrying a small,
        server-built "continuation context" the Voice agent can fold into its prompt additively (see
        `start_session`'s `continuation_context` and `voice_agent/src/agent.py`'s `{{continuation.summary}}`).

        Ownership/validation: `get_one` already enforces "the owning Student, or nobody" for a Student caller, so a
        session that is not the caller's own raises the same 403/404 it always would. The source must still be an
        Avatar Practice (`TOPIC_DISCUSSION`) session - a Voice/Cartoon session has nothing to hand off FROM.

        Idempotency: a session is handed off at most once. A retry (double-click, a retried request after a flaky
        network) finds `handoff_source_session_id` already set and returns a FRESH token for the SAME existing target
        session - built from that target's own stored `continuation_context` - rather than creating a second one.

        Never ends the source session: `handed_off_to_session_id` is written onto it only AFTER the target session is
        confirmed created and saved. If target creation fails (e.g. Voice Practice misconfigured), nothing is written
        to the source at all - it is left exactly as it was, and the raised exception is the caller's clear error.

        Live-call case (fixed - see `record_live_progress`): `transcript_history` for an Avatar session is still
        written only ONCE, at `mark_ended`, from the whole call the `livekit_agent` worker buffered in memory (see
        that method's own docstring on why - a measured source of response lag otherwise). But the same worker now
        ALSO periodically flushes its last few turns, fire-and-forget, into `LiveSession.live_recent_turns` over a
        new, equally-trusted internal route (`POST /sessions/{id}/flush-progress`) while the call is still live. So:
        if a student switches to Cartoon WHILE the Avatar call is still live, `recent_turns` below is built from
        whatever has been flushed so far (possibly empty, if switching happens before the first flush - reported
        honestly as empty, never fabricated); once the Avatar session has actually ended, its real, final
        `transcript_history` is used instead, same as before this fix.
        """
        source = await self.get_one(source_session_id, student)
        if source.mode != PracticeMode.TOPIC_DISCUSSION:
            raise UnprocessableException("Only an Avatar Practice session can be handed off to Cartoon")

        if source.handed_off_to_session_id:
            target = await self.session_repository.get_by_id(source.handed_off_to_session_id)
            if target is None:
                raise ConflictException("This session's Cartoon handoff target no longer exists")
            return await self._reissue_handoff_token(target)

        if source.topic_id is None:
            raise UnprocessableException("This session has no topic to continue")

        # `transcript_history` is only ever populated once, at `mark_ended` - a still-`CONNECTED`/open source session
        # has none yet, however long the call has actually run. `live_recent_turns` is this same trusted transcript's
        # bounded, periodically-refreshed preview for exactly that still-live case (see `record_live_progress` /
        # `LiveSession.live_recent_turns`'s own docstring). Prefer the real, final transcript once it exists; fall
        # back to whatever the worker has flushed so far otherwise. Honest, not fabricated: a call with no flush yet
        # (e.g. the first few seconds) simply yields an empty `recent_turns` below, same as today.
        source_turns = source.transcript_history or source.live_recent_turns
        recent_turns = source_turns[-HANDOFF_CONTEXT_MAX_TURNS:]
        last_question = next(
            (turn.text for turn in reversed(recent_turns) if turn.speaker == "mariam"), None
        )
        continuation_context = {
            "source_session_id": source.id,
            "recent_turns": [{"speaker": turn.speaker, "text": turn.text} for turn in recent_turns],
            "last_question": last_question,
        }

        target_response = await self.start_session(
            student,
            source.topic_id,
            PracticeMode.VOICE_PRACTICE,
            continuation_context=continuation_context,
            handoff_source_session_id=source.id,
            # Carries the SOURCE session's own resolved enrollment forward
            # rather than re-resolving - a handoff must never switch
            # enrollment context mid-conversation, even if the student holds
            # several active enrollments. `None` for a legacy source session
            # (every pre-existing one) preserves the old fallback behavior.
            enrollment_id=source.enrollment_id,
        )

        # Only written once the target is confirmed created and saved above - a failure there raises before this
        # line, leaving the source session untouched (see this method's own docstring).
        source.handed_off_to_session_id = target_response.session_id
        await self.session_repository.save(source)
        logger.info(
            "session handoff: source={} target={} topic={} recent_turns_carried={}",
            source.id, target_response.session_id, source.topic_id, len(recent_turns),
        )
        return target_response

    async def _reissue_handoff_token(self, target: LiveSession) -> SessionStartResponse:
        """A fresh LiveKit token for an ALREADY-CREATED handoff target session - the idempotent-retry path of
        `initiate_handoff`. Token minting is a pure local JWT signature (no state change), so this is safe to call
        any number of times; it never creates another session record."""
        topic = await self.topic_repository.get_by_id(target.topic_id) if target.topic_id else None
        if topic is None:
            raise NotFoundException("Topic not found")
        settings = get_settings()
        student = await self.user_repository.get_by_id(target.student_id)
        if student is None:
            raise NotFoundException("Student not found")
        # Resolved from the TARGET session's own stamped context, never
        # re-derived from the student's CURRENT active enrollments - a student
        # who has since gained a second enrollment must not turn re-minting a
        # token for an existing (possibly legacy) session into an
        # ambiguous-enrollment error.
        if target.enrollment_id:
            context = await self.enrollment_service.resolve_context(student, target.enrollment_id)
        else:
            context = EnrollmentContext(None, target.program_id, target.program_level_id, None)
        max_duration_minutes = await self.progress_service.get_topic_session_minutes_for_context(student, target.topic_id, context)
        metadata = {
            "project": "tamakkan",
            "session_id": target.id,
            "topic_id": topic.id,
            "max_duration_minutes": max_duration_minutes,
            "lesson_title": topic.title,
            "lesson_prompt": wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", topic.voice_practice_prompt),
            "opening_line": sanitize_untrusted_text(topic.opening_line),
            "level": topic.level.value,
            "arabic_support_enabled": topic.arabic_support_enabled,
            "arabic_intervention_level": topic.arabic_intervention_level.value,
        }
        if target.continuation_context:
            metadata["continuation"] = target.continuation_context
        token = (
            livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
            .with_identity(f"student-{target.student_id}")
            .with_name(student.full_name)
            .with_grants(livekit_api.VideoGrants(room_join=True, room=target.livekit_room_name))
            .with_room_config(
                livekit_api.RoomConfiguration(
                    agents=[
                        livekit_api.RoomAgentDispatch(
                            agent_name=settings.LIVEKIT_VOICE_AGENT_NAME, metadata=json.dumps(metadata)
                        )
                    ]
                )
            )
        )
        return SessionStartResponse(
            server_url=settings.LIVEKIT_URL,
            participant_token=token.to_jwt(),
            session_id=target.id,
            room_name=target.livekit_room_name,
            max_duration_minutes=max_duration_minutes,
        )

    async def list_for_student(self, student: User) -> list[LiveSession]:
        return await self.session_repository.get_all_for_student(student.id)

    async def list_for_admin(self, admin: User) -> list[LiveSession]:
        """The Center Admin (`Role.ADMINISTRATOR`) sees every session in the center; a Teacher sees only their
        own roster's."""
        roster = (
            await self.user_repository.get_all_students()
            if admin.role == Role.ADMINISTRATOR
            else await self.user_repository.get_students_created_by(admin.id)
        )
        roster_ids = [s.id for s in roster]
        if not roster_ids:
            return []
        return await self.session_repository.get_all_for_admin(roster_ids)

    async def search_for_admin(
        self,
        admin: User,
        *,
        search: Optional[str] = None,
        student_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        mode: Optional[PracticeMode] = None,
        report_status: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_before: Optional[datetime] = None,
        page: int = 1,
        limit: Optional[int] = None,
    ) -> tuple[list[LiveSession], int]:
        """The admin's AI Reports / sessions list with server-side filters, search and a page + total. Restricted to the
        caller's own roster, except the Center Admin (`Role.ADMINISTRATOR`), who gets every student in the center. The
        time window is the half-open range [`started_from`, `started_before`) of explicit instants; a value without an
        offset is read as UTC, one with an offset is converted to UTC (sessions are stored in UTC)."""
        started_from = _as_naive_utc(started_from)
        started_before = _as_naive_utc(started_before)
        if started_from is not None and started_before is not None and started_from >= started_before:
            raise UnprocessableException("started_from must be before started_before")
        roster = (
            await self.user_repository.get_all_students()
            if admin.role == Role.ADMINISTRATOR
            else await self.user_repository.get_students_created_by(admin.id)
        )
        roster_ids = [s.id for s in roster]
        if not roster_ids:
            return [], 0
        search_student_ids = search_topic_ids = None
        text = (search or "").strip()
        if text:
            needle = text.lower()
            search_student_ids = [s.id for s in roster if needle in s.full_name.lower() or needle in s.email.lower()]
            search_topic_ids = await self.topic_repository.find_ids_by_title(text)
        return await self.session_repository.search(
            roster_ids,
            student_id=student_id,
            topic_id=topic_id,
            mode=mode,
            report_status=report_status,
            started_from=started_from,
            started_before=started_before,
            search_student_ids=search_student_ids,
            search_topic_ids=search_topic_ids,
            skip=(page - 1) * limit if limit else 0,
            limit=limit,
        )

    async def get_one(self, session_id: str, requester: User) -> LiveSession:
        """Same ownership rule as `RecordingSubmissionService.get_one`: the
        owning Student, or that Student's own owning Teacher - never any
        Teacher, never a different Student. The Center Admin
        (`Role.ADMINISTRATOR`) bypasses this entirely, same reach as every
        other center-wide action.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Session not found")

        if requester.role == Role.STUDENT:
            if session.student_id != requester.id:
                raise ForbiddenException("This session does not belong to you")
            return session

        if requester.role == Role.ADMINISTRATOR:
            return session

        student = await self.user_repository.get_by_id(session.student_id)
        if student is None or student.created_by_id != requester.id:
            raise ForbiddenException("This student is not assigned to you")
        return session

    async def get_open_avatar_session(self, session_id: str, student: User) -> LiveSession:
        """The session an avatar token may be issued for: the caller's OWN Avatar Practice session that is still open.
        Ownership is the same rule as every other session read (`get_one`); a Voice Practice session has no avatar and
        an ended session needs none, so neither can be used to mint a token."""
        session = await self.get_one(session_id, student)
        if session.mode != PracticeMode.TOPIC_DISCUSSION:
            raise UnprocessableException("Only Avatar Practice sessions use an avatar")
        if session.state not in OPEN_STATES:
            raise UnprocessableException("This session has already ended")
        return session

    async def force_end(self, session_id: str, admin: User) -> LiveSession:
        """FR-083: an Administrator can terminate any of their own students'
        active sessions. Deletes the LiveKit room (which drops every
        participant, including the Mariam worker) and marks the record
        `ENDED` with `end_reason=ADMIN_TERMINATED` - always audited, same as
        every other privileged action in this app.
        """
        session = await self.get_one(session_id, admin)
        if session.state in (SessionState.ENDED, SessionState.CONNECTION_FAILED):
            raise UnprocessableException("This session has already ended")

        settings = get_settings()
        async with livekit_api.LiveKitAPI(
            settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET
        ) as lk:
            await lk.room.delete_room(livekit_api.DeleteRoomRequest(room=session.livekit_room_name))

        # Deliberately bypasses `_advance`/`SESSION_TRANSITIONS`: the strict
        # table only allows Connected/Paused -> Ended for the *normal*
        # client-driven flow, but FR-083 lets an admin force-end a session in
        # any active state (including Connecting/Disconnected) - a distinct,
        # administrative override path, not another instance of the same
        # student-driven machine.
        before_state = session.state
        session.state = SessionState.ENDED
        session.end_reason = EndReason.ADMIN_TERMINATED
        saved = await self.session_repository.save(session)

        await self.audit_log_repository.save(
            AuditLog(
                actor=admin,
                action="force_end_session",
                target_type="LiveSession",
                target_id=session.id,
                before={"state": before_state.value},
                after={"state": SessionState.ENDED.value, "end_reason": EndReason.ADMIN_TERMINATED.value},
            )
        )
        return saved

    async def mark_connected(self, session_id: str) -> LiveSession:
        """Called by the `livekit_agent` worker once the room and avatar are
        actually live. An unreachable transition (e.g. the session already
        ended) is logged and ignored rather than raised - there's no student
        waiting on this specific HTTP response the way there is for
        student-facing routes, and the worker has nothing useful to do with
        a 409 mid-call.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Session not found")
        # Isolation: the internal worker routes only ever drive Avatar
        # Practice records; a Voice Practice session is never touched here.
        if session.mode != PracticeMode.TOPIC_DISCUSSION:
            logger.info("session connected [agent]: id={} ignored (mode={})", session.id, session.mode.value)
            return session
        if SessionState.CONNECTED not in SESSION_TRANSITIONS.get(session.state, set()):
            logger.info("session connected [agent]: id={} ignored (state={})", session.id, session.state.value)
            return session
        session.state = SessionState.CONNECTED
        logger.info("session connected [agent]: id={} mode={} state -> connected", session.id, session.mode.value)
        return await self.session_repository.save(session)

    async def record_live_progress(self, session_id: str, recent_turns: list[InteractionTurn]) -> LiveSession:
        """Called by the `livekit_agent` worker periodically, DURING a still-live Avatar call - a fire-and-forget,
        best-effort progress update, never the authoritative final transcript (that is still only `mark_ended`,
        unchanged). Overwrites `live_recent_turns` in place with whatever bounded window the worker currently has;
        never touches `transcript_history` (so scoring, reports and every existing reader of the real transcript are
        completely unaffected by this). Same "ignore an unreachable state" reasoning as `mark_connected`: there is no
        student waiting on this specific HTTP response, and a session that already ended or never started has
        nothing useful to record progress against.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Session not found")
        if session.mode != PracticeMode.TOPIC_DISCUSSION:  # same isolation as `mark_connected`/`mark_ended`
            logger.info("session progress [agent]: id={} ignored (mode={})", session.id, session.mode.value)
            return session
        if session.state not in OPEN_STATES:
            logger.info("session progress [agent]: id={} ignored (state={})", session.id, session.state.value)
            return session
        session.live_recent_turns = recent_turns
        return await self.session_repository.save(session)

    async def mark_ended(
        self, session_id: str, end_reason: EndReason, transcript_history: Optional[list[InteractionTurn]] = None
    ) -> MarkEndedResult:
        """Called by the `livekit_agent` worker once its job is finishing,
        however it ends (hangup, idle timeout, max duration, or a connection
        failure). Same "ignore an unreachable transition" reasoning as
        `mark_connected` above.

        `transcript_history` arrives here as one batched payload, the turns
        the worker buffered in memory over the whole call - not written
        incrementally per turn (see this worker's own `agent.py` docstring on
        why: a full-document save on every turn, sharing this process's
        single event loop with the live audio, was a real, measured
        contributor to Mariam's response lag). Only written when non-empty,
        so a worker that (for whatever reason) sends none never blanks out a
        transcript some other path already stored.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Session not found")
        if session.mode != PracticeMode.TOPIC_DISCUSSION:  # same isolation as `mark_connected`
            return MarkEndedResult(session=session, transitioned=False)
        target = SessionState.CONNECTION_FAILED if end_reason == EndReason.CONNECTION_FAILURE else SessionState.ENDED
        allowed = target in SESSION_TRANSITIONS.get(session.state, set())
        if not allowed and session.state == SessionState.CONNECTING and target == SessionState.ENDED and transcript_history:
            # The agent is reporting a FINISHED conversation with its own transcript, but its `mark-connected` never
            # arrived (a lost or failed callback). Without this the whole transcript was dropped and the session stayed
            # `connecting` forever. A transcript is proof the call happened; an end call with none still changes nothing.
            logger.warning("session {} ended while still 'connecting': its mark-connected callback was lost; accepting the trusted transcript", session.id)
            allowed = True
        if not allowed:
            return MarkEndedResult(session=session, transitioned=False)
        session.state = target
        session.end_reason = end_reason
        session.ended_at = utcnow()
        if transcript_history:
            session.transcript_history = transcript_history
        saved = await self.session_repository.save(session)
        return MarkEndedResult(session=saved, transitioned=True)

    async def record_voice_transcript(
        self, session_id: str, end_reason: EndReason, transcript_history: Optional[list[InteractionTurn]] = None
    ) -> MarkEndedResult:
        """Called by the Voice Practice AGENT (internal key, never a browser) when it has an authoritative, ordered
        transcript of its own conversation. A dedicated route for Voice, so Mariam's `mark-connected` / `mark-ended`
        keep ignoring Voice sessions (session isolation) and the browser's `browser_transcript` stays untrusted: it is
        never read here and never copied into `transcript_history`.

        Order-independent with the student's own `/end`: the browser usually ends the session first (state `ENDED`),
        and the agent's transcript then arrives late - it is attached without touching the state. If the agent ends
        first, the session ends here. The first trusted transcript wins; later calls change nothing. A session that
        never connected (`CONNECTION_FAILED`) is left alone.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Session not found")
        if session.mode != PracticeMode.VOICE_PRACTICE:
            return MarkEndedResult(session=session, transitioned=False)
        if not transcript_history or session.transcript_history:
            return MarkEndedResult(session=session, transitioned=False)
        if session.state in OPEN_STATES:
            failed = end_reason == EndReason.CONNECTION_FAILURE
            session.state = SessionState.CONNECTION_FAILED if failed else SessionState.ENDED
            session.end_reason = end_reason
            session.ended_at = utcnow()
        elif session.state != SessionState.ENDED:
            return MarkEndedResult(session=session, transitioned=False)
        session.transcript_history = transcript_history
        session.transcript_source = VOICE_AGENT_TRANSCRIPT_SOURCE
        return MarkEndedResult(session=await self.session_repository.save(session), transitioned=True)

    async def generate_report(self, session_id: str) -> None:
        """Runs via `BackgroundTasks` right after `mark_ended` reaches
        `ENDED` with a real transcript (see the route) - a real Gemini call,
        so not run inline on the request. Best-effort, same reasoning as
        `TranscriptPipelineService.run_content_drafting`: a failed or skipped
        score is a missed convenience, not a broken session record - nothing
        else in this app depends on a report existing.

        Never re-scores a session that already has a `report` - this is a
        one-shot pass per session (there's no "regenerate" action), and
        `ENDED` is terminal (see `SESSION_TRANSITIONS`), so this only ever
        has one real caller per session in the first place; the extra check
        is what makes a duplicate `mark-ended` call's background task
        harmless rather than double-billing a Gemini call.

        Short on evidence (see `has_enough_evidence`)? Still writes a report,
        with null scores and `analysis.status == "insufficient_evidence"` -
        a report record always exists once a real transcript came in, it's
        just not always a scored one.
        """
        session = await self.session_repository.get_by_id(session_id)
        if session is None or not session.transcript_history or session.report is not None:
            return
        # `transcript_history` is written ONLY by the internal agent routes (`mark_ended`, `record_voice_transcript`).
        # Defense in depth: a Voice session is scored only if the Voice agent's route stamped its transcript, so a
        # transcript-looking `transcript_history` from any other source is refused. `browser_transcript` is never read.
        if session.mode == PracticeMode.VOICE_PRACTICE and session.transcript_source != VOICE_AGENT_TRANSCRIPT_SOURCE:
            logger.error("report refused: id={} voice session has a transcript without the voice agent's provenance stamp", session.id)
            return

        metrics = transcript_metrics(session)
        logger.info(
            "report input: id={} mode={} trusted_turns={} student_turns={} student_words={} enough_evidence={}",
            session.id, session.mode.value, len(session.transcript_history), metrics.student_turns, metrics.student_words,
            has_enough_evidence(metrics),
        )
        analysis = LearningAnalysis(status="insufficient_evidence", metrics=metrics, target_level=session.assessment_level)
        scores = {name: None for name in SCORE_FIELDS}
        summary = "There is not enough student English to assess this session fairly. Try at least three developed answers before reviewing a score."
        if has_enough_evidence(metrics):
            transcript = json.dumps({
                "target_level": session.assessment_level,
                "turns": [{"turn_index": index + 1, "speaker": turn.speaker, "text": turn.text}
                          for index, turn in enumerate(session.transcript_history)],
            }, ensure_ascii=False)
            if len(transcript) > 100_000:
                logger.warning("Session {} exceeds the assessment input limit", session.id)
                return
            try:
                score = await self.session_scorer.score(transcript)
                if score.feedback is None:
                    raise ValueError("Assessment feedback is missing")
                validate_feedback(score.feedback, session.transcript_history)
                analysis.feedback = score.feedback
                analysis.status = score.feedback.status
                summary = score.summary
                if analysis.status == "assessed":
                    scores = {name: getattr(score, name) for name in SCORE_FIELDS}
                    if any(value is None for value in scores.values()):
                        raise ValueError("An assessed report requires all scores")
                    scores["overall_score"] = round(sum(scores[name] for name in SCORE_FIELDS[:4]) / 4)
            except SessionScoringNotConfigured as exc:
                # Without this line a missing key looks like any other failure: sessions end normally, nothing is
                # scored and nobody is told why. The message is our own text, so it is safe to log.
                logger.error(
                    "Session {} has a transcript but no report was generated: {}. Set it in the backend's .env and "
                    "restart the API; sessions that ended without a report are not re-scored automatically.",
                    session.id, exc,
                )
                return
            except (SessionScoringError, ValueError) as exc:
                # Do not log model output or student speech on validation failure: the exception class (and the class
                # of what caused it, e.g. a provider HTTP error) says what went wrong without carrying any content.
                cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else "-"
                logger.warning("Assessment failed for session {} ({}, cause {})", session.id, type(exc).__name__, cause)
                return
        report = SpeakingReport(**scores, summary=summary, generated_at=utcnow(), analysis=analysis)
        if analysis.status == "assessed":
            history = await self.session_repository.get_all_for_student(session.student_id)
            analysis.comparison = comparison_for(session, report, history)
        current = await self.session_repository.get_by_id(session.id)
        if current is None or current.report is not None:
            return
        current.report = report
        await self.session_repository.save(current)
        logger.info("Session report generated for session {}", session_id)

    async def mark_report_reviewed(self, session_id: str, admin: User, reviewed: bool) -> LiveSession:
        """Administrator only (enforced by the route) - reuses `get_one`'s
        ownership check (that student's own owning Administrator, not any
        Administrator), same pattern as `RecordingSubmissionService
        .mark_reviewed`.
        """
        session = await self.get_one(session_id, admin)
        if session.report is None:
            raise UnprocessableException("This session has no report to review yet")
        session.report.reviewed = reviewed
        return await self.session_repository.save(session)

    async def confirm_voice_connected(self, session_id: str, student: User) -> LiveSession:
        """Student-driven `mark_connected` for Voice Practice. The browser
        only *asks*; the transition happens only if LiveKit itself lists an
        agent participant in this session's own room, so `CONNECTED` (and
        hence a later `ENDED`) always means the hosted agent really joined.
        Idempotent: an already-connected/ended session is returned as is.
        """
        session = await self._get_own_voice_session(session_id, student)
        if session.state != SessionState.CONNECTING:
            return session
        if not await self._agent_in_room(session.livekit_room_name):
            raise ConflictException("The voice agent has not joined this session yet")
        session.state = SessionState.CONNECTED
        return await self.session_repository.save(session)

    async def end_voice_session(
        self,
        session_id: str,
        student: User,
        end_reason: EndReason,
        browser_transcript: Optional[list[InteractionTurn]] = None,
    ) -> LiveSession:
        """Student-driven `mark_ended` for Voice Practice. Idempotent: a
        session that already ended is returned unchanged (its first recorded
        outcome and transcript are kept). Always deletes the session's own
        LiveKit room, best-effort, so the hosted agent is released even if an
        earlier end attempt never reached LiveKit.

        A session never verified as `CONNECTED` ends as `CONNECTION_FAILED`
        whatever the browser claims. The submitted transcript is stored only
        as `browser_transcript` - never `transcript_history` - and no report
        is scheduled: browser-captured text is not a trustworthy basis for
        teacher-facing scoring.
        """
        if end_reason not in STUDENT_REPORTABLE_END_REASONS:
            raise UnprocessableException("This end reason cannot be reported by a student")
        session = await self._get_own_voice_session(session_id, student)
        await self._delete_room_quietly(session.livekit_room_name)
        if session.state not in OPEN_STATES:
            return session

        state_before = session.state.value
        if session.state == SessionState.CONNECTED and end_reason != EndReason.CONNECTION_FAILURE:
            session.state, session.end_reason = SessionState.ENDED, end_reason
        else:
            session.state, session.end_reason = SessionState.CONNECTION_FAILED, EndReason.CONNECTION_FAILURE
        session.ended_at = utcnow()
        if browser_transcript:
            session.browser_transcript = browser_transcript
        saved = await self.session_repository.save(session)
        log_session_end(
            "student-browser", MarkEndedResult(session=saved, transitioned=True), state_before, False,
            "browser transcript is untrusted; a report needs the voice agent's own transcript (voice-transcript route)",
        )
        return saved

    async def _get_own_voice_session(self, session_id: str, student: User) -> LiveSession:
        session = await self.get_one(session_id, student)
        if session.mode != PracticeMode.VOICE_PRACTICE:
            # Mariam's lifecycle belongs to her worker (internal routes only).
            raise UnprocessableException("Only Voice Practice sessions can be updated by the student")
        return session

    async def _close_open_voice_sessions(self, student: User) -> None:
        for stale in await self.session_repository.get_all_for_student(student.id):
            if stale.mode == PracticeMode.VOICE_PRACTICE and stale.state in OPEN_STATES:
                await self.end_voice_session(stale.id, student, EndReason.USER_ENDED)

    async def _agent_in_room(self, room_name: str) -> bool:
        settings = get_settings()
        try:
            async with livekit_api.LiveKitAPI(
                settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET
            ) as lk:
                response = await lk.room.list_participants(livekit_api.ListParticipantsRequest(room=room_name))
        except Exception as exc:  # room not created yet, or LiveKit unreachable
            logger.info("Could not list participants for room {} ({})", room_name, type(exc).__name__)
            return False
        return any(p.kind == livekit_api.ParticipantInfo.Kind.AGENT for p in response.participants)

    async def _delete_room_quietly(self, room_name: str) -> None:
        if not room_name:
            return
        settings = get_settings()
        try:
            async with livekit_api.LiveKitAPI(
                settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET
            ) as lk:
                await lk.room.delete_room(livekit_api.DeleteRoomRequest(room=room_name))
        except Exception as exc:  # already gone (404) or LiveKit unreachable
            logger.info("Room {} not deleted ({})", room_name, type(exc).__name__)

    @staticmethod
    def _checkpoint_transcript_excerpt(topic: TopicContent, checkpoint: InteractiveVideoCheckpoint) -> str:
        """Transcript text strictly BEFORE this checkpoint's timestamp - never
        after. Built from `raw_transcript` (the same timed-segment source the
        teacher's Video Studio timeline/AI drafting already read), not the
        full `approved_transcript`, which has no per-segment timestamps to
        filter by."""
        segments = sorted(
            (segment for segment in topic.raw_transcript if segment.start < checkpoint.timestamp_seconds),
            key=lambda segment: segment.start,
        )
        return "\n".join(segment.text for segment in segments if segment.text.strip())

    def _build_checkpoint_topic_instructions(
        self,
        topic: TopicContent,
        checkpoint: InteractiveVideoCheckpoint,
        delivery_level,
        delivery_level_name: str,
        delivery_guidance: str,
    ) -> str:
        """A scoped, checkpoint-specific context block for a Practice with
        Mariam session - deliberately NOT the topic's generic `avatar_prompt`
        (a Practice with Mariam round is a short, narrow spoken-practice
        moment, not a general topic discussion). Each untrusted piece is
        wrapped/sanitized via `app.core.prompt_safety` before being joined."""
        blocks: list[str] = []
        if topic.learning_objectives:
            objectives_text = "\n".join(f"- {objective.text}" for objective in topic.learning_objectives)
            block = wrap_untrusted_lesson_content("LESSON LEARNING OBJECTIVES", objectives_text)
            if block:
                blocks.append(block)
        practice_text = (
            f"Learning objective for this practice: {checkpoint.learning_objective or ''}\n\n"
            f"Conversation goal: {checkpoint.conversation_goal or ''}"
        )
        block = wrap_untrusted_lesson_content("PRACTICE WITH MARIAM INSTRUCTIONS", practice_text)
        if block:
            blocks.append(block)
        excerpt = self._checkpoint_transcript_excerpt(topic, checkpoint)
        block = wrap_untrusted_lesson_content("VIDEO TRANSCRIPT UP TO THIS POINT", excerpt)
        if block:
            blocks.append(block)
        guidance_note = f" ({delivery_level_name}: {delivery_guidance})" if delivery_guidance else ""
        blocks.append(
            f"Student level: {delivery_level.value}{guidance_note}. This is a short, scoped spoken-practice "
            "round for the ONE concept just taught at this checkpoint - never a general topic discussion. Do "
            "not reveal or discuss any video content beyond this checkpoint."
        )
        return "\n\n".join(blocks)

    async def get_checkpoint_session_status(self, student: User, checkpoint_id: str) -> "CheckpointSessionStatus":
        """Whether THIS student has a genuinely completed Practice with Mariam
        session for this one checkpoint (see `is_checkpoint_practice_completed`
        - reaching `ENDED` alone is not enough) - the lightweight completion
        signal a Practice with Mariam checkpoint needs instead of an exercise
        attempt (see `app.api.v1.topics.get_checkpoint_session_status`).
        Scoped to the requesting student only - another student's sessions
        for the same checkpoint are never visible here."""
        sessions = await self.session_repository.get_for_student_and_checkpoint(student.id, checkpoint_id)
        latest = sessions[0] if sessions else None
        completed = any(is_checkpoint_practice_completed(session) for session in sessions)
        return CheckpointSessionStatus(
            completed=completed,
            latest_session_id=latest.id if latest else None,
            latest_session_state=latest.state if latest else None,
        )

    def _advance(self, current: SessionState, target: SessionState) -> SessionState:
        try:
            return transition(current, target, SESSION_TRANSITIONS)
        except InvalidTransition as exc:
            raise ConflictException(str(exc)) from exc


def get_live_session_service() -> LiveSessionService:
    return LiveSessionService(
        LiveSessionRepository(),
        TopicContentRepository(),
        UserRepository(),
        AuditLogRepository(),
        get_session_scorer(),
        get_progress_service(),
        get_enrollment_service(),
    )
