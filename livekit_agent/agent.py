"""Real-time "Discuss with Mariam" worker (Phase 4). A fully separate
deployable from the FastAPI backend - its own directory, its own
requirements, its own settings (see `config.py`), and no import of `app.*`
anywhere in this package. This is deliberate: when LiveKit Cloud builds and
deploys an agent, it packages that agent's own directory, not the whole
repo - so this worker can only ever rely on what lives under
`livekit_agent/`. It has no database connection of its own: everything it
needs to run a session (topic content, level, opening line) is handed to it
directly in the job's dispatch metadata, and the only state it reports back
to the main backend is two small HTTP calls (`_mark_connected`/`_mark_ended`)
- see this file's README for why (latency: a local Mongo connection sharing
this process's single event loop with the live audio/video session was a
real, measured contributor to Mariam's response lag).

Registered with LiveKit Cloud under `LIVEKIT_AGENT_NAME` (`config.py`,
default `"mariam-agent"`) - `LiveSessionService.start_session` (in the main
backend) mints a token with that same agent name dispatched into the room
whenever a student starts a session. The two must match exactly, and a
locally-run worker should use a name distinct from production's (see
`Settings.LIVEKIT_AGENT_NAME`'s docstring, on both sides) - LiveKit
dispatches by this name across every worker registered under it regardless
of who deployed it, so a local worker sharing production's name would have
real student sessions land on it too.

Run from the backend repository root for local dev (same convention as
`python -m app.main` for the API server, though this is otherwise an
unrelated process):
    python -m livekit_agent.agent dev      # local development, auto-reload
    python -m livekit_agent.agent start    # production

See this package's own README for full setup (LiveKit Cloud project,
SpatialReal account, environment variables).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentServer, JobContext, room_io
from livekit.agents import AgentSession as LiveKitAgentSession
from livekit.agents.llm import RealtimeModelFallbackAdapter
from livekit.agents.voice.events import CloseEvent, CloseReason, ConversationItemAddedEvent
from livekit.plugins import krisp, openai, spatialreal, ultravox

from livekit_agent.config import get_settings
from livekit_agent.diagnostics import AudioDiagnostics, diagnostics_enabled_for  # DIAG (temporary)
from livekit_agent.models import ArabicInterventionLevel, EndReason, Level
from livekit_agent.prompts import MARIAM_BASE_SYSTEM_PROMPT, build_arabic_support_instructions, build_level_instructions

load_dotenv(os.environ.get("TAMAKKAN_ENV_FILE", ".env"))

logger = logging.getLogger("mariam-agent")

# Read at import time - `@server.rtc_session(agent_name=...)` below is a
# decorator, evaluated once when this module loads, so it needs the real
# value already resolved rather than a lazy `get_settings()` call inside a
# function. See `Settings.LIVEKIT_AGENT_NAME`'s own docstring for why this
# must match the main backend's dispatch, and why a local run needs its own
# distinct name.
AGENT_NAME = get_settings().LIVEKIT_AGENT_NAME
IDLE_CHECK_INTERVAL_SECONDS = 15
# Best-effort calls back to the main backend - short enough that a slow or
# unreachable backend never meaningfully stalls the live call, which doesn't
# actually depend on these succeeding (see `_report_state` below).
BACKEND_CALL_TIMEOUT_SECONDS = 5.0
# A single hiccup (backend restarting, tunnel blip, cold start) must not lose a whole conversation's transcript: network
# errors, 5xx and 429 are retried after these pauses. Any other 4xx (wrong key / URL / route) is permanent and not retried.
BACKEND_CALL_RETRY_DELAYS_SECONDS = (1.0, 3.0)
# Live-progress flush (Avatar -> Cartoon handoff, mid-call case): every this-many NEW turns, push the last
# `PROGRESS_FLUSH_MAX_TURNS` turns to the backend's `/flush-progress` route, fire-and-forget, so a handoff triggered
# while the call is still live has something real to carry - see `_on_conversation_item_added` and
# `LiveSessionService.record_live_progress`. Small and simple on purpose: a full turn (student + Mariam) is usually
# two items, so this flushes roughly once per exchange - frequent enough that a dropped call loses at most one
# exchange's worth of progress, without spamming the backend every single item.
PROGRESS_FLUSH_EVERY_N_TURNS = 2
# Same bound as the backend's own `LiveSessionService.HANDOFF_CONTEXT_MAX_TURNS` - kept as its own local constant
# (this worker is a fully separate deployable with no import of `app.*`, see module docstring) rather than shared.
PROGRESS_FLUSH_MAX_TURNS = 8

server = AgentServer()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Krisp VIVA voice isolation strength (0-100). DEV A/B test of the noise-isolation model: this replaces the earlier
# ai-coustics QUAIL_VF_S, which passed loud table tapping and nearby speech (see the AUDIO_DIAG measurements).
KRISP_NOISE_SUPPRESSION_LEVEL = 90
NOISE_ISOLATION_LABEL = f"KRISP_VIVA_VOICE_ISOLATION_{KRISP_NOISE_SUPPRESSION_LEVEL}"


def _avatar_room_options(diag: AudioDiagnostics | None = None) -> room_io.RoomOptions:
    """Inbound Voice Isolation for the student's microphone: LiveKit's agent-side Krisp VIVA voice isolation runs on
    the audio the agent receives from the room, BEFORE it reaches the realtime model, so breathing, keyboard noise and
    nearby sounds are suppressed at the source. This is the only enhanced noise processing in the Avatar path - the
    student's browser keeps only its standard echo-cancellation / noise-suppression, and nothing else is stacked on
    top. Avatar Practice only: Voice Practice runs on a separate hosted agent that this worker does not start."""
    enhancer = krisp.voice_isolation(noise_suppression_level=KRISP_NOISE_SUPPRESSION_LEVEL)
    if diag is not None:  # DIAG (temporary): observe the enhancer; it is otherwise untouched
        diag.attach_enhancer(enhancer, model=NOISE_ISOLATION_LABEL)
    return room_io.RoomOptions(audio_input=room_io.AudioInputOptions(noise_cancellation=enhancer))


class MariamAgent(Agent):
    """The conversational persona itself - kept separate from the job
    entrypoint below so it (and the baseline guardrail prompt it always
    carries) can be constructed and exercised directly in tests, without a
    real room, LiveKit Cloud, or SpatialReal account.
    """

    def __init__(
        self,
        topic_instructions: str = "",
        level: Level = Level.INTERMEDIATE,
        arabic_support_enabled: bool = False,
        arabic_intervention_level: ArabicInterventionLevel = ArabicInterventionLevel.LOW,
    ) -> None:
        instructions = f"{MARIAM_BASE_SYSTEM_PROMPT}\n\n{build_level_instructions(level)}"
        # Additive only: a topic that leaves this off (the default, and the
        # only possibility before this feature existed) gets exactly the
        # base + level instructions above, byte-for-byte unchanged.
        if arabic_support_enabled:
            instructions = f"{instructions}\n\n{build_arabic_support_instructions(arabic_intervention_level)}"
        if topic_instructions:
            instructions = f"{instructions}\n\n{topic_instructions}"
        super().__init__(instructions=instructions)


async def _report_state(path: str, json_body: dict | None = None) -> None:
    """POSTs to one of the main backend's internal session-state routes
    (`mark-connected`/`mark-ended`). Deliberately swallows any failure
    (timeout, connection error, non-2xx): this worker's own job must keep
    running the live call regardless of whether the backend's record of that
    call's state is currently reachable - a missed update here means a
    session's `LiveSession.state` can lag or land wrong, which is a real but
    non-fatal cost, not a reason to drop the student's call.
    """
    settings = get_settings()
    url = f"{settings.BACKEND_API_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {"X-Internal-Api-Key": settings.AGENT_INTERNAL_API_KEY}
    attempts = len(BACKEND_CALL_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=BACKEND_CALL_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=json_body, headers=headers)
            status = response.status_code
            if status < 400:
                logger.info("Reported %s to the backend: HTTP %s (attempt %d)", path, status, attempt)
                return
            if status < 500 and status != 429:
                # Permanent: nothing about a retry will fix a wrong key, URL or route. Say so loudly - this is exactly
                # the failure that leaves a session stuck in "connecting" with no transcript and no report.
                logger.error(
                    "Backend REJECTED %s: HTTP %s. Check BACKEND_API_URL (%s) and AGENT_INTERNAL_API_KEY.",
                    path, status, urlsplit(url).netloc,
                )
                return
            failure = f"HTTP {status}"
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
        if attempt < attempts:
            logger.warning("Reporting %s failed (%s), attempt %d/%d - retrying", path, failure, attempt, attempts)
            await asyncio.sleep(BACKEND_CALL_RETRY_DELAYS_SECONDS[attempt - 1])
        else:
            logger.error("Could not report %s to the backend after %d attempts (%s, host %s)", path, attempts, failure, urlsplit(url).netloc)


async def _mark_connected(session_id: str) -> None:
    await _report_state(f"/sessions/{session_id}/mark-connected")


async def _flush_progress(session_id: str, recent_turns: list[dict]) -> None:
    """Best-effort, periodic mid-call progress push - see `PROGRESS_FLUSH_EVERY_N_TURNS`. Reuses `_report_state`'s
    same retry/backoff as `_mark_connected`/`_mark_ended`; nothing here blocks the live audio/event loop - callers
    schedule this as a background task (`asyncio.create_task`), never await it inline."""
    if not recent_turns:
        return
    await _report_state(f"/sessions/{session_id}/flush-progress", {"recent_turns": recent_turns})


async def _mark_ended(session_id: str, end_reason: EndReason, transcript_history: list[dict] | None = None) -> None:
    body: dict = {"end_reason": end_reason.value}
    if transcript_history:
        body["transcript_history"] = transcript_history
    # Counts only - never the text of what the student said.
    logger.info("Ending session %s: reason=%s trusted_transcript_turns=%d", session_id, end_reason.value, len(transcript_history or []))
    await _report_state(f"/sessions/{session_id}/mark-ended", body)


async def _close_room(room_name: str) -> None:
    """The agent leaving the room (job shutdown) does NOT disconnect the
    student - LiveKit only fires `RoomEvent.Disconnected` for a participant
    when their own connection drops or the room itself is deleted, never
    just because another participant (the agent) left. Without this, the
    student's frontend never learns the call ended: it keeps showing "live"
    indefinitely after the session ends (max duration or idle timeout),
    because the only signal it listens for is its own room disconnecting.
    Explicitly deleting the room kicks the student too, which is exactly
    what should happen - there's nothing useful left in an empty room with
    no agent in it.
    """
    settings = get_settings()
    if not (settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET):
        return
    try:
        async with api.LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET) as lk:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        logger.exception("Failed to close room %s after session end", room_name)


async def _wait_for_student(room: rtc.Room) -> rtc.RemoteParticipant:
    """Resolves once a participant with a `student-` identity (see
    `LiveSessionService.start_session`'s token minting) is in the room -
    deliberately narrower than `JobContext.wait_for_participant()`, which
    would just as happily resolve on SpatialReal's own avatar-rendering
    participant (a *separate* remote participant it joins as - see
    `AvatarSession._avatar_participant_identity`) since that one is usually
    already present by the time this runs.
    """
    for p in room.remote_participants.values():
        if p.identity.startswith("student-"):
            return p
    joined = asyncio.get_event_loop().create_future()

    def _on_connected(p: rtc.RemoteParticipant) -> None:
        if p.identity.startswith("student-") and not joined.done():
            joined.set_result(p)

    room.on("participant_connected", _on_connected)
    try:
        return await joined
    finally:
        room.off("participant_connected", _on_connected)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    await _run_session(ctx)


async def _run_session(ctx: JobContext) -> None:
    metadata = json.loads(ctx.job.metadata or "{}")
    session_id = metadata.get("session_id")
    max_duration_minutes = metadata.get("max_duration_minutes") or 15
    # Proves in the cloud logs that the job started at all, and where it will report to (host only).
    logger.info("Job started: session=%s backend_host=%s", session_id, urlsplit(get_settings().BACKEND_API_URL).netloc)

    # Handed down directly from `LiveSessionService.start_session` - this
    # worker has no database of its own to look them up from (see module
    # docstring).
    topic_instructions = metadata.get("topic_instructions") or ""
    opening_line = metadata.get("opening_line") or ""
    try:
        level = Level(metadata.get("level"))
    except ValueError:
        level = Level.INTERMEDIATE
    # Optional, teacher-controlled Jordanian-Arabic clarification support -
    # `None`/absent/anything falsy means "off" here (this agent never had any
    # Arabic behavior before this feature, unlike the separate Voice Practice
    # agent, so there is no legacy default to preserve on this path). See
    # `app.models.topic_content.TopicContent.arabic_support_enabled`.
    arabic_support_enabled = bool(metadata.get("arabic_support_enabled"))
    try:
        arabic_intervention_level = ArabicInterventionLevel(metadata.get("arabic_intervention_level"))
    except ValueError:
        arabic_intervention_level = ArabicInterventionLevel.LOW

    if opening_line:
        # Just the specific text - the general "if you're given a suggested
        # opening line, use it naturally, no need to repeat word-for-word"
        # behavior lives once in `MARIAM_BASE_SYSTEM_PROMPT` (prompts.py),
        # not reconstructed as prose here on every session. A plain
        # `generate_reply()` nudge (below, near the end of `_run_session`)
        # is still what actually makes her speak first - LiveKit's own
        # quickstart does the same unconditionally for every agent - but
        # unlike the old, removed mechanism it doesn't force any particular
        # wording: it just prompts her to begin, and she uses this guidance
        # naturally, in her own voice, same as any other instruction.
        topic_instructions = f'{topic_instructions}\n\nSuggested opening line for this topic: "{opening_line}"'

    settings = get_settings()

    # Set once by our own idle/max-duration timers below, so the "close"
    # handler reports the real reason instead of guessing from LiveKit's
    # own (coarser) `CloseReason`.
    pending_end_reason: EndReason | None = None

    # `mariam_agent` is built once, upfront: `agent_session.start()` reuses
    # it below, and the Ultravox fallback model needs its `.instructions`
    # baked into `system_prompt=` right now, at construction time (see that
    # model's own comment for why).
    mariam_agent = MariamAgent(topic_instructions, level, arabic_support_enabled, arabic_intervention_level)

    # OpenAI's Realtime API is primary; Ultravox is the fallback, wired
    # together with LiveKit's own `RealtimeModelFallbackAdapter` - if
    # OpenAI's session dies with an unrecoverable error, the adapter swaps
    # to Ultravox transparently (preserving chat context) rather than
    # ending the call. `models[0]` is always the primary; order matters.
    # Both models report `audio_output=True`/`turn_detection=True` (checked
    # via `RealtimeModel(...).capabilities` on the installed 1.7.1 plugins)
    # - the adapter requires every model to agree on those two specifically,
    # or it raises at construction time.
    agent_session = LiveKitAgentSession(
        llm=RealtimeModelFallbackAdapter(
            models=[
                openai.realtime.RealtimeModel(
                    model="gpt-realtime-2.1-mini",
                    api_key=settings.OPENAI_API_KEY,
                    # Plugin default is "marin" - untested here yet whether
                    # it actually suits Mariam's persona; swap freely.
                ),
                ultravox.RealtimeModel(
                    api_key=settings.ULTRAVOX_API_KEY,
                    # Ultravox's `RealtimeSession` has `mutable_instructions=
                    # False` (unlike OpenAI's `True`) - it does not accept
                    # updated instructions after a session starts, so its
                    # persona has to be baked into `system_prompt=` here,
                    # upfront, rather than left for `AgentSession.start()`
                    # to push dynamically the way OpenAI's model above
                    # allows. This matters specifically for the fallback
                    # case: if the adapter ever swaps to this model, it
                    # replays the captured instructions via
                    # `update_instructions()`, which Ultravox implements as
                    # "update `system_prompt` and restart the call" rather
                    # than a true live update - but having the right prompt
                    # here from the start avoids relying on that swap-time
                    # path being hit correctly on the very first call.
                    system_prompt=mariam_agent.instructions,
                    # One of Ultravox's own built-in voices ("Mark" or
                    # "Jessica" are the only two, per `UltravoxVoice` on the
                    # installed 1.7.1 plugin) - "Jessica" for a female
                    # voice, matching Mariam's persona. An `external_voice=
                    # {"inworld": {...}}` config was tried first, but
                    # reproducing the exact failing request directly against
                    # Ultravox's API (bypassing the plugin, which swallows
                    # the response body) surfaced the real error: "The
                    # requested voice provider is not enabled for this
                    # account" - an Ultravox account/plan setting, not
                    # something fixable here. Revisit `external_voice` once
                    # that's enabled, or switch to a provider that already
                    # is (elevenLabs was used successfully before).
                    voice="Jessica",
                ),
            ],
        ),
    )

    last_activity_at = _utcnow()
    # Buffered in memory and sent once, in the final `mark-ended` call below
    # - not written incrementally per turn. A full-document Mongo write on
    # every turn, competing with the live audio/video session for this
    # process's single event loop, was a real, measured contributor to
    # Mariam's response lag (see this package's README); a single batched
    # payload at the very end of an already-ending call costs nothing extra.
    transcript_turns: list[dict] = []
    # Avatar -> Cartoon handoff (mid-call case): how many new turns have landed since the last successful progress
    # flush. Fire-and-forget background tasks are kept here only so they aren't garbage-collected mid-flight; they
    # are never awaited by this handler (see `_flush_progress`'s own docstring on why).
    turns_since_last_flush = 0
    background_tasks: set[asyncio.Task] = set()

    def _on_conversation_item_added(ev: ConversationItemAddedEvent) -> None:
        nonlocal last_activity_at, turns_since_last_flush
        last_activity_at = _utcnow()

        item = ev.item
        # `item` can also be an `AgentHandoff` (multi-agent handoff, unused
        # here) - `type == "message"` is what narrows it to a real
        # `ChatMessage` with a `role`/`text_content`. "developer"/"system"
        # messages (the instructions/prompt themselves, not anything either
        # party said aloud) are deliberately excluded from the transcript.
        if getattr(item, "type", None) != "message" or item.role not in ("user", "assistant"):
            return
        text = item.text_content
        if not text:
            return
        transcript_turns.append(
            {
                "speaker": "student" if item.role == "user" else "mariam",
                "text": text,
                "timestamp": datetime.fromtimestamp(item.created_at, tz=timezone.utc).replace(tzinfo=None).isoformat(),
            }
        )

        turns_since_last_flush += 1
        if session_id and turns_since_last_flush >= PROGRESS_FLUSH_EVERY_N_TURNS:
            turns_since_last_flush = 0
            # Snapshot now (list slicing copies) - `transcript_turns` keeps growing after this task is scheduled, and
            # the task itself runs later, asynchronously. Fire-and-forget: never awaited here, so this never blocks
            # the realtime event loop handling this very callback.
            recent = transcript_turns[-PROGRESS_FLUSH_MAX_TURNS:]
            task = asyncio.create_task(_flush_progress(session_id, recent))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

    session_closed = asyncio.Event()

    def _on_close(ev: CloseEvent) -> None:
        reason = pending_end_reason
        if reason is None:
            reason = {
                CloseReason.USER_INITIATED: EndReason.USER_ENDED,
                CloseReason.PARTICIPANT_DISCONNECTED: EndReason.USER_ENDED,
                CloseReason.TASK_COMPLETED: EndReason.USER_ENDED,
                CloseReason.JOB_SHUTDOWN: EndReason.USER_ENDED,
                CloseReason.ERROR: EndReason.CONNECTION_FAILURE,
            }.get(ev.reason, EndReason.USER_ENDED)

        async def _finalize() -> None:
            if session_id:
                await _mark_ended(session_id, reason, transcript_turns)
            session_closed.set()

        asyncio.create_task(_finalize())

    agent_session.on("conversation_item_added", _on_conversation_item_added)
    agent_session.on("close", _on_close)
    # DIAG (temporary, dev-only): audio-path diagnostics for the noise-isolation investigation - see diagnostics.py.
    diag = AudioDiagnostics(enabled=diagnostics_enabled_for(AGENT_NAME), room=ctx.room.name, session_id=session_id)
    diag.session_start(model=NOISE_ISOLATION_LABEL, agent_name=AGENT_NAME)
    diag.hook_events(agent_session)

    async def _close_session() -> None:
        """Ends the call without a spoken farewell - see this file's README
        for why the scripted-announcement mechanism (opening trigger,
        time-warning, farewell) this used to go through was dropped
        entirely. `agent_session.aclose()` still lets LiveKit tear down the
        realtime session cleanly; `_close_room` is what actually removes the
        student too (see its own docstring for why that's needed).
        """
        await agent_session.aclose()
        await _close_room(ctx.room.name)

    async def _enforce_max_duration() -> None:
        nonlocal pending_end_reason
        await asyncio.sleep(max_duration_minutes * 60)
        pending_end_reason = EndReason.MAX_DURATION
        await _close_session()

    async def _enforce_idle_timeout() -> None:
        nonlocal pending_end_reason
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            idle_for = (_utcnow() - last_activity_at).total_seconds()
            if idle_for >= settings.MARIAM_IDLE_TIMEOUT_SECONDS:
                pending_end_reason = EndReason.IDLE_TIMEOUT
                await _close_session()
                return

    # Deliberately NOT created here: `_enforce_max_duration`'s clock starts
    # ticking the instant its task exists, with no way to reset it later like
    # `last_activity_at` below - creating it this early would burn a chunk of
    # the student's (sometimes only a few minutes - see
    # `get_topic_session_minutes`) session budget on avatar startup and
    # waiting for the student to connect. Started for real once the student
    # has actually joined - see below.
    max_duration_task: asyncio.Task[None] | None = None
    idle_timeout_task = asyncio.create_task(_enforce_idle_timeout())

    try:
        avatar = spatialreal.AvatarSession(
            api_key=settings.SPATIALREAL_API_KEY,
            app_id=settings.SPATIALREAL_APP_ID,
            avatar_id=settings.SPATIALREAL_AVATAR_ID,
        )
        await avatar.start(agent_session, room=ctx.room)

        # `avatar.wait_for_join()` and `agent_session.start()` run
        # concurrently, not sequentially: `agent_session.start()` is what
        # kicks off the realtime model's WebSocket connect (a background
        # task the call itself doesn't await - confirmed against the Gemini
        # plugin when this fix was written, and the same connect-in-the-
        # background pattern applies generically), and that connect
        # previously didn't even begin until after the avatar's own
        # up-to-30s join wait finished, adding a further ~10s of dead air
        # before Mariam could respond to anything. Overlapping the two lets
        # the (usually longer) avatar-join wait absorb the connect time
        # instead of adding to it in serial.
        await asyncio.gather(
            avatar.wait_for_join(),
            agent_session.start(agent=mariam_agent, room=ctx.room, room_options=_avatar_room_options(diag)),
        )

        diag.hook_realtime(agent_session)  # DIAG (temporary): raw OpenAI speech-start/stop events

        if session_id:
            await _mark_connected(session_id)
        # Start the idle clock only once the call is actually live - the
        # avatar join / session start above can take several seconds (up to
        # 30s), and counting that against the student's idle budget could
        # auto-end a brand-new session well before the real idle timeout.
        last_activity_at = _utcnow()

        # The student's browser goes through its own, much slower startup
        # (downloading avatar assets, initializing the WebGPU/WASM render
        # pipeline - see `useSpatialRealAvatar.ts`) before it even attempts to
        # join this room, while this job - with no comparable asset download
        # of its own - is usually ready within a second or two of being
        # dispatched. Waited for so the max-duration clock below doesn't
        # start ticking against a student who hasn't actually arrived yet.
        # Capped so a student who never connects (denied permissions, crashed
        # tab) doesn't strand the job waiting forever - a duration this low
        # (see `get_topic_session_minutes`) could genuinely take nearly this
        # long just to load the avatar on a slow connection.
        try:
            await asyncio.wait_for(_wait_for_student(ctx.room), timeout=25.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("No participant joined within 25s; starting the session anyway")

        # A realtime model still needs one explicit nudge to actually start
        # talking - confirmed against LiveKit's own quickstart, which calls
        # `generate_reply()` right after `session.start()` unconditionally,
        # for every agent template they ship (including realtime models).
        # Without this (tested against Gemini, before this worker switched
        # providers), Mariam never spoke a single word, in either direction
        # of the conversation - a realtime model's own "auto-responds once
        # VAD detects end of turn" behavior doesn't reliably engage through
        # LiveKit's session layer without this first explicit call. No
        # mic-hold or audio-muting here (unlike the old, removed mechanism)
        # - just the same bare nudge LiveKit's own docs use; Mariam decides
        # what to say from the opening-line guidance already in her base
        # prompt.
        try:
            handle = agent_session.generate_reply(
                instructions="Begin the conversation now, following the opening guidance in your instructions."
            )
            await asyncio.wait_for(handle.wait_for_playout(), timeout=10.0)
        except Exception:
            logger.exception("Initial greeting failed; continuing without it")

        # The real session clock: starts only once the student is actually in
        # the room, not at job start - see the comment on `max_duration_task`'s
        # declaration above for why. `last_activity_at` gets a matching reset
        # so the idle clock doesn't count this same startup stretch against
        # the student.
        last_activity_at = _utcnow()
        max_duration_task = asyncio.create_task(_enforce_max_duration())

        # Block here for the rest of the job's life - `entrypoint()`
        # returning tears down this job's own resources, so it must not
        # return until the session has actually ended (user hangup, idle
        # timeout, max duration, or error).
        await session_closed.wait()
    finally:
        if max_duration_task is not None:
            max_duration_task.cancel()
        idle_timeout_task.cancel()
        # Guarantee the backend never strands a session in `connected`: if
        # we're leaving without the `close` handler having already reported
        # the end (a crash in avatar/session startup, an unexpected error,
        # or an avatar-join timeout), send one best-effort final mark. A
        # duplicate is harmless - the backend's transition table makes a
        # second mark from an already-ended state a no-op.
        if session_id and not session_closed.is_set():
            await _mark_ended(session_id, EndReason.CONNECTION_FAILURE, transcript_turns)
            await _close_room(ctx.room.name)


if __name__ == "__main__":
    agents.cli.run_app(server)
