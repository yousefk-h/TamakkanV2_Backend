"""The agent's callbacks to the backend are what move a session out of `connecting` and deliver the trusted transcript
that reports are scored from. Production sessions stayed `connecting` with no transcript and nothing in any log. A single
transient failure must not lose a conversation, a permanent one (wrong key/URL) must be loud, and no log may carry speech.
No network, no keys."""

import logging
import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret")

from livekit_agent import agent as mariam  # noqa: E402
from livekit_agent.models import EndReason  # noqa: E402

TURNS = [{"speaker": "student", "text": "SECRET-STUDENT-WORDS about my morning routine", "timestamp": "2026-01-01T00:00:00"}]


@pytest.fixture
def backend(monkeypatch):
    calls: list[httpx.Request] = []
    script: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        step = script.pop(0) if script else 200
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step)

    real = httpx.AsyncClient
    monkeypatch.setattr(mariam.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mariam, "get_settings", lambda: SimpleNamespace(BACKEND_API_URL="https://backend.example/api/v1", AGENT_INTERNAL_API_KEY="agent-key"))
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(mariam.asyncio, "sleep", fake_sleep)
    return SimpleNamespace(calls=calls, script=script, slept=slept)


async def test_the_end_callback_carries_the_key_and_the_whole_trusted_transcript(backend):
    await mariam._mark_ended("sess-1", EndReason.USER_ENDED, TURNS)
    assert len(backend.calls) == 1
    request = backend.calls[0]
    assert str(request.url) == "https://backend.example/api/v1/sessions/sess-1/mark-ended"
    assert request.headers["X-Internal-Api-Key"] == "agent-key"
    assert b"SECRET-STUDENT-WORDS" in request.content and b'"end_reason":"user_ended"' in request.content.replace(b" ", b"")


@pytest.mark.parametrize("failures", [[503], [httpx.ConnectError("boom")], [429, 502]])
async def test_a_transient_failure_is_retried_so_the_transcript_is_not_lost(backend, failures):
    backend.script.extend(failures + [200])
    await mariam._mark_ended("sess-1", EndReason.USER_ENDED, TURNS)
    assert len(backend.calls) == len(failures) + 1
    assert backend.slept == list(mariam.BACKEND_CALL_RETRY_DELAYS_SECONDS)[: len(failures)]


async def test_a_persistent_outage_gives_up_loudly_without_raising(backend, caplog):
    backend.script.extend([500, 500, 500, 500])
    with caplog.at_level(logging.INFO, logger="mariam-agent"):
        await mariam._mark_ended("sess-1", EndReason.USER_ENDED, TURNS)  # must never break the live job
    assert len(backend.calls) == len(mariam.BACKEND_CALL_RETRY_DELAYS_SECONDS) + 1
    assert any("Could not report" in r.getMessage() and "backend.example" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403, 404, 422])
async def test_a_permanent_rejection_is_not_retried_and_names_the_likely_cause(backend, caplog, status):
    backend.script.append(status)
    with caplog.at_level(logging.INFO, logger="mariam-agent"):
        await mariam._mark_connected("sess-1")
    assert len(backend.calls) == 1 and backend.slept == []
    rejected = [r.getMessage() for r in caplog.records if "REJECTED" in r.getMessage()]
    assert rejected and f"HTTP {status}" in rejected[0] and "AGENT_INTERNAL_API_KEY" in rejected[0] and "agent-key" not in rejected[0]


async def test_flush_progress_posts_the_bounded_recent_turns_with_the_same_internal_key(backend):
    await mariam._flush_progress("sess-1", TURNS)
    assert len(backend.calls) == 1
    request = backend.calls[0]
    assert str(request.url) == "https://backend.example/api/v1/sessions/sess-1/flush-progress"
    assert request.headers["X-Internal-Api-Key"] == "agent-key"
    assert b"SECRET-STUDENT-WORDS" in request.content


async def test_flush_progress_skips_the_call_entirely_when_there_is_nothing_yet_to_report(backend):
    await mariam._flush_progress("sess-1", [])
    assert backend.calls == []


async def test_flush_progress_reuses_the_same_retry_backoff_as_mark_ended(backend):
    backend.script.extend([503, 200])
    await mariam._flush_progress("sess-1", TURNS)
    assert len(backend.calls) == 2
    assert backend.slept == [mariam.BACKEND_CALL_RETRY_DELAYS_SECONDS[0]]


async def test_agent_logs_carry_counts_never_speech(backend, caplog):
    with caplog.at_level(logging.DEBUG, logger="mariam-agent"):
        await mariam._mark_connected("sess-1")
        await mariam._mark_ended("sess-1", EndReason.USER_ENDED, TURNS)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "trusted_transcript_turns=1" in text and "Reported /sessions/sess-1/mark-ended" in text
    assert "SECRET-STUDENT-WORDS" not in text and "agent-key" not in text
