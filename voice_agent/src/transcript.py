"""Trusted transcript capture and delivery for Tamakkan Voice Practice.

The transcript comes ONLY from this agent's own runtime: the conversation items the `AgentSession` itself committed
(what its speech-to-text heard the student say and what Mariam said). Nothing here reads, accepts or forwards anything a
browser sent. It is sent once, when the call ends, to the backend's internal `voice-transcript` route, which stores it as the
trusted `transcript_history` and schedules the one existing report pass. This module never scores anything and never calls
an AI provider; the session itself must never fail because of anything in here.

Plain Python on purpose (no LiveKit imports): the agent's wiring is in `agent.py`, the behaviour is testable without a room.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger("agent-tamakkan-voice-dev")

# The backend stamps `project` into every Voice dispatch (`LiveSessionService.start_session`). This agent is Tamakkan's own:
# a job that is not explicitly Tamakkan's never reports back, so it can never write into another product's records.
PROJECT = "tamakkan"

# The backend's `InteractionTurn` speakers.
STUDENT = "student"
ASSISTANT = "mariam"

BACKEND_CALL_TIMEOUT_SECONDS = 10.0
# One hiccup (backend restarting, tunnel blip, cold start) must not lose a whole conversation: network errors, 5xx and 429
# are retried after these pauses. Any other 4xx (wrong key / URL / route) is permanent and not retried.
BACKEND_CALL_RETRY_DELAYS_SECONDS = (1.0, 3.0)

# (url, json body, headers, timeout seconds) -> HTTP status code. Raises OSError / asyncio.TimeoutError on network failure.
PostFn = Callable[[str, dict, dict, float], Awaitable[int]]


class TranscriptCollector:
    """Ordered student + Mariam turns from the session's committed conversation items."""

    def __init__(self) -> None:
        self._turns: list[dict] = []
        self._seen_ids: set[str] = set()

    def add(self, item: object) -> bool:
        """Records one committed conversation item; returns whether it became a turn.

        Only real spoken `user` / `assistant` messages count: system/developer messages (the instructions) and anything
        that isn't a message are not part of the conversation. Empty text is skipped and an item is never recorded twice.
        """
        if getattr(item, "type", None) != "message":
            return False
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return False
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return False
        item_id = getattr(item, "id", None)
        if item_id:
            if item_id in self._seen_ids:
                return False
            self._seen_ids.add(item_id)
        created_at = getattr(item, "created_at", None)
        moment = datetime.fromtimestamp(created_at, tz=timezone.utc) if created_at else datetime.now(timezone.utc)
        self._turns.append(
            {
                "speaker": STUDENT if role == "user" else ASSISTANT,
                "text": text,
                # Naive UTC, like the Avatar agent and the backend's stored timestamps.
                "timestamp": moment.replace(tzinfo=None).isoformat(),
            }
        )
        return True

    @property
    def turns(self) -> list[dict]:
        return list(self._turns)

    @property
    def student_turns(self) -> int:
        return sum(1 for t in self._turns if t["speaker"] == STUDENT)


def parse_job(metadata: str | None) -> tuple[str | None, str | None]:
    """(session_id, project) from the job's dispatch metadata; either is None when absent or the metadata is malformed."""
    import json

    try:
        value = json.loads(metadata or "")
    except (TypeError, ValueError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    session_id, project = value.get("session_id"), value.get("project")
    return (str(session_id) if session_id else None, str(project) if project else None)


async def _aiohttp_post(url: str, body: dict, headers: dict, timeout: float) -> int:
    import aiohttp  # a dependency of livekit-agents; imported here so the module stays importable without it

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.post(url, json=body, headers=headers) as response:
                return response.status
    except aiohttp.ClientError as exc:
        raise OSError(type(exc).__name__) from exc


class TranscriptDelivery:
    """Sends the collected transcript to the backend exactly once. Never raises."""

    def __init__(
        self,
        collector: TranscriptCollector,
        *,
        session_id: str | None,
        project: str | None,
        post: PostFn | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._collector = collector
        self._session_id = session_id
        self._project = project
        self._post = post or _aiohttp_post
        self._sleep = sleep
        self._task: asyncio.Task | None = None

    def deliver_once(self, end_reason: str) -> "asyncio.Task":
        """Starts the delivery on the first call; later calls (session close, then job shutdown) share that same task."""
        if self._task is None:
            self._task = asyncio.ensure_future(self._deliver(end_reason))
        return self._task

    async def _deliver(self, end_reason: str) -> bool:
        try:
            return await self._send(end_reason)
        except Exception:  # noqa: BLE001 - a failed report must never fail or hang the call's teardown
            logger.exception("Voice transcript delivery crashed (session %s)", self._session_id)
            return False

    async def _send(self, end_reason: str) -> bool:
        session_id = self._session_id
        turns = self._collector.turns
        if not session_id:
            logger.warning("Voice job has no session_id in its metadata: nothing to report (turns=%d)", len(turns))
            return False
        if self._project != PROJECT:
            # Not provably Tamakkan's: report nothing rather than risk writing into another product's backend.
            logger.error("Voice job for session %s is not a Tamakkan job (project=%r): transcript not sent", session_id, self._project)
            return False
        if not turns:
            logger.info("Voice session %s ended with no spoken turns: nothing to send", session_id)
            return False
        base = os.environ.get("BACKEND_API_URL", "").rstrip("/")
        key = os.environ.get("AGENT_INTERNAL_API_KEY", "")
        if not base or not key:
            logger.error("Voice session %s: BACKEND_API_URL or AGENT_INTERNAL_API_KEY is not configured: transcript not sent", session_id)
            return False

        url = f"{base}/sessions/{session_id}/voice-transcript"
        body = {"end_reason": end_reason, "transcript_history": turns}
        headers = {"X-Internal-Api-Key": key}
        host = urlsplit(url).netloc
        # Counts only - never the text of what the student said.
        summary = f"session={session_id} turns={len(turns)} student_turns={self._collector.student_turns}"
        attempts = len(BACKEND_CALL_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(1, attempts + 1):
            try:
                status = await self._post(url, body, headers, BACKEND_CALL_TIMEOUT_SECONDS)
                if status < 400:
                    logger.info("Voice transcript delivered: %s HTTP %s (attempt %d)", summary, status, attempt)
                    return True
                if status < 500 and status != 429:
                    logger.error("Backend REJECTED the voice transcript: %s HTTP %s. Check BACKEND_API_URL (%s) and AGENT_INTERNAL_API_KEY.", summary, status, host)
                    return False
                failure = f"HTTP {status}"
            except (OSError, asyncio.TimeoutError) as exc:
                failure = type(exc).__name__
            if attempt < attempts:
                logger.warning("Voice transcript delivery failed (%s): %s attempt %d/%d - retrying", failure, summary, attempt, attempts)
                await self._sleep(BACKEND_CALL_RETRY_DELAYS_SECONDS[attempt - 1])
            else:
                logger.error("Could not deliver the voice transcript after %d attempts (%s): %s host %s", attempts, failure, summary, host)
        return False
