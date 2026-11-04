"""`YouTubeVideoTranscriber` is the only way `TranscriptPipelineService` gets
a transcript for a topic's video (see app/core/transcription.py's docstring).
These tests fake the `google-genai` client entirely - no real Gemini call is
made - to exercise the 429 retry-with-backoff logic without actually
sleeping for real backoff durations or hitting a real quota.
"""

import pytest
from google.genai import errors
from types import SimpleNamespace
from unittest.mock import Mock

import app.core.transcription as transcription_module
from app.core.transcription import TranscriptionError, YouTubeVideoTranscriber


class _FakeUsage:
    total_input_tokens = 100
    total_output_tokens = 50
    total_tokens = 150


class _FakeInteraction:
    def __init__(self, text):
        self.output_text = text
        self.usage = _FakeUsage()


def _rate_limit_error():
    return errors.APIError(429, {"error": {"message": "Resource has been exhausted.", "code": "too_many_requests"}})


def _bad_request_error():
    return errors.APIError(400, {"error": {"message": "Invalid argument.", "code": "invalid_argument"}})


class _FakeInteractions:
    def __init__(self, results):
        # Each item is either an exception instance (raised) or a string
        # (wrapped as a successful `_FakeInteraction`).
        self._results = list(results)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeInteraction(result)


class _FakeAio:
    def __init__(self, interactions):
        self.interactions = interactions


class _FakeClient:
    def __init__(self, results):
        self.aio = _FakeAio(_FakeInteractions(results))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The retry loop's backoff would otherwise really sleep (5s, 10s, ...) -
    these tests only care that a retry happened, not real timing.
    """
    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(transcription_module.asyncio, "sleep", instant_sleep)


async def test_transcribe_returns_text_on_first_success(monkeypatch):
    fake_client = _FakeClient(["hello world"])
    monkeypatch.setattr(transcription_module, "_get_client", lambda: fake_client)

    text = await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")

    assert text == "hello world"
    assert fake_client.aio.interactions.calls == 1


async def test_transcribe_retries_a_429_and_then_succeeds(monkeypatch):
    fake_client = _FakeClient([_rate_limit_error(), _rate_limit_error(), "recovered"])
    monkeypatch.setattr(transcription_module, "_get_client", lambda: fake_client)

    text = await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")

    assert text == "recovered"
    assert fake_client.aio.interactions.calls == 3


async def test_transcribe_gives_up_after_exhausting_429_retries(monkeypatch):
    results = [_rate_limit_error() for _ in range(transcription_module.MAX_RATE_LIMIT_RETRIES + 1)]
    fake_client = _FakeClient(results)
    monkeypatch.setattr(transcription_module, "_get_client", lambda: fake_client)

    with pytest.raises(TranscriptionError):
        await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")

    assert fake_client.aio.interactions.calls == transcription_module.MAX_RATE_LIMIT_RETRIES + 1


async def test_transcribe_does_not_retry_a_non_429_api_error(monkeypatch):
    fake_client = _FakeClient([_bad_request_error(), "should never be reached"])
    monkeypatch.setattr(transcription_module, "_get_client", lambda: fake_client)

    with pytest.raises(TranscriptionError):
        await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")

    assert fake_client.aio.interactions.calls == 1


async def test_transcribe_raises_on_empty_output_text(monkeypatch):
    fake_client = _FakeClient([""])
    monkeypatch.setattr(transcription_module, "_get_client", lambda: fake_client)

    with pytest.raises(TranscriptionError):
        await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")


@pytest.mark.parametrize("api_key", ["", " \t "])
async def test_missing_profile_key_does_not_construct_sdk_client(monkeypatch, api_key):
    monkeypatch.setattr(transcription_module, "_client", None)
    monkeypatch.setattr(transcription_module, "get_settings", lambda: SimpleNamespace(GEMINI_API_KEY=api_key))
    # Another profile's inherited Google key must not mask missing configuration.
    monkeypatch.setenv("GOOGLE_API_KEY", "different-profile-key")
    constructor = Mock()
    monkeypatch.setattr(transcription_module.genai, "Client", constructor)

    with pytest.raises(TranscriptionError, match=r"GEMINI_API_KEY is missing.*backend\'s \.env"):
        await YouTubeVideoTranscriber().transcribe("https://youtu.be/abc123")

    constructor.assert_not_called()
    assert transcription_module._client is None


def test_client_uses_active_profile_key_and_is_reused(monkeypatch):
    monkeypatch.setattr(transcription_module, "_client", None)
    monkeypatch.setattr(transcription_module, "get_settings", lambda: SimpleNamespace(GEMINI_API_KEY=" profile-key "))
    constructor = Mock()
    monkeypatch.setattr(transcription_module.genai, "Client", constructor)

    first = transcription_module._get_client()
    assert transcription_module._get_client() is first
    constructor.assert_called_once_with(api_key="profile-key")
