"""AI reports need GEMINI_API_KEY. Without it the API used to keep running and silently produce no reports (see the incident
note in tests/api/test_sessions.py). The scorer now says so explicitly and the API warns at startup."""

import types

import pytest
from fastapi import FastAPI
from loguru import logger

from app.core import application, session_scoring


@pytest.mark.parametrize("blank", ["", "   "])
async def test_the_real_scorer_refuses_without_a_key_and_never_builds_the_sdk(monkeypatch, blank):
    monkeypatch.setattr(session_scoring, "get_settings", lambda: types.SimpleNamespace(GEMINI_API_KEY=blank))
    monkeypatch.setattr(session_scoring, "_get_agent", lambda: (_ for _ in ()).throw(AssertionError("SDK must not be built")))
    with pytest.raises(session_scoring.SessionScoringNotConfigured, match="GEMINI_API_KEY"):
        await session_scoring.GeminiSessionScorer().score("{}")


def test_not_configured_is_a_scoring_error_so_existing_handling_still_applies():
    assert issubclass(session_scoring.SessionScoringNotConfigured, session_scoring.SessionScoringError)


async def _startup_logs(monkeypatch, key):
    async def noop():
        return None

    monkeypatch.setattr(application, "init_db", noop)
    monkeypatch.setattr(application, "close_db", noop)
    monkeypatch.setattr(application, "get_settings", lambda: types.SimpleNamespace(GEMINI_API_KEY=key))
    records = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level="INFO")
    try:
        async with application.lifespan(FastAPI()):
            pass
    finally:
        logger.remove(sink)
    return records


async def test_startup_warns_when_the_key_is_empty(monkeypatch):
    records = await _startup_logs(monkeypatch, "")
    assert any(level == "WARNING" and "GEMINI_API_KEY" in message for level, message in records)


async def test_startup_is_quiet_when_the_key_is_set(monkeypatch):
    records = await _startup_logs(monkeypatch, "configured")
    assert not any("GEMINI_API_KEY" in message for _, message in records)
