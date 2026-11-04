"""Phase 2 isolation audit: stale session / token / room reuse, OLD session -> NEW session and OLD user -> NEW user.

Only scenarios NOT already proven in `test_session_isolation.py` / `test_voice_practice.py`:
token claims and scope, "old session left open (crash / closed tab), then a new student or a new session starts",
all four Avatar<->Voice pairings with late events, a complete ownership matrix across EVERY mutation route, absence
of hidden global state in the service, and a randomized multi-student stress run.

No real LiveKit / Gemini. Tokens are decoded ONLY here, for claims - never printed.
"""

import itertools
import random
from datetime import timedelta

import pytest

from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import InteractionTurn, LiveSession, PracticeMode, SessionState
from app.models.topic_content import Level, TopicContent, VoicePromptMode
from app.models.user import Role
from tests.api.test_sessions import (
    DEFAULT_ADMIN_ID,
    FAKE_INTERNAL_API_KEY,
    SUFFICIENT_EVIDENCE_TRANSCRIPT,
    FakeSessionScorer,
    _login_with_user,
    _override_session_scorer,
)
from tests.api.test_session_isolation import (  # noqa: F401  (fixtures + helpers re-used)
    INTERNAL,
    LATE_BROWSER_TRANSCRIPT,
    LATE_TRANSCRIPT,
    _avatar,
    _avatar_connected,
    _avatar_ended,
    _clear_overrides,
    _fake_livekit_credentials,
    _row,
    _state,
    _voice,
    _voice_connected,
    livekit,
)
from tests.api.test_voice_practice import SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER, _auth, _claims, _dispatch, _end, _lesson, _start

MODES = ("avatar", "voice")
START = {"avatar": _avatar, "voice": _voice}


def _connect(client, livekit, token, started, mode):
    return _avatar_connected(client, started["session_id"]) if mode == "avatar" else _voice_connected(client, livekit, token, started)


def _late_events(client, token, started, mode):
    """Everything a stale session can still send after it was superseded - each in that mode's own dialect."""
    sid = started["session_id"]
    if mode == "avatar":  # Mariam's worker: connected, ended (+ transcript -> report), duplicate ended, connection failure
        _avatar_connected(client, sid)
        _avatar_ended(client, sid, transcript=LATE_TRANSCRIPT)
        _avatar_ended(client, sid, transcript=LATE_TRANSCRIPT, reason="connection_failure")
    else:  # the student's browser: late /connected and /end (other reason + transcript)
        client.post(f"/api/v1/sessions/{sid}/connected", headers=_auth(token))
        _end(client, token, sid, reason="connection_failure", transcript=LATE_BROWSER_TRANSCRIPT)
        _end(client, token, sid, transcript=LATE_BROWSER_TRANSCRIPT)


# --- tokens: scope, uniqueness, lifetime ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_token_authorizes_exactly_one_room_one_identity_and_nothing_administrative(client, livekit, mode):
    ta, sa = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, sb = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a, b = START[mode](client, ta, lesson.id), START[mode](client, tb, lesson.id)
    for started, student in ((a, sa), (b, sb)):
        claims = _claims(started)
        grant = claims["video"]
        assert claims["sub"] == f"student-{student.id}"
        assert grant["room"] == started["room_name"] and grant["roomJoin"] is True
        # no administrative power: cannot create/list/admin/record/ingress, cannot be hidden
        for forbidden in ("roomCreate", "roomList", "roomAdmin", "roomRecord", "ingressAdmin", "hidden", "recorder", "canUpdateOwnMetadata"):
            assert not grant.get(forbidden), f"token must not carry {forbidden}"
        assert len(_dispatch(started)) == 2
    ca, cb = _claims(a), _claims(b)
    assert a["participant_token"] != b["participant_token"]
    assert ca["video"]["room"] != cb["video"]["room"], "token A can never authorize B's room"
    assert ca["sub"] != cb["sub"], "token A can never act as B's participant"
    # the token lifetime is bounded (documented: LiveKit SDK default 6 h - see the audit report)
    assert 0 < ca["exp"] - ca["nbf"] <= 6 * 3600


@pytest.mark.parametrize("mode", MODES)
async def test_a_then_b_each_get_a_brand_new_token_even_after_a_ended(client, livekit, mode):
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a = START[mode](client, ta, lesson.id)
    _connect(client, livekit, ta, a, mode)
    (_end(client, ta, a["session_id"], transcript=LATE_BROWSER_TRANSCRIPT) if mode == "voice" else _avatar_ended(client, a["session_id"]))
    b = START[mode](client, tb, lesson.id)
    assert b["participant_token"] != a["participant_token"] and b["session_id"] != a["session_id"] and b["room_name"] != a["room_name"]
    assert _claims(b)["video"]["room"] == b["room_name"] != _claims(a)["video"]["room"]
    again = START[mode](client, ta, lesson.id)  # A returns: also brand new, never A's first token
    assert again["participant_token"] not in (a["participant_token"], b["participant_token"])


# --- OLD session left open (crash / closed tab) -> NEW student, all four pairings ---------------------------------------------------


@pytest.mark.parametrize("a_mode,b_mode", list(itertools.product(MODES, MODES)))
async def test_student_a_crashes_then_student_b_starts_and_inherits_nothing(client, livekit, a_mode, b_mode):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    ta, sa = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, sb = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    l_a = TopicContent(title="Lesson of A", level=Level.INTERMEDIATE, avatar_prompt="PROMPT-A-AVATAR", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="PROMPT-A-VOICE", opening_line="OPENING-A")
    l_b = TopicContent(title="Lesson of B", level=Level.INTERMEDIATE, avatar_prompt="PROMPT-B-AVATAR", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="PROMPT-B-VOICE", opening_line="OPENING-B")
    await l_a.insert(); await l_b.insert()
    await CalendarTemplate(name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE, is_active=True,
                           days=[CalendarDayEntry(day=1, topic_ids=[l_a.id, l_b.id])]).insert()

    a = START[a_mode](client, ta, l_a.id)
    _connect(client, livekit, ta, a, a_mode)  # A is live ...
    # ... and then A's browser crashes / the tab closes / the network drops: NOTHING is sent. The record stays open.
    assert await _state(a) == SessionState.CONNECTED
    deleted_before = list(livekit.deleted_rooms)

    b = START[b_mode](client, tb, l_b.id)
    ca, cb = _claims(a), _claims(b)
    assert b["session_id"] != a["session_id"] and b["room_name"] != a["room_name"]
    assert b["participant_token"] != a["participant_token"]
    assert cb["sub"] == f"student-{sb.id}" != ca["sub"]
    assert cb["video"]["room"] == b["room_name"]
    agent_b, meta_b = _dispatch(b)
    assert agent_b == ("tamakkan-mariam-dev" if b_mode == "avatar" else "tamakkan-voice-dev")
    assert meta_b["session_id"] == b["session_id"] and meta_b["topic_id"] == l_b.id
    blob = str(meta_b)
    assert "PROMPT-A" not in blob and "OPENING-A" not in blob and "Lesson of A" not in blob, "B must never receive A's lesson/prompt"
    assert meta_b["opening_line"] == "OPENING-B"
    row_b = await _row(b)
    assert row_b.student_id == sb.id and row_b.topic_id == l_b.id and row_b.state == SessionState.CONNECTING
    assert row_b.transcript_history == [] and row_b.browser_transcript == [] and row_b.report is None
    # B starting never touched A's open session or its room
    assert await _state(a) == SessionState.CONNECTED
    assert livekit.deleted_rooms == deleted_before and a["room_name"] not in livekit.deleted_rooms

    # A's zombie events (the crashed client's last messages arriving late) cannot mutate B
    _late_events(client, ta, a, a_mode)
    assert (await _row(b)).state == SessionState.CONNECTING and (await _row(b)).end_reason is None
    assert (await _row(b)).transcript_history == [] and (await _row(b)).browser_transcript == [] and (await _row(b)).report is None


# --- OLD session -> NEW session of the SAME student, all four pairings -----------------------------------------------------------------


@pytest.mark.parametrize("first,second", list(itertools.product(MODES, MODES)))
async def test_same_student_new_session_is_brand_new_and_late_events_from_the_old_one_cannot_reach_it(client, livekit, first, second):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, student = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    s1 = START[first](client, token, lesson.id)
    _connect(client, livekit, token, s1, first)
    # crash/refresh: no end is sent for session 1. The student starts session 2 (possibly in the other mode).
    s2 = START[second](client, token, lesson.id)
    assert s2["session_id"] != s1["session_id"] and s2["room_name"] != s1["room_name"] and s2["participant_token"] != s1["participant_token"]
    assert _claims(s2)["video"]["room"] == s2["room_name"]
    r2 = await _row(s2)
    assert r2.mode == (PracticeMode.TOPIC_DISCUSSION if second == "avatar" else PracticeMode.VOICE_PRACTICE)
    assert _dispatch(s2)[1]["session_id"] == s2["session_id"]

    old = await _row(s1)
    if first == "voice" and second == "voice":
        # Intended product rule: a newer Voice session closes THIS student's older open Voice session - by explicit ownership.
        assert old.state.value == "ended" and s1["room_name"] in livekit.deleted_rooms
        assert s2["room_name"] not in livekit.deleted_rooms
    else:
        assert old.state == SessionState.CONNECTED, "no other pairing may close the old session implicitly"
        assert livekit.deleted_rooms == []

    _connect(client, livekit, token, s2, second)
    snapshot = (await _row(s2)).state
    _late_events(client, token, s1, first)
    r2_after = await _row(s2)
    assert r2_after.state == snapshot == SessionState.CONNECTED and r2_after.end_reason is None and r2_after.ended_at is None
    assert r2_after.transcript_history == [] and r2_after.browser_transcript == [] and r2_after.report is None
    assert s2["room_name"] not in livekit.deleted_rooms
    # session 1 was not silently "reused" as session 2: still its own record, own room, own mode
    assert (await _row(s1)).livekit_room_name == s1["room_name"] and (await _row(s1)).id != r2_after.id


# --- old agent / old room cannot attach to a new session -----------------------------------------------------------------------------


async def test_dispatch_and_connected_verification_are_bound_to_the_exact_new_room(client, livekit):
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a = _voice(client, ta, lesson.id)
    livekit.agent_joins(a["room_name"])  # A's hosted voice agent is (still) running in A's room
    b = _voice(client, tb, lesson.id)
    # An agent that is still bound to A's previous room must NOT make B's session look connected.
    assert client.post(f"/api/v1/sessions/{b['session_id']}/connected", headers=_auth(tb)).status_code == 409
    assert await _state(b) == SessionState.CONNECTING
    livekit.agent_joins(b["room_name"])
    assert client.post(f"/api/v1/sessions/{b['session_id']}/connected", headers=_auth(tb)).json()["state"] == "connected"
    # B's dispatch names exactly B's session/topic, never A's
    (_, meta_a), (_, meta_b) = _dispatch(a), _dispatch(b)
    assert meta_a["session_id"] == a["session_id"] and meta_b["session_id"] == b["session_id"] != meta_a["session_id"]


async def test_a_reconnect_after_b_started_can_only_ever_resolve_a_own_session(client, livekit):
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a = _voice(client, ta, lesson.id)
    _voice_connected(client, livekit, ta, a)
    livekit.participants.pop(a["room_name"], None)  # A drops
    b = _voice(client, tb, lesson.id)  # B starts while A is away
    livekit.agent_joins(a["room_name"])  # A's agent still there; A's client reconnects and re-confirms by explicit id
    again = client.post(f"/api/v1/sessions/{a['session_id']}/connected", headers=_auth(ta))
    assert again.status_code == 200 and again.json()["id"] == a["session_id"] and again.json()["state"] == "connected"
    assert await _state(b) == SessionState.CONNECTING, "A's reconnect must never resolve or touch B"
    # and A cannot be made to "reconnect" into B: B's id from A is a 403
    assert client.post(f"/api/v1/sessions/{b['session_id']}/connected", headers=_auth(ta)).status_code == 403


# --- ownership matrix: EVERY mutation route x every wrong actor, both modes --------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_ownership_matrix_no_wrong_actor_can_mutate_any_session(client, livekit, mode):
    from app.models.live_session import SessionState as S

    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    t_admin2, _ = await _login_with_user(client, Role.TEACHER, email="other-admin@example.com")
    lesson = await _lesson()
    a = START[mode](client, ta, lesson.id)
    _connect(client, livekit, ta, a, mode)
    sid = a["session_id"]
    before = await _row(a)
    deleted = list(livekit.deleted_rooms)

    wrong_actors = {"student B": _auth(tb), "unrelated admin": _auth(t_admin2), "anonymous": {}}
    routes = [
        ("POST", f"/api/v1/sessions/{sid}/connected", None),
        ("POST", f"/api/v1/sessions/{sid}/end", {"end_reason": "user_ended", "browser_transcript": []}),
        ("POST", f"/api/v1/sessions/{sid}/force-end", None),
        ("PATCH", f"/api/v1/sessions/{sid}/report-reviewed", {"reviewed": True}),
        ("GET", f"/api/v1/sessions/{sid}", None),
    ]
    for actor, headers in wrong_actors.items():
        for method, url, body in routes:
            resp = client.request(method, url, json=body, headers=headers)
            assert resp.status_code in (401, 403, 404, 422), f"{actor} {method} {url.split('/')[-1]} -> {resp.status_code}"
    # worker routes: a student JWT, no key, a wrong key - all refused; the right key works only on the right mode
    for headers in (_auth(ta), {}, {"X-Internal-API-Key": "definitely-wrong"}):
        assert client.post(f"/api/v1/sessions/{sid}/mark-connected", headers=headers).status_code in (401, 403)
        assert client.post(f"/api/v1/sessions/{sid}/mark-ended", json={"end_reason": "user_ended", "transcript_history": LATE_TRANSCRIPT}, headers=headers).status_code in (401, 403)

    after = await _row(a)
    assert (after.state, after.end_reason, after.ended_at) == (before.state, before.end_reason, before.ended_at) == (S.CONNECTED, None, None)
    assert after.transcript_history == before.transcript_history == [] and after.browser_transcript == [] and after.report is None
    assert livekit.deleted_rooms == deleted and scorer.calls == []


# --- no hidden global state in the service ------------------------------------------------------------------------------------------------


def test_the_session_service_holds_no_global_session_state_or_caches():
    import inspect

    import app.services.live_session_service as module

    source = inspect.getsource(module)
    assert "lru_cache" not in source and "\nglobal " not in source and "    global " not in source
    mutable_globals = {
        name: type(value).__name__
        for name, value in vars(module).items()
        if not name.startswith("_") and isinstance(value, (dict, list, set))
    }
    # the only module-level containers are constants (room prefixes, allowed reasons/states) - never per-session data
    assert set(mutable_globals) <= {"ROOM_PREFIX", "STUDENT_REPORTABLE_END_REASONS", "OPEN_STATES", "SESSION_TRANSITIONS"}, mutable_globals
    assert module.get_live_session_service() is not module.get_live_session_service(), "a fresh service per request"
    service = module.get_live_session_service()
    assert not [a for a in vars(service) if isinstance(getattr(service, a), (dict, list, set)) and getattr(service, a)], "no per-session cache attributes"


# --- randomized stress: many students, many modes, crashes and restarts -------------------------------------------------------------------


async def test_stress_many_students_random_starts_ends_and_crashes_keep_everything_unique_and_owned(client, livekit):
    rng = random.Random(20260930)
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    lessons = [TopicContent(title=f"L{i}", level=Level.INTERMEDIATE, avatar_prompt=f"AVATAR-{i}", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt=f"VOICE-{i}", opening_line=f"OPEN-{i}") for i in range(4)]
    for lesson in lessons:
        await lesson.insert()
    await CalendarTemplate(name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE, is_active=True,
                           days=[CalendarDayEntry(day=1, topic_ids=[lesson.id for lesson in lessons])]).insert()
    students = [await _login_with_user(client, Role.STUDENT, email=f"stress{i}@example.com") for i in range(40)]

    started = []  # (token, student, lesson, mode, response)
    for _ in range(160):
        token, student = rng.choice(students)
        lesson, mode = rng.choice(lessons), rng.choice(MODES)
        response = START[mode](client, token, lesson.id)
        started.append((token, student, lesson, mode, response))
        roll = rng.random()
        if roll < 0.35:  # connect + clean end
            _connect(client, livekit, token, response, mode)
            (_end(client, token, response["session_id"], transcript=[{"speaker": "student", "text": f"MARK-{student.id}", "timestamp": "2026-01-01T00:00:00"}]) if mode == "voice"
             else _avatar_ended(client, response["session_id"], transcript=[{"speaker": "mariam", "text": "hi", "timestamp": "2026-01-01T00:00:00"}, {"speaker": "student", "text": f"MARK-{student.id}", "timestamp": "2026-01-01T00:00:05"}]))
        elif roll < 0.6:  # connect, then crash: nothing further is sent
            _connect(client, livekit, token, response, mode)
        elif roll < 0.75:  # late event for a random OLD session (any student's!), arriving now
            old_token, _, _, old_mode, old = rng.choice(started)
            _late_events(client, old_token, old, old_mode)

    ids = [r["session_id"] for *_, r in started]
    rooms = [r["room_name"] for *_, r in started]
    tokens = [r["participant_token"] for *_, r in started]
    assert len(set(ids)) == len(set(rooms)) == len(set(tokens)) == len(started) == 160
    for token, student, lesson, mode, r in started:
        claims = _claims(r)
        agent, meta = _dispatch(r)
        assert claims["sub"] == f"student-{student.id}" and claims["video"]["room"] == r["room_name"]
        assert r["room_name"] == f"{'mariam' if mode == 'avatar' else 'voice'}-{r['session_id']}"
        assert agent == ("tamakkan-mariam-dev" if mode == "avatar" else "tamakkan-voice-dev")
        assert (meta["session_id"], meta["topic_id"], meta["opening_line"]) == (r["session_id"], lesson.id, lesson.opening_line)
        row = await LiveSession.get(r["session_id"])
        assert row.student_id == student.id and row.topic_id == lesson.id
        assert row.mode == (PracticeMode.TOPIC_DISCUSSION if mode == "avatar" else PracticeMode.VOICE_PRACTICE)
        # transcripts only ever contain the OWNER's marker, and only in the mode-correct field
        texts = [t.text for t in row.transcript_history] + [t.text for t in row.browser_transcript]
        allowed = {"hi", f"MARK-{student.id}"} | {turn["text"] for turn in LATE_TRANSCRIPT + LATE_BROWSER_TRANSCRIPT}
        assert set(texts) <= allowed, "a session may only ever hold its OWN marker (or the shared late-event text)"
        assert not any(f"MARK-{other.id}" in " ".join(texts) for _, other in students if other.id != student.id), "another student's transcript leaked in"
        if row.mode == PracticeMode.VOICE_PRACTICE:
            assert row.transcript_history == [] and row.report is None
        else:
            assert row.browser_transcript == []
    # invariant: at most ONE open Voice session per student (newest wins), never affecting other students
    per_student = {}
    for token, student, lesson, mode, r in started:
        row = await LiveSession.get(r["session_id"])
        if mode == "voice" and row.state in (SessionState.CONNECTING, SessionState.CONNECTED):
            per_student.setdefault(student.id, []).append(r["session_id"])
    assert all(len(v) <= 1 for v in per_student.values()), {k: len(v) for k, v in per_student.items() if len(v) > 1}
    assert all(room.startswith("voice-") for room in livekit.deleted_rooms), "only Voice rooms were ever released by the student path"
