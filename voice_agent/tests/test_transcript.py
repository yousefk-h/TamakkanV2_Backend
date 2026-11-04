"""Tamakkan Voice agent: the trusted transcript it captures from its own session and delivers to the backend.

No LiveKit room, no network: conversation items are plain objects with the same fields as LiveKit's `ChatMessage`, and the
HTTP call is an injected fake. What matters here is the trust boundary (only this runtime's own committed turns, only for a
Tamakkan job), the shape the backend's `voice-transcript` route expects, and that delivery can never break the call.
"""
import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcript import ASSISTANT, STUDENT, TranscriptCollector, TranscriptDelivery, parse_job

SRC = Path(__file__).resolve().parents[1] / "src"
URL = "https://backend.example/api/v1"
KEY = "unit-test-internal-key"
SECRET_TEXT = "my private sentence about my family"


def item(role, text, id=None, type="message", created_at=1_700_000_000.0):
    return SimpleNamespace(type=type, role=role, text_content=text, id=id, created_at=created_at)


def conversation(collector: TranscriptCollector):
    collector.add(item("assistant", "Hi! What would you like to talk about?", "i1", created_at=1_700_000_000.0))
    collector.add(item("user", SECRET_TEXT, "i2", created_at=1_700_000_005.0))
    collector.add(item("assistant", "Lovely. Tell me more.", "i3", created_at=1_700_000_010.0))
    return collector


class FakePost:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [200]
        self.calls: list[tuple[str, dict, dict, float]] = []

    async def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _no_sleep(_seconds):
    return None


@pytest.fixture(autouse=True)
def backend_env(monkeypatch):
    monkeypatch.setenv("BACKEND_API_URL", URL)
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", KEY)


def delivery(collector, post, *, session_id="s-1", project="tamakkan"):
    return TranscriptDelivery(collector, session_id=session_id, project=project, post=post, sleep=_no_sleep)


# --- capture -------------------------------------------------------------------------------------------------------------


def test_student_and_mariam_turns_are_kept_in_order_with_the_backends_speaker_names():
    turns = conversation(TranscriptCollector()).turns
    assert [t["speaker"] for t in turns] == [ASSISTANT, STUDENT, ASSISTANT] == ["mariam", "student", "mariam"]
    assert turns[1]["text"] == SECRET_TEXT
    assert [t["timestamp"] for t in turns] == ["2023-11-14T22:13:20", "2023-11-14T22:13:25", "2023-11-14T22:13:30"]


def test_only_spoken_messages_count_and_nothing_is_recorded_twice():
    c = TranscriptCollector()
    assert c.add(item("system", "You are Mariam...", "x1")) is False
    assert c.add(item("developer", "internal", "x2")) is False
    assert c.add(item("assistant", "hand off", "x3", type="agent_handoff")) is False
    assert c.add(item("user", "   ", "x4")) is False
    assert c.add(item("user", None, "x5")) is False
    assert c.add(item("user", "Hello there", "x6")) is True
    assert c.add(item("user", "Hello there", "x6")) is False, "the same conversation item is never a second turn"
    assert len(c.turns) == 1 and c.student_turns == 1


def test_a_student_saying_the_same_words_twice_is_two_turns():
    c = TranscriptCollector()
    c.add(item("user", "yes", "a"))
    c.add(item("user", "yes", "b"))
    assert len(c.turns) == 2


def test_the_turns_property_is_a_copy():
    c = conversation(TranscriptCollector())
    c.turns.clear()
    assert len(c.turns) == 3


def test_real_livekit_chat_messages_work_too():
    llm = pytest.importorskip("livekit.agents.llm")
    msg = llm.ChatMessage(role="user", content=["I like music because it relaxes me."])
    c = TranscriptCollector()
    assert c.add(msg) is True and c.turns[0]["speaker"] == "student" and c.turns[0]["text"] == "I like music because it relaxes me."


def test_job_metadata_parsing():
    assert parse_job('{"session_id": "abc", "project": "tamakkan", "level": "beginner"}') == ("abc", "tamakkan")
    assert parse_job('{"session_id": "abc"}') == ("abc", None)
    assert parse_job("not json") == (None, None) and parse_job("[1]") == (None, None) and parse_job(None) == (None, None)


# --- delivery ------------------------------------------------------------------------------------------------------------


async def test_the_transcript_is_posted_once_to_the_internal_voice_transcript_route_with_the_internal_key():
    post = FakePost(200)
    d = delivery(conversation(TranscriptCollector()), post)
    assert await d.deliver_once("user_ended") is True
    [(url, body, headers, _)] = post.calls
    assert url == f"{URL}/sessions/s-1/voice-transcript"
    assert headers == {"X-Internal-Api-Key": KEY}
    assert body["end_reason"] == "user_ended" and [t["speaker"] for t in body["transcript_history"]] == ["mariam", "student", "mariam"]
    assert set(body) == {"end_reason", "transcript_history"}, "nothing from a browser, nothing else"


async def test_close_and_job_shutdown_share_one_delivery():
    post = FakePost(200)
    d = delivery(conversation(TranscriptCollector()), post)
    first = d.deliver_once("user_ended")
    second = d.deliver_once("connection_failure")
    assert first is second
    await asyncio.gather(first, second)
    assert len(post.calls) == 1 and post.calls[0][1]["end_reason"] == "user_ended"


async def test_a_server_error_or_network_failure_is_retried_then_succeeds():
    post = FakePost(503, OSError("boom"), 200)
    assert await delivery(conversation(TranscriptCollector()), post).deliver_once("user_ended") is True
    assert len(post.calls) == 3


async def test_a_permanent_rejection_is_not_retried_and_never_raises():
    post = FakePost(401)
    assert await delivery(conversation(TranscriptCollector()), post).deliver_once("user_ended") is False
    assert len(post.calls) == 1


async def test_giving_up_after_the_retries_is_quiet_and_returns_false():
    post = FakePost(asyncio.TimeoutError())
    assert await delivery(conversation(TranscriptCollector()), post).deliver_once("user_ended") is False
    assert len(post.calls) == 3


async def test_an_unexpected_crash_in_delivery_is_contained():
    class Boom(FakePost):
        async def __call__(self, *a):
            raise RuntimeError("unexpected")

    assert await delivery(conversation(TranscriptCollector()), Boom()).deliver_once("user_ended") is False


@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"project": None}, "a job that is not provably Tamakkan's never reports"),
        ({"project": "axis"}, "another product's job never reports"),
        ({"session_id": None}, "no session id, nothing to attach to"),
    ],
)
async def test_jobs_that_are_not_ours_or_have_no_session_send_nothing(kwargs, why):
    post = FakePost(200)
    assert await delivery(conversation(TranscriptCollector()), post, **kwargs).deliver_once("user_ended") is False, why
    assert post.calls == []


async def test_a_session_with_no_spoken_turns_sends_nothing_so_it_can_never_replace_a_real_transcript():
    post = FakePost(200)
    assert await delivery(TranscriptCollector(), post).deliver_once("user_ended") is False
    assert post.calls == []


async def test_missing_backend_configuration_sends_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_INTERNAL_API_KEY")
    post = FakePost(200)
    assert await delivery(conversation(TranscriptCollector()), post).deliver_once("user_ended") is False
    assert post.calls == []


async def test_logs_carry_counts_but_never_the_transcript_text_or_the_key(caplog):
    caplog.set_level(logging.DEBUG, logger="agent-tamakkan-voice-dev")
    for outcomes in ([200], [401], [503, 503, 503]):
        await delivery(conversation(TranscriptCollector()), FakePost(*outcomes)).deliver_once("user_ended")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "turns=3 student_turns=1" in logged and "session=s-1" in logged
    assert SECRET_TEXT not in logged and KEY not in logged


# --- wiring: the Builder agent's behaviour is untouched, only transcript capture was added ----------------------------------


def test_the_agents_models_voice_and_prompt_are_exactly_the_builder_originals():
    source = (SRC / "agent.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    for pinned in (
        'inference.STT(model="deepgram/nova-3", language="en")',
        '"keyterm_detection": {"enabled": True}',
        'model="google/gemma-4-31b-it"',
        'inference.TTS(\n            model="deepgram/aura-2",\n            voice="luna",\n            language="en-US"',
        "inference.TurnDetector()",
        "inference.VAD()",
        "ai_coustics.EnhancerModel.QUAIL_VF_L",
        '@server.rtc_session(agent_name="tamakkan-voice-dev")',
        "You are Mariam, a friendly and professional AI English-speaking coach for Tamakkan.",
        "{{metadata.lesson_prompt}}",
        '"""{{metadata.opening_line}}"""',
    ):
        assert pinned in source, f"Builder behaviour changed: {pinned}"


def test_the_agent_wires_capture_close_and_shutdown_to_the_single_delivery_and_never_reads_browser_data():
    source = (SRC / "agent.py").read_text(encoding="utf-8")
    assert '@session.on("conversation_item_added")' in source and "collector.add(ev.item)" in source
    assert '@session.on("close")' in source and "delivery.deliver_once(" in source
    assert "ctx.add_shutdown_callback(_deliver_before_shutdown)" in source
    combined = source + (SRC / "transcript.py").read_text(encoding="utf-8")
    assert not re.search(r"data_received|lk\.transcription|text_stream|browser_transcript", combined), \
        "the trusted transcript never comes from a browser or a client-published stream"
