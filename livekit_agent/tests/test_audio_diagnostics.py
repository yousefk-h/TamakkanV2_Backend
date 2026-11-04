"""TEMPORARY diagnostics (livekit_agent/diagnostics.py) - delete together with that module. Verifies they are dev-only,
log only counters/timestamps (never audio or transcript text), never alter the audio, and detect a silent bypass."""

import os
import re
from pathlib import Path

import numpy as np

os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret")

from livekit import rtc  # noqa: E402
from livekit.plugins import krisp  # noqa: E402

from livekit_agent import agent as mariam  # noqa: E402
from livekit_agent.diagnostics import AIC_VAD_KEY, HEARTBEAT_SECONDS, AudioDiagnostics, diagnostics_enabled_for  # noqa: E402

AGENT_DIR = Path(__file__).resolve().parents[1]
FRAME_MS = 50
RATE = 24000
SAMPLES = RATE * FRAME_MS // 1000


def frame(amplitude: int = 0) -> rtc.AudioFrame:
    data = np.full(SAMPLES, amplitude, dtype=np.int16)
    return rtc.AudioFrame(data=data.tobytes(), sample_rate=RATE, num_channels=1, samples_per_channel=SAMPLES)


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeEnhancer:
    """Stands in for an enhancer: returns a NEW frame (processed), annotated with a VAD flag when `vad` is a bool
    (ai-coustics style) and with none when `vad` is None (Krisp style), or the very same frame object when bypassed -
    exactly how the real plugins behave."""

    def __init__(self) -> None:
        self.enabled = True
        self.vad: bool | None = False
        self.bypass = False
        self.seen: list[rtc.AudioFrame] = []

    def _process(self, f: rtc.AudioFrame) -> rtc.AudioFrame:
        self.seen.append(f)
        if self.bypass:
            return f
        out = rtc.AudioFrame(data=bytes(f.data), sample_rate=f.sample_rate, num_channels=f.num_channels, samples_per_channel=f.samples_per_channel)
        if self.vad is not None:
            out.userdata[AIC_VAD_KEY] = self.vad
        return out


def make(enabled: bool = True):
    clock, lines = Clock(), []
    diag = AudioDiagnostics(enabled=enabled, room="mariam-room-1", session_id="sess-1", clock=clock, emit=lines.append)
    enhancer = FakeEnhancer()
    diag.attach_enhancer(enhancer, model="QUAIL_VF_S")
    return diag, enhancer, clock, lines


def run(enhancer, clock, n, amplitude=100, ms=FRAME_MS):
    for _ in range(n):
        enhancer._process(frame(amplitude))
        clock.now += ms / 1000.0


def events(lines):
    return [line.split()[1] for line in lines]


def test_only_dev_agents_enable_diagnostics() -> None:
    assert diagnostics_enabled_for("tamakkan-mariam-dev")
    for production_like in ("mariam-agent", "tamakkan-mariam-v2", "tamakkan-mariam", ""):
        assert not diagnostics_enabled_for(production_like)


def test_disabled_diagnostics_do_nothing_at_all() -> None:
    diag, enhancer, clock, lines = make(enabled=False)
    original = FakeEnhancer._process
    assert type(enhancer)._process is original and "_process" not in vars(enhancer), "the processor was not wrapped"
    run(enhancer, clock, 50)
    diag.session_start(model="QUAIL_VF_S", agent_name="x")
    assert lines == []


def test_instantiation_and_first_frame_are_logged_once_with_passed_through_true() -> None:
    _, enhancer, clock, lines = make()
    assert events(lines) == ["enhancer_instantiated"]
    run(enhancer, clock, 30)
    first = [line for line in lines if " first_frame_processed " in line]
    assert len(first) == 1 and "passed_through=True" in first[0] and "room=mariam-room-1" in first[0] and "session=sess-1" in first[0]


def test_a_silent_bypass_is_detected_and_reported_rate_limited() -> None:
    _, enhancer, clock, lines = make()
    enhancer.bypass = True
    run(enhancer, clock, 400)  # 20 s of bypassed frames
    assert "passed_through=False" in next(line for line in lines if " first_frame_processed " in line)
    bypass = [line for line in lines if " enhancer_bypass " in line]
    assert 1 <= len(bypass) <= 3, "rate-limited, not one line per frame"
    window = [line for line in lines if " audio_window " in line][0]
    assert "processed=0" in window and re.search(r"bypassed=\d+", window)


def test_voice_segments_are_debounced_and_carry_levels() -> None:
    _, enhancer, clock, lines = make()
    run(enhancer, clock, 5, amplitude=10)  # silence, VAD false
    enhancer.vad = True
    run(enhancer, clock, 1, amplitude=3000)  # 50 ms: below the 100 ms on-threshold
    assert not any(" voice_segment_start " in line for line in lines)
    run(enhancer, clock, 9, amplitude=3000)  # well past 100 ms
    assert sum(" voice_segment_start " in line for line in lines) == 1
    enhancer.vad = False
    run(enhancer, clock, 4, amplitude=10)  # 200 ms of silence: still the same segment
    assert not any(" voice_segment_end " in line for line in lines)
    run(enhancer, clock, 10, amplitude=10)  # past 500 ms
    end = [line for line in lines if " voice_segment_end " in line]
    assert len(end) == 1 and "duration_ms=" in end[0] and "in_dbfs_peak=" in end[0] and "out_dbfs_peak=" in end[0]
    assert "source=ai-coustics-vad" in end[0]


def test_heartbeat_covers_the_silent_case_and_is_infrequent() -> None:
    _, enhancer, clock, lines = make()
    run(enhancer, clock, int(35 * 1000 / FRAME_MS))  # 35 s of "silence"
    windows = [line for line in lines if " audio_window " in line]
    assert 2 <= len(windows) <= 4, "one line per ~10 s, never per frame"
    assert all("vad_true_frames=0" in w and "processed=" in w for w in windows)
    assert len(lines) < 12
    assert HEARTBEAT_SECONDS == 10.0


def test_an_enhancer_without_a_voice_flag_never_gets_one_invented() -> None:
    """Krisp provides no voice-activity flag: no voice_segment lines, the heartbeat says so, levels still logged."""
    _, enhancer, clock, lines = make()
    enhancer.vad = None
    run(enhancer, clock, int(25 * 1000 / FRAME_MS), amplitude=3000)  # loud for 25 s
    assert not any(" voice_segment_start " in line or " voice_segment_end " in line for line in lines)
    windows = [line for line in lines if " audio_window " in line]
    assert windows and all("vad_flag=not_provided" in w and "vad_true_frames" not in w for w in windows)
    assert all("processed=" in w and "in_dbfs_peak=" in w and "out_dbfs_peak=" in w for w in windows)
    assert "passed_through=True" in next(line for line in lines if " first_frame_processed " in line)


def test_audio_is_never_altered_and_no_audio_or_text_is_logged() -> None:
    _, enhancer, clock, lines = make()
    f = frame(1234)
    original_bytes = bytes(f.data)
    out = enhancer._process(f)
    assert bytes(f.data) == original_bytes, "the input frame is untouched"
    assert out is not f and bytes(out.data) == original_bytes, "the caller receives exactly what the processor returned"
    joined = "\n".join(lines)
    assert not re.search(r"token|secret|api_key|bearer|jwt", joined, re.IGNORECASE)
    assert not re.search(r"transcript=|text=|audio=|data=", joined)


def test_a_failing_observer_can_never_break_the_audio() -> None:
    diag, enhancer, clock, lines = make()
    diag._emit = lambda _line: (_ for _ in ()).throw(RuntimeError("log sink exploded"))  # noqa: SLF001
    out = enhancer._process(frame(10))
    assert isinstance(out, rtc.AudioFrame)


def test_session_and_openai_event_hooks_log_timestamps_only() -> None:
    clock, lines = Clock(), []
    diag = AudioDiagnostics(enabled=True, room="r", session_id="s", clock=clock, emit=lines.append)

    class Emitter:
        def __init__(self) -> None:
            self.handlers: dict = {}

        def on(self, name, handler):
            self.handlers[name] = handler

    session, rt = Emitter(), Emitter()
    session._activity = type("A", (), {"realtime_llm_session": rt})()
    diag.hook_events(session)
    diag.hook_realtime(session)
    session.handlers["user_state_changed"](type("E", (), {"old_state": "listening", "new_state": "speaking"})())
    session.handlers["agent_state_changed"](type("E", (), {"old_state": "speaking", "new_state": "listening"})())
    session.handlers["user_input_transcribed"](type("E", (), {"is_final": True, "transcript": "SECRET WORDS"})())
    rt.handlers["input_speech_started"](object())
    rt.handlers["input_speech_stopped"](object())
    joined = "\n".join(lines)
    for event in ("user_state", "agent_state", "user_input_transcribed", "openai_input_speech_started", "openai_input_speech_stopped", "realtime_hooked"):
        assert f" {event} " in joined or joined.count(event), event
    assert "SECRET WORDS" not in joined and "chars=12" in joined and "is_final=True" in joined


def test_a_missing_realtime_session_is_reported_not_raised() -> None:
    lines: list[str] = []
    diag = AudioDiagnostics(enabled=True, room="r", session_id="s", emit=lines.append)
    diag.hook_realtime(object())  # no _activity at all
    assert any("hook_failed" in line for line in lines)


def test_agent_wiring_is_marked_temporary_and_the_real_enhancer_keeps_its_model() -> None:
    source = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")
    assert source.count("DIAG (temporary") >= 4
    assert "diagnostics_enabled_for(AGENT_NAME)" in source
    options = mariam._avatar_room_options()  # no diagnostics: identical to the pre-diagnostics behaviour
    assert isinstance(options.audio_input.noise_cancellation, krisp.KrispVivaFilterFrameProcessor)
    assert options.audio_input.noise_cancellation.noise_suppression_level == 90
    lines: list[str] = []
    diag = AudioDiagnostics(enabled=True, room="r", session_id="s", emit=lines.append)
    wrapped = mariam._avatar_room_options(diag).audio_input.noise_cancellation
    assert type(wrapped).__name__ == "KrispVivaFilterFrameProcessor", "same processor class - only observed"
    assert wrapped.noise_suppression_level == 90
    assert any("model=KRISP_VIVA_VOICE_ISOLATION_90" in line for line in lines)
