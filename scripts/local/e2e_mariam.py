"""ONE cost-capped end-to-end Mariam test against the hosted development agent.

A scripted STUDENT joins a real session, says one short sentence, listens for Mariam's audible reply,
then leaves immediately. Afterwards it checks that the platform recorded session completion and the
transcript (via the callbacks through the gateway/tunnel). It does NOT request a report or any Gemini call.

Needs `pip install livekit numpy` (kept out of the backend requirements on purpose) and the local stack
running in agent mode (`tamakkan start -Agent` + `tamakkan tunnel`). Prints no credentials.

    python e2e_mariam.py --student student-standard-day4 --wav short.wav [--max-convo-seconds 30]

Cost rules baked in: one student, one room, one agent job, one sentence, hard cap on active conversation,
no retries. If anything fails it stops; re-running is a separate, deliberate (paid) decision.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
from livekit import rtc

ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS = ROOT / ".tamakkan" / "local" / "accounts.json"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(ok)


def http(method: str, url: str, body=None, token: str | None = None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


class Probe:
    """Counts remote audio activity, video frames and data packets."""

    def __init__(self):
        self.data_packets = 0
        self.data_bytes = 0
        self.audio_ms = 0.0          # cumulative ms of remote audio above the silence threshold
        self.last_audio_at = 0.0
        self.video_frames = 0
        self.video_size = (0, 0)
        self.tasks: list[asyncio.Task] = []

    async def pump_audio(self, track):
        async for ev in rtc.AudioStream(track):
            f = ev.frame
            samples = np.frombuffer(f.data, dtype=np.int16).astype(np.float32)
            if samples.size and float(np.sqrt(np.mean(samples ** 2))) > 300:   # ~ -40 dBFS
                self.audio_ms += 1000.0 * f.samples_per_channel / f.sample_rate
                self.last_audio_at = time.monotonic()

    async def pump_video(self, track):
        async for ev in rtc.VideoStream(track):
            self.video_frames += 1
            self.video_size = (ev.frame.width, ev.frame.height)

    async def wait_audio(self, min_ms: float, timeout: float) -> bool:
        start = self.audio_ms
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.audio_ms - start >= min_ms:
                return True
            await asyncio.sleep(0.2)
        return False

    async def wait_quiet(self, quiet_s: float, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if time.monotonic() - self.last_audio_at >= quiet_s:
                return True
            await asyncio.sleep(0.2)
        return False


async def speak(source: rtc.AudioSource, path: str) -> float:
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "WAV must be mono 16-bit"
        rate, data = w.getframerate(), w.readframes(w.getnframes())
    step = rate // 100  # 10 ms
    for i in range(0, len(data) // 2, step):
        chunk = data[i * 2:(i + step) * 2]
        n = len(chunk) // 2
        if n:
            await source.capture_frame(rtc.AudioFrame(chunk, rate, 1, n))
    await source.wait_for_playout()
    return len(data) / 2 / rate


async def run(a) -> int:
    acct = json.loads(ACCOUNTS.read_text(encoding="utf-8"))[a.student]
    s, p = http("POST", a.api + "/auth/login", {"email": acct["email"], "password": acct["password"]})
    if not check("student login", s == 200):
        return 1
    tok = p["access_token"]
    s, topics = http("GET", a.api + "/topics", token=tok)
    open_topics = [t for t in topics if t.get("lesson_status") not in (None, "locked")]
    topic = open_topics[0] if open_topics else topics[0]
    s, start = http("POST", a.api + "/sessions/start", {"topic_id": topic["id"]}, tok)
    if not check("session start (token minted, agent dispatched)", s == 200, f"HTTP {s}" if s != 200 else f"topic '{topic['title']}'"):
        print(str(start)[:300])
        return 1
    sid = start["session_id"]
    print(f"  session {sid[:8]}..., room {start['room_name'][:15]}...", flush=True)

    probe = Probe()
    room = rtc.Room()
    seen: dict[str, dict] = {}

    def note(part):
        return seen.setdefault(part.identity, {"kind": str(part.kind).split(".")[-1], "attrs": sorted((part.attributes or {}).keys()), "tracks": set()})

    @room.on("participant_connected")
    def _pc(part):
        note(part)

    @room.on("data_received")
    def _data(pkt):
        probe.data_packets += 1
        probe.data_bytes += len(pkt.data)

    @room.on("track_subscribed")
    def _sub(track, pub, participant):
        note(participant)["tracks"].add(str(track.kind).split(".")[-1])
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            probe.tasks.append(asyncio.create_task(probe.pump_audio(track)))
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            probe.tasks.append(asyncio.create_task(probe.pump_video(track)))

    await room.connect(start["server_url"], start["participant_token"], options=rtc.RoomOptions(auto_subscribe=True))
    check("student joined the LiveKit room", room.connection_state == rtc.ConnectionState.CONN_CONNECTED)
    for part in room.remote_participants.values():
        note(part)
    source = rtc.AudioSource(24000, 1)
    mic = rtc.LocalAudioTrack.create_audio_track("student-mic", source)
    await room.local_participant.publish_track(mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    convo_started: float | None = None

    def convo_left() -> float:
        return a.max_convo_seconds - (time.monotonic() - convo_started) if convo_started else a.max_convo_seconds

    # --- startup (not counted as conversation): agent + avatar participant join ---
    t0 = time.monotonic()
    while not room.remote_participants and time.monotonic() - t0 < 60:
        await asyncio.sleep(0.5)
    check("hosted agent / avatar participant joined the room", bool(room.remote_participants), f"after {time.monotonic() - t0:.0f}s")
    t0 = time.monotonic()
    while probe.video_frames < 5 and probe.audio_ms < 500 and time.monotonic() - t0 < 25:
        await asyncio.sleep(0.5)

    # --- the conversation clock starts at Mariam's first audio (bounded wait if she never speaks) ---
    got_greeting = await probe.wait_audio(800, 20)
    convo_started = time.monotonic()
    check("Mariam spoke (audible greeting)", got_greeting)
    await probe.wait_quiet(1.2, min(10, max(1, convo_left() - 12)))

    if convo_left() > 12:
        secs = await speak(source, a.wav)
        replied = await probe.wait_audio(800, max(3, min(10, convo_left() - 1)))
        check(f"student said one short sentence ({secs:.1f}s) -> Mariam replied audibly", replied)
    else:
        check("student sentence skipped (conversation budget too small)", False, f"{convo_left():.0f}s left")

    print(f"  ending immediately ({time.monotonic() - convo_started:.0f}s of active conversation)", flush=True)
    await room.disconnect()
    for t in probe.tasks:
        t.cancel()

    print("  avatar / media evidence:")
    for ident, info in seen.items():
        print(f"      participant {ident[:26]:<26} kind={info['kind']:<20} tracks={sorted(info['tracks'])} attrs={info['attrs']}")
    print(f"      video frames={probe.video_frames} size={probe.video_size[0]}x{probe.video_size[1]}  remote audio={probe.audio_ms / 1000:.1f}s  data packets={probe.data_packets} ({probe.data_bytes} bytes)")
    check("avatar rendered (video frames from the avatar participant)", probe.video_frames >= 5,
          f"{probe.video_frames} frames {probe.video_size[0]}x{probe.video_size[1]}; data packets={probe.data_packets}")

    # --- what the platform recorded (bounded polling; no report wait, no Gemini) ---
    tl = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["teacher1"]
    _, tp = http("POST", a.api + "/auth/login", {"email": tl["email"], "password": tl["password"]})
    ttok = tp["access_token"]
    sess = None
    for _ in range(int(a.wait_end / 3)):
        _, sess = http("GET", a.api + f"/sessions/{sid}", token=ttok)
        if isinstance(sess, dict) and sess.get("state") == "ended":
            break
        time.sleep(3)
    state = sess.get("state") if isinstance(sess, dict) else None
    check("session completed (state = ended via callback)", state == "ended",
          f"state={state} end_reason={sess.get('end_reason') if isinstance(sess, dict) else None}")
    turns = (sess or {}).get("transcript_history") or []
    check("transcript persisted", len(turns) >= 1, f"{len(turns)} turns, speakers={sorted({str(t.get('speaker')) for t in turns})}")
    for t in turns[:8]:
        print(f"      {str(t.get('speaker')):>8}: {str(t.get('text'))[:110]}")
    rep = (sess or {}).get("report")
    print(f"  report present: {'yes (' + str((rep.get('analysis') or {}).get('status')) + ')' if rep else 'no'}  (this test requests no Gemini scoring)")

    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n==== {ok}/{len(RESULTS)} checks passed ====")
    return 0 if ok == len(RESULTS) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:4673/api/v1")
    ap.add_argument("--student", default="student-standard-day4")
    ap.add_argument("--wav", required=True, help="ONE short spoken sentence (mono 16-bit WAV)")
    ap.add_argument("--max-convo-seconds", type=float, default=30.0, help="hard cap on active conversation")
    ap.add_argument("--wait-end", type=int, default=60)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
