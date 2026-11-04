"""TEMPORARY, DEV-ONLY diagnostics for the Avatar audio path. Remove after the noise-isolation investigation:
delete this file, `tests/test_audio_diagnostics.py`, and the call sites in `agent.py` marked `DIAG (temporary)`.

What it logs (INFO, grep for `AUDIO_DIAG`), timestamps and counters only - NEVER audio, transcript text, tokens or
secrets:

  session_start            room / session id, the enhancer model, frame size
  enhancer_instantiated    the QUAIL_VF_S enhancer object was created for this session
  first_frame_processed    the first frame really went through the enhancer (`passed_through=True`), or came back
                           untouched (`passed_through=False` = a silent bypass)
  enhancer_bypass          frames came back untouched (rate-limited), with a running count
  voice_segment_start/end  the enhancer's OWN voice-activity flag, when it provides one (ai-coustics did; Krisp does
                           not, so no such lines appear and nothing is invented), debounced into segments
                           (>=100 ms on, >=500 ms off) with duration and peak input/output level (dBFS)
  audio_window             a heartbeat every 10 s: frames seen / processed / bypassed, frames flagged as voice
                           (`vad_true_frames`, or `vad_flag=not_provided`), peak input vs output level (dBFS) -
                           proves the silence case too
  user_state               AgentSession user state changes (speaking/listening) - derived from OpenAI's speech events
  openai_input_speech_started / openai_input_speech_stopped
                           the OpenAI Realtime server-VAD events, as received by the session
  agent_state              Mariam's state changes (shows whether she was interrupted)
  user_input_transcribed   that OpenAI produced a transcript (final or not) and its LENGTH - never the text

Enabled only for a worker whose dispatch name ends in `-dev`, so the production agent can never emit it, and no
secret/env change is needed to turn it on. Diagnostics must never affect the call: every hook swallows its own errors."""

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("mariam-agent.diag")

# `rtc.AudioFrame.userdata` key an enhancer may write a voice-activity flag to (the ai-coustics plugin's
# `FRAME_USERDATA_AIC_VAD_ATTRIBUTE`). Other enhancers (Krisp) write nothing: the flag is then simply not provided.
AIC_VAD_KEY = "lk.aic-vad"
HEARTBEAT_SECONDS = 10.0
BYPASS_LOG_SECONDS = 10.0
VAD_ON_MS = 100.0
VAD_OFF_MS = 500.0
_SILENCE_DBFS = -120.0


def diagnostics_enabled_for(agent_name: str) -> bool:
    """Dev-only by construction: the production dispatch name never ends in `-dev`."""
    return agent_name.endswith("-dev")


def _dbfs(frame: Any) -> float:
    """Peak-of-frame RMS level in dBFS. A single number per frame - no audio is kept or logged."""
    import numpy as np  # local import: numpy is only needed while diagnostics are on

    samples = np.frombuffer(frame.data, dtype=np.int16)
    if samples.size == 0:
        return _SILENCE_DBFS
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) / 32768.0
    return _SILENCE_DBFS if rms <= 0 else max(_SILENCE_DBFS, 20.0 * math.log10(rms))


class AudioDiagnostics:
    def __init__(
        self,
        *,
        enabled: bool,
        room: str,
        session_id: str | None,
        clock: Callable[[], float] = time.time,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self._room = room
        self._session_id = session_id or "unknown"
        self._clock = clock
        self._emit = emit or logger.info
        # first-frame / window / segment state
        self._first_seen = False
        self._window_start: float | None = None
        self._frames = self._processed = self._bypassed = self._vad_true = 0
        self._vad_seen = False  # did ANY frame in this window carry a voice-activity flag?
        self._in_peak = self._out_peak = _SILENCE_DBFS
        self._last_bypass_log = 0.0
        self._bypass_since_log = 0
        self._in_segment = False
        self._on_ms = self._off_ms = 0.0
        self._seg_start = 0.0
        self._seg_in_peak = self._seg_out_peak = _SILENCE_DBFS

    # ---- logging ---------------------------------------------------------------------------------------------
    def log(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="milliseconds")
        parts = " ".join(f"{key}={value}" for key, value in fields.items())
        try:
            self._emit(f"AUDIO_DIAG {event} t={stamp} room={self._room} session={self._session_id} {parts}".rstrip())
        except Exception:  # noqa: BLE001 - diagnostics must never break the call
            pass

    def session_start(self, *, model: str, agent_name: str) -> None:
        self.log("session_start", agent=agent_name, enhancer=model, note="diagnostics are temporary and dev-only")

    # ---- enhancer --------------------------------------------------------------------------------------------
    def attach_enhancer(self, processor: Any, *, model: str) -> Any:
        """Wraps the plugin's per-frame `_process` (the method LiveKit's AudioStream calls) so every frame can be
        observed. The processor, its model and its configuration are untouched; the returned frame is always exactly
        what the plugin returned."""
        if not self.enabled:
            return processor
        self.log("enhancer_instantiated", model=model, processor=type(processor).__name__, enabled=getattr(processor, "enabled", None))
        original = processor._process  # noqa: SLF001

        def traced(frame: Any) -> Any:
            out = original(frame)
            try:
                self._observe(processor, frame, out)
            except Exception:  # noqa: BLE001
                pass
            return out

        processor._process = traced  # noqa: SLF001
        return processor

    def _observe(self, processor: Any, frame: Any, out: Any) -> None:
        now = self._clock()
        passed = out is not frame  # the plugin returns the ORIGINAL frame object whenever it bypasses
        frame_ms = frame.samples_per_channel * 1000.0 / frame.sample_rate
        if not self._first_seen:
            self._first_seen = True
            self.log(
                "first_frame_processed",
                passed_through=passed,
                enhancer_enabled=getattr(processor, "enabled", None),
                sample_rate=frame.sample_rate,
                frame_ms=round(frame_ms, 1),
                channels=frame.num_channels,
            )
        if self._window_start is None:
            self._window_start = now
        in_db = _dbfs(frame)
        out_db = _dbfs(out) if passed else in_db
        self._frames += 1
        self._in_peak = max(self._in_peak, in_db)
        self._out_peak = max(self._out_peak, out_db)
        vad = None
        if passed:
            self._processed += 1
            flag = getattr(out, "userdata", {}).get(AIC_VAD_KEY)
            vad = None if flag is None else bool(flag)  # no flag from the enhancer -> not tracked, never invented
            if vad is not None:
                self._vad_seen = True
                self._vad_true += 1 if vad else 0
        else:
            self._bypassed += 1
            self._bypass_since_log += 1
            if now - self._last_bypass_log >= BYPASS_LOG_SECONDS:
                self._last_bypass_log = now
                self.log("enhancer_bypass", frames_since_last=self._bypass_since_log, enhancer_enabled=getattr(processor, "enabled", None))
                self._bypass_since_log = 0
        if vad is not None:
            self._track_segment(now, frame_ms, vad, in_db, out_db)
        if now - self._window_start >= HEARTBEAT_SECONDS:
            self.log(
                "audio_window",
                seconds=round(now - self._window_start, 1),
                frames=self._frames,
                processed=self._processed,
                bypassed=self._bypassed,
                **({"vad_true_frames": self._vad_true} if self._vad_seen else {"vad_flag": "not_provided"}),
                in_dbfs_peak=round(self._in_peak, 1),
                out_dbfs_peak=round(self._out_peak, 1),
            )
            self._window_start = now
            self._frames = self._processed = self._bypassed = self._vad_true = 0
            self._vad_seen = False
            self._in_peak = self._out_peak = _SILENCE_DBFS

    def _track_segment(self, now: float, frame_ms: float, vad: bool, in_db: float, out_db: float) -> None:
        if vad:
            self._on_ms += frame_ms
            self._off_ms = 0.0
            if self._in_segment:
                self._seg_in_peak = max(self._seg_in_peak, in_db)
                self._seg_out_peak = max(self._seg_out_peak, out_db)
            elif self._on_ms >= VAD_ON_MS:
                self._in_segment = True
                self._seg_start = now
                self._seg_in_peak, self._seg_out_peak = in_db, out_db
                self.log("voice_segment_start", source="ai-coustics-vad")
        else:
            self._on_ms = 0.0
            if self._in_segment:
                self._off_ms += frame_ms
                if self._off_ms >= VAD_OFF_MS:
                    self._in_segment = False
                    self.log(
                        "voice_segment_end",
                        source="ai-coustics-vad",
                        duration_ms=round((now - self._seg_start) * 1000.0 - self._off_ms),
                        in_dbfs_peak=round(self._seg_in_peak, 1),
                        out_dbfs_peak=round(self._seg_out_peak, 1),
                    )

    # ---- session / OpenAI events -----------------------------------------------------------------------------
    def hook_events(self, agent_session: Any) -> None:
        """AgentSession-level events (public API). `user_state` mirrors OpenAI's speech-start/stop while the session
        uses its default VAD setting."""
        if not self.enabled:
            return

        def user_state(ev: Any) -> None:
            self.log("user_state", old=ev.old_state, new=ev.new_state)

        def agent_state(ev: Any) -> None:
            self.log("agent_state", old=ev.old_state, new=ev.new_state)

        def transcribed(ev: Any) -> None:
            self.log("user_input_transcribed", is_final=ev.is_final, chars=len(ev.transcript or ""))

        for name, handler in (("user_state_changed", user_state), ("agent_state_changed", agent_state), ("user_input_transcribed", transcribed)):
            try:
                agent_session.on(name, handler)
            except Exception as exc:  # noqa: BLE001
                self.log("hook_failed", hook=name, error=type(exc).__name__)

    def hook_realtime(self, agent_session: Any) -> None:
        """The raw OpenAI Realtime server-VAD events, from the session's realtime session (call after `start()`).
        Timestamp = when the agent received the event."""
        if not self.enabled:
            return
        try:
            rt = agent_session._activity.realtime_llm_session  # noqa: SLF001 - no public accessor
            if rt is None:
                self.log("hook_failed", hook="realtime_session", error="not_available")
                return
            rt.on("input_speech_started", lambda _ev: self.log("openai_input_speech_started"))
            rt.on("input_speech_stopped", lambda _ev: self.log("openai_input_speech_stopped"))
            self.log("realtime_hooked", session=type(rt).__name__)
        except Exception as exc:  # noqa: BLE001
            self.log("hook_failed", hook="realtime_session", error=type(exc).__name__)
