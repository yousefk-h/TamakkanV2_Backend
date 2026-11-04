"""Avatar Practice inbound Voice Isolation (DEV A/B test): the Mariam worker starts its AgentSession with LiveKit's
agent-side Krisp VIVA voice isolation (level 90), it is the ONLY enhanced noise processor in the audio path (ai-coustics
is fully removed), and nothing else about the session (models, turn-taking, Voice Practice, frontend) changed.
No network, no keys, no paid session."""

import importlib.util
import os
import re
from pathlib import Path

os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret")

from livekit.agents import room_io  # noqa: E402
from livekit.plugins import krisp  # noqa: E402

from livekit_agent import agent as mariam  # noqa: E402

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO = AGENT_DIR.parents[1]
SOURCE = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")


def _code(text: str) -> str:
    """Source without comments/docstrings, so prose can mention a setting without counting as using it."""
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    return re.sub(r"(?m)^\s*#.*$", "", text)


CODE = _code(SOURCE)


def test_avatar_room_options_use_krisp_viva_voice_isolation_level_90() -> None:
    options = mariam._avatar_room_options()
    assert isinstance(options, room_io.RoomOptions)
    processor = options.audio_input.noise_cancellation
    assert isinstance(processor, krisp.KrispVivaFilterFrameProcessor)
    assert processor.noise_suppression_level == 90
    assert str(processor._inner._mode) == "VivaMode.VOICE_ISOLATION"  # noqa: SLF001 - no public accessor; NOT the telephony mode
    assert mariam.KRISP_NOISE_SUPPRESSION_LEVEL == 90


def test_the_avatar_session_is_started_with_those_options() -> None:
    assert CODE.count("agent_session.start(") == 1, "exactly one AgentSession.start() in the Mariam worker"
    assert "agent_session.start(agent=mariam_agent, room=ctx.room, room_options=_avatar_room_options(diag))" in CODE


def test_exactly_one_enhanced_noise_processor_and_ai_coustics_is_gone() -> None:
    assert CODE.count("noise_cancellation") == 1, "one (and only one) noise_cancellation setting"
    assert CODE.count("krisp.voice_isolation(") == 1
    assert not re.search(r"ai_coustics|aic|quail|BVC|noise_filter|rnnoise|voice_isolation_telephony", CODE, re.IGNORECASE)
    requirements = (AGENT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*livekit-plugins-ai-coustics", requirements), "the ai-coustics plugin is no longer a dependency"
    assert importlib.util.find_spec("livekit.plugins.ai_coustics") is None, "ai-coustics is not installed in the validated environment"


def test_frontend_adds_no_enhanced_noise_cancellation() -> None:
    offenders = []
    for folder in ("lib", "components", "app"):
        for path in (REPO / "frontend" / folder).rglob("*"):
            if path.suffix in {".ts", ".tsx"} and re.search(r"noiseCancel|krisp|coustics|noise-filter", path.read_text(encoding="utf-8"), re.IGNORECASE):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
    package_json = (REPO / "frontend" / "package.json").read_text(encoding="utf-8")
    assert "krisp" not in package_json.lower() and "coustics" not in package_json.lower()


def test_models_and_turn_taking_are_unchanged() -> None:
    assert 'model="gpt-realtime-2.1-mini"' in CODE, "OpenAI Realtime primary unchanged"
    assert "ultravox.RealtimeModel(" in CODE and "RealtimeModelFallbackAdapter(" in CODE, "Ultravox fallback unchanged"
    for setting in ("turn_detection", "TurnHandlingOptions", "interruption", "eagerness", "vad=", "input_audio_noise_reduction"):
        assert setting not in CODE, f"{setting} must not be configured in this change"


def test_voice_practice_and_the_api_are_untouched() -> None:
    for path in (REPO / "backend" / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "krisp" not in text.lower() and "ai_coustics" not in text, f"{path.name} must not use the Avatar worker's noise processing"
    assert not (AGENT_DIR / "voice").exists()
    assert "tamakkan-voice" not in SOURCE, "this worker does not start or configure the Voice agent"


def test_dependencies_are_pinned_and_the_sdk_is_unchanged() -> None:
    requirements = (AGENT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^livekit-plugins-krisp==0\.3\.1$", requirements)
    assert re.search(r"(?m)^livekit-agents\[openai\]==1\.7\.1$", requirements)
    assert re.search(r"(?m)^livekit-plugins-ultravox==1\.7\.1$", requirements)
    assert re.search(r"(?m)^livekit-plugins-spatialreal==1\.8\.4$", requirements), "the NEW SpatialReal platform plugin"
