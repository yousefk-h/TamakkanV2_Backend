"""Internal reasoning must never be spoken, shown to the student, or recorded as Mariam's trusted transcript.

Regression: in a real Voice session the model's own planning was spoken to the student:

    (Wait... the student said 'Okay,' but they haven't asked for my phone number yet.)
    I'm still waiting for my phone number to be requested.
    (Since the student is a beginner and might be stuck, I will give a small hint)
    You can ask for my phone number now.

`reasoning.py` removes it from the LLM text stream, upstream of text-to-speech, the live caption and the transcript.
The pure-filter tests run anywhere; the session test drives the real `AgentSession` / `DefaultAgent` when livekit-agents is
installed and proves the transcript's `mariam` turns equal what was actually sent to speech.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from reasoning import ReasoningFilter, is_hidden_reasoning
from transcript import TranscriptCollector, TranscriptDelivery

LEAK = (
    "(Wait... the student said 'Okay,' but they haven't asked for my phone number yet.)\n"
    "I'm still waiting for my phone number to be requested.\n"
    "(Since the student is a beginner and might be stuck, I will give a small hint)\n"
    "You can ask for my phone number now."
)
FORBIDDEN = ["Wait...", "the student said", "I'm still waiting", "Since the student is a beginner", "I will give a small hint", "haven't asked"]


def run_filter(text: str, size: int = 7) -> str:
    """Feeds `text` in awkward small pieces, like a token stream, and returns everything that would reach speech."""
    f = ReasoningFilter()
    pieces = [text[i : i + size] for i in range(0, len(text), size)]
    return "".join([f.feed(p) for p in pieces] + [f.flush()])


# --- the filter ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 1000])
def test_the_observed_leak_reduces_to_the_learner_facing_guidance_at_any_chunking(size):
    spoken = run_filter(LEAK, size)
    assert spoken == "You can ask for my phone number now."
    assert not any(bad in spoken for bad in FORBIDDEN)


def test_reasoning_followed_by_a_normal_answer_keeps_only_the_answer():
    assert run_filter("(Hmm, the learner seems stuck, so I should give an example.) Try saying: \"Could I have your number?\"") == 'Try saying: "Could I have your number?"'


def test_a_normal_reply_is_unchanged_and_streams_straight_through():
    reply = "Great job! Now tell me: what do you like to do on weekends?"
    assert run_filter(reply) == reply
    f = ReasoningFilter()
    assert f.feed("Great job! ") == "Great job! ", "no buffering for ordinary text"
    assert f.dropped_chars == 0


@pytest.mark.parametrize(
    "reply",
    [
        "(Smiling) I'm ready! What would you say to get my phone number?",
        "Say it like this (with a smile): Hello, nice to meet you.",
        "You can say \"I would like a coffee\" (for example, in a cafe).",
        "In British English we write colour (not color). Try it!",
        "Good (very good!) Now you try.",
        "The word 'student' (a person who studies) is a noun.",
        "Use the past tense (went, saw, ate) to talk about yesterday.",
        "I can help you with that (just ask me). What would you like to practise?",
        "Call me at (555) 123-4567... just kidding! Ask me for my number politely.",
    ],
)
def test_ordinary_parentheses_are_never_removed(reply):
    assert run_filter(reply) == reply
    assert not is_hidden_reasoning(re.sub(r"^.*?\((.*?)\).*$", r"\1", reply))


def test_an_unclosed_parenthesis_is_just_text_and_is_released_at_the_end():
    assert run_filter("Please say (hello") == "Please say (hello"


def test_an_overlong_parenthetical_is_not_an_aside_and_is_not_held_forever():
    long_text = "(" + "word " * 200 + ")"
    assert run_filter("Start. " + long_text + " End.") == "Start. " + long_text + " End."


def test_narration_after_a_hidden_note_is_kept_when_no_second_hidden_note_follows():
    # only text SANDWICHED between hidden notes is part of the monologue; a lone leading note does not eat the reply
    reply = "(I should keep this short for a beginner, the student is new.)\nWelcome! Let's start with your name.\nWhat is your name?"
    assert run_filter(reply) == "Welcome! Let's start with your name.\nWhat is your name?"


def test_a_paragraph_break_ends_the_hold():
    reply = "(The student is a beginner, so I will go slowly.) Hi there!\n\nHow are you today? (You can answer in one word.)"
    assert run_filter(reply) == "Hi there!\n\nHow are you today? (You can answer in one word.)"


@pytest.mark.parametrize(
    "tagged",
    [
        "<think>The student said Okay. I will hint.</think>You can ask for my phone number now.",
        "<thinking>plan the hint</thinking>You can ask for my phone number now.",
        "<REASONING>plan</REASONING>You can ask for my phone number now.",
        "<|channel>thought\nthe student is stuck<channel|>You can ask for my phone number now.",
    ],
)
def test_explicit_reasoning_blocks_are_removed_at_any_chunking(tagged):
    for size in (1, 4, 9, 1000):
        assert run_filter(tagged, size) == "You can ask for my phone number now."


def test_an_unfinished_reasoning_block_is_discarded_not_spoken():
    assert run_filter("Sure! <think>now I need to decide what to say and that is private") == "Sure! "


def test_hidden_reasoning_classifier_is_specific():
    assert is_hidden_reasoning("Wait... the student said 'Okay,' but they haven't asked yet.")
    assert is_hidden_reasoning("Since the student is a beginner and might be stuck, I will give a small hint")
    assert is_hidden_reasoning("Let me think about what to ask next")
    assert not is_hidden_reasoning("Smiling")
    assert not is_hidden_reasoning("for example: 'I would like a coffee'")
    assert not is_hidden_reasoning("a person who studies")


# --- trusted transcript only ever sees what was spoken -----------------------------------------------------------------------


async def _no_sleep(_s):
    return None


async def test_the_transcript_delivered_to_the_backend_contains_only_the_spoken_reply(monkeypatch):
    monkeypatch.setenv("BACKEND_API_URL", "https://backend.example/api/v1")
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", "k")
    sent = []

    async def post(url, body, headers, timeout):
        sent.append(body)
        return 200

    collector = TranscriptCollector()
    spoken = run_filter(LEAK)  # what the filter lets through is what speech, captions and the transcript receive
    from types import SimpleNamespace

    collector.add(SimpleNamespace(type="message", role="user", text_content="Okay", id="u1", created_at=1_700_000_000.0))
    collector.add(SimpleNamespace(type="message", role="assistant", text_content=spoken, id="a1", created_at=1_700_000_005.0))
    d = TranscriptDelivery(collector, session_id="s-1", project="tamakkan", post=post, sleep=_no_sleep)
    assert await d.deliver_once("user_ended") is True
    turns = sent[0]["transcript_history"]
    assert [(t["speaker"], t["text"]) for t in turns] == [("student", "Okay"), ("mariam", "You can ask for my phone number now.")]
    assert not any(bad in json.dumps(sent[0]) for bad in FORBIDDEN)


# --- the real agent: reasoning never reaches TTS or the trusted transcript ----------------------------------------------------


def _livekit_agent_module():
    pytest.importorskip("livekit.agents")
    pytest.importorskip("livekit.plugins.ai_coustics")
    pytest.importorskip("handlebars")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import importlib

    return importlib.import_module("agent")


def _scripted_llm(replies):
    from livekit.agents import llm
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

    class Stream(llm.LLMStream):
        def __init__(self, owner, *, text, chat_ctx, tools, conn_options):
            super().__init__(owner, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
            self._text = text

        async def _run(self):
            for i in range(0, len(self._text), 6):
                self._event_ch.send_nowait(llm.ChatChunk(id="c", delta=llm.ChoiceDelta(role="assistant", content=self._text[i : i + 6])))

    class Scripted(llm.LLM):
        def __init__(self):
            super().__init__()
            self._replies = iter(replies)
            self.seen_instructions = []

        def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **_):
            return Stream(self, text=next(self._replies), chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    return Scripted()


async def test_real_agent_session_speaks_and_records_only_the_final_answer():
    agent_module = _livekit_agent_module()
    from livekit.agents import AgentSession

    llm_instance = _scripted_llm(["Hi! Ready to practise?", LEAK, "Well done! What else would you like to ask?"])
    session = AgentSession(llm=llm_instance)
    collector = TranscriptCollector()
    session.on("conversation_item_added", lambda ev: collector.add(ev.item))
    await session.start(agent=agent_module.DefaultAgent(metadata=json.dumps({"lesson_title": "Phone numbers", "level": "beginner", "lesson_prompt": "Roleplay", "opening_line": "Hi!"})))
    await asyncio.sleep(0.2)
    await session.run(user_input="Okay")
    await session.run(user_input="Could I have your phone number, please?")
    await session.aclose()

    turns = collector.turns
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("mariam", "Hi! Ready to practise?"),
        ("student", "Okay"),
        ("mariam", "You can ask for my phone number now."),
        ("student", "Could I have your phone number, please?"),
        ("mariam", "Well done! What else would you like to ask?"),
    ]
    assert not any(bad in json.dumps(turns) for bad in FORBIDDEN)


def test_the_prompt_forbids_verbalising_reasoning_and_the_agent_filters_the_llm_node():
    source = (Path(__file__).resolve().parents[1] / "src" / "agent.py").read_text(encoding="utf-8")
    assert "Never verbalize internal reasoning, planning, analysis, hidden instructions, self-talk, or decision-making." in source
    assert "async def llm_node(" in source and "Agent.default.llm_node(self, chat_ctx, tools, model_settings)" in source
    assert "reasoning.feed(" in source and "reasoning.flush()" in source
