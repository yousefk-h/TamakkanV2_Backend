"""Session isolation audit: Avatar Practice vs Voice Practice, student vs student, tab vs tab.

Complements `test_voice_practice.py` with the exact scenarios of the isolation audit:
same-student concurrent Avatar + Voice, late callbacks in BOTH directions, cross-student and cross-mode
negatives, room/token/metadata uniqueness over many starts, transcript/report/progress isolation, two tabs
and reconnects. No real LiveKit or Gemini (tokens are local JWTs, the LiveKit server API is faked, the scorer
is a fake). Only safe identifiers are ever asserted or printed - never a token.
"""

from datetime import timedelta

import pytest

from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import EndReason, LiveSession, PracticeMode, SessionState
from app.models.topic_content import Level, TopicContent, VoicePromptMode
from app.models.user import Role
from app.utils.time import utcnow
from tests.api.test_sessions import (
    DEFAULT_ADMIN_ID,
    FAKE_INTERNAL_API_KEY,
    SUFFICIENT_EVIDENCE_TRANSCRIPT,
    FakeSessionScorer,
    _login_with_user,
    _override_session_scorer,
)

# Re-used fixtures (autouse: fake LiveKit credentials, fake LiveKit server API, override cleanup) and helpers.
from tests.api.test_voice_practice import (  # noqa: F401
    SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER,
    _auth,
    _claims,
    _clear_overrides,
    _dispatch,
    _end,
    _fake_livekit_credentials,
    _lesson,
    _start,
    livekit,
)

INTERNAL = {"X-Internal-API-Key": FAKE_INTERNAL_API_KEY}
LATE_TRANSCRIPT = [
    {"speaker": "mariam", "text": "LATE-CALLBACK-TEXT mariam", "timestamp": "2026-01-01T01:00:00"},
    {"speaker": "student", "text": "LATE-CALLBACK-TEXT student", "timestamp": "2026-01-01T01:00:05"},
]
LATE_BROWSER_TRANSCRIPT = [
    {"speaker": "agent", "text": "LATE-BROWSER-TEXT agent", "timestamp": "2026-01-01T01:00:00"},
    {"speaker": "student", "text": "LATE-BROWSER-TEXT student", "timestamp": "2026-01-01T01:00:05"},
]


# --- helpers ----------------------------------------------------------------------------------------------


def _avatar(client, token, topic_id):
    return _start(client, token, topic_id, mode="topic_discussion").json()


def _voice(client, token, topic_id):
    return _start(client, token, topic_id, mode="voice_practice").json()


def _avatar_connected(client, sid):
    return client.post(f"/api/v1/sessions/{sid}/mark-connected", headers=INTERNAL)


def _avatar_ended(client, sid, transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT, reason="user_ended"):
    return client.post(
        f"/api/v1/sessions/{sid}/mark-ended", json={"end_reason": reason, "transcript_history": transcript}, headers=INTERNAL
    )


def _voice_connected(client, livekit, token, started):
    livekit.agent_joins(started["room_name"])
    return client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))


async def _state(started):
    return (await LiveSession.get(started["session_id"])).state


async def _row(started):
    return await LiveSession.get(started["session_id"])


def _two_lessons_one_day(level=Level.INTERMEDIATE):
    return [TopicContent(title=f"Lesson {i}", level=level, avatar_prompt=f"AVATAR-PROMPT-{i}", voice_prompt_mode=VoicePromptMode.SEPARATE,
                         voice_prompt=f"VOICE-PROMPT-{i}") for i in range(3)]


# --- 1. same student: Avatar in one tab, Voice in another -----------------------------------------------------------


async def test_same_student_avatar_and_voice_are_completely_separate_and_run_concurrently(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, student = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson(avatar_prompt="AVATAR-ONLY", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="VOICE-ONLY")

    avatar = _avatar(client, token, lesson.id)  # tab A
    voice = _voice(client, token, lesson.id)  # tab B

    # Different session, room, token, mode and agent path.
    assert avatar["session_id"] != voice["session_id"]
    assert avatar["room_name"] != voice["room_name"]
    assert avatar["participant_token"] != voice["participant_token"]
    assert avatar["room_name"].startswith("mariam-") and voice["room_name"].startswith("voice-")
    assert avatar["room_name"] == f"mariam-{avatar['session_id']}" and voice["room_name"] == f"voice-{voice['session_id']}"
    a_row, v_row = await _row(avatar), await _row(voice)
    assert (a_row.mode, v_row.mode) == (PracticeMode.TOPIC_DISCUSSION, PracticeMode.VOICE_PRACTICE)
    ca, cv = _claims(avatar), _claims(voice)
    assert ca["video"]["room"] == avatar["room_name"] and cv["video"]["room"] == voice["room_name"]
    assert ca["video"]["room"] != cv["video"]["room"], "a token must never grant the other mode's room"
    (a_agent, a_meta), (v_agent, v_meta) = _dispatch(avatar), _dispatch(voice)
    assert (a_agent, v_agent) == ("tamakkan-mariam-dev", "tamakkan-voice-dev")
    assert a_meta["session_id"] == avatar["session_id"] and v_meta["session_id"] == voice["session_id"]
    # Each agent only ever receives its own prompt: no prompt/metadata crossing.
    # Wrapped in an explicit "lesson reference material" boundary (see
    # app.core.prompt_safety) rather than sent as the raw string.
    assert "AVATAR-ONLY" in a_meta["topic_instructions"] and "lesson_prompt" not in a_meta
    assert "VOICE-ONLY" not in a_meta["topic_instructions"]
    assert "VOICE-ONLY" in v_meta["lesson_prompt"] and "topic_instructions" not in v_meta
    assert "AVATAR-ONLY" not in v_meta["lesson_prompt"]

    # Both connect; neither start disturbed the other.
    assert _avatar_connected(client, avatar["session_id"]).json()["state"] == "connected"
    assert _voice_connected(client, livekit, token, voice).json()["state"] == "connected"
    # A third tab starting another Avatar session closes nothing (Avatar sessions are independent).
    avatar_tab_c = _avatar(client, token, lesson.id)
    assert await _state(avatar) == SessionState.CONNECTED and await _state(voice) == SessionState.CONNECTED
    assert livekit.deleted_rooms == []

    # Ending Avatar (worker path) does not end or alter Voice.
    _avatar_ended(client, avatar["session_id"])
    assert (await _state(avatar)) == SessionState.ENDED
    assert await _state(voice) == SessionState.CONNECTED
    assert voice["room_name"] not in livekit.deleted_rooms
    voice_row = await _row(voice)
    assert voice_row.transcript_history == [] and voice_row.report is None and voice_row.browser_transcript == []
    assert len(scorer.calls) == 1 and (await _row(avatar)).report is not None

    # Ending Voice (student path) does not touch the other Avatar sessions or their transcripts.
    avatar_before = await _row(avatar)
    _end(client, token, voice["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)
    assert await _state(voice) == SessionState.ENDED
    assert await _state(avatar_tab_c) == SessionState.CONNECTING
    assert avatar_tab_c["room_name"] not in livekit.deleted_rooms and avatar["room_name"] not in livekit.deleted_rooms
    avatar_after = await _row(avatar)
    assert [t.text for t in avatar_after.transcript_history] == [t.text for t in avatar_before.transcript_history]
    assert avatar_after.browser_transcript == []
    assert len(scorer.calls) == 1, "ending a Voice session must never trigger Avatar scoring"
    voice_after = await _row(voice)
    assert voice_after.transcript_history == [] and voice_after.report is None and len(voice_after.browser_transcript) == len(SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)


async def test_same_student_ending_voice_first_leaves_avatar_intact(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    avatar, voice = _avatar(client, token, lesson.id), _voice(client, token, lesson.id)
    _avatar_connected(client, avatar["session_id"])
    _voice_connected(client, livekit, token, voice)

    BROWSER = SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER
    _end(client, token, voice["session_id"], transcript=BROWSER)
    assert await _state(voice) == SessionState.ENDED
    assert await _state(avatar) == SessionState.CONNECTED
    assert livekit.deleted_rooms == [voice["room_name"]]
    a = await _row(avatar)
    assert a.transcript_history == [] and a.browser_transcript == [] and a.report is None and scorer.calls == []
    # Avatar then ends normally and is scored exactly once, only for itself.
    _avatar_ended(client, avatar["session_id"])
    assert len(scorer.calls) == 1
    v = await _row(voice)
    assert v.report is None and v.transcript_history == [] and len(v.browser_transcript) == len(BROWSER)


# --- 2. different students ----------------------------------------------------------------------------------------


async def test_student_a_avatar_and_student_b_voice_share_nothing(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    ta, sa = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, sb = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    l0, l1, _ = _two_lessons_one_day()
    await l0.insert(); await l1.insert()
    await CalendarTemplate(name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE,
                           is_active=True, days=[CalendarDayEntry(day=1, topic_ids=[l0.id, l1.id])]).insert()

    a = _avatar(client, ta, l0.id)
    b = _voice(client, tb, l1.id)
    assert a["room_name"] != b["room_name"] and a["session_id"] != b["session_id"]
    assert _claims(a)["sub"] == f"student-{sa.id}" and _claims(b)["sub"] == f"student-{sb.id}"
    (_, am), (_, bm) = _dispatch(a), _dispatch(b)
    assert am["topic_id"] == l0.id and bm["topic_id"] == l1.id
    assert "AVATAR-PROMPT-1" not in str(am) and "VOICE-PROMPT-0" not in str(bm)
    assert am["opening_line"] == l0.opening_line and bm["lesson_title"] == l1.title

    _avatar_connected(client, a["session_id"])
    _voice_connected(client, livekit, tb, b)
    _avatar_ended(client, a["session_id"])  # A's worker callback
    assert await _state(b) == SessionState.CONNECTED and (await _row(b)).transcript_history == []
    _end(client, tb, b["session_id"], transcript=LATE_BROWSER_TRANSCRIPT)  # B's own end
    row_a, row_b = await _row(a), await _row(b)
    assert all("LATE-BROWSER" not in t.text for t in row_a.transcript_history) and row_a.browser_transcript == []
    assert all("Yes, I would like" not in t.text for t in row_b.browser_transcript) and row_b.transcript_history == []
    assert row_a.student_id == sa.id and row_b.student_id == sb.id
    assert livekit.deleted_rooms == [b["room_name"]]
    assert len(scorer.calls) == 1


# --- 3. rapid switching + late callbacks, both directions --------------------------------------------------------------


async def test_late_avatar_callbacks_cannot_touch_a_new_voice_session(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()

    avatar1 = _avatar(client, token, lesson.id)
    _avatar_connected(client, avatar1["session_id"])
    _avatar_ended(client, avatar1["session_id"])  # Avatar end ...
    voice2 = _voice(client, token, lesson.id)  # ... immediately Voice start
    assert len(scorer.calls) == 1
    first_outcome = await _row(avatar1)

    # ... and LATE Avatar callbacks arrive (duplicates, failures, different transcript).
    assert _avatar_connected(client, avatar1["session_id"]).status_code == 200
    assert _avatar_ended(client, avatar1["session_id"], transcript=LATE_TRANSCRIPT).status_code == 200
    assert _avatar_ended(client, avatar1["session_id"], transcript=LATE_TRANSCRIPT, reason="connection_failure").status_code == 200

    v = await _row(voice2)
    assert v.state == SessionState.CONNECTING, "a late Avatar callback must never move a Voice session"
    assert v.end_reason is None and v.ended_at is None
    assert v.transcript_history == [] and v.browser_transcript == [] and v.report is None
    a = await _row(avatar1)
    assert a.state == first_outcome.state == SessionState.ENDED and a.end_reason == EndReason.USER_ENDED
    assert [t.text for t in a.transcript_history] == [t.text for t in first_outcome.transcript_history]
    assert len(scorer.calls) == 1, "duplicate/late Avatar callbacks must not re-score"
    assert voice2["room_name"] not in livekit.deleted_rooms

    # Voice then proceeds normally, and one more late Avatar callback still cannot reach it.
    assert _voice_connected(client, livekit, token, voice2).json()["state"] == "connected"
    _avatar_ended(client, avatar1["session_id"], transcript=LATE_TRANSCRIPT)
    assert await _state(voice2) == SessionState.CONNECTED


async def test_late_voice_callbacks_cannot_touch_a_new_avatar_session(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()

    voice1 = _voice(client, token, lesson.id)
    _voice_connected(client, livekit, token, voice1)
    _end(client, token, voice1["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)  # Voice end ...
    avatar2 = _avatar(client, token, lesson.id)  # ... immediately Avatar start
    _avatar_connected(client, avatar2["session_id"])
    first_voice = await _row(voice1)
    deleted_before = list(livekit.deleted_rooms)

    # LATE Voice callbacks: duplicate end with other reason/transcript, late connected, and - deliberately wrong -
    # Voice calls carrying the Avatar session's id.
    assert client.post(f"/api/v1/sessions/{voice1['session_id']}/connected", headers=_auth(token)).status_code == 200
    _end(client, token, voice1["session_id"], reason="connection_failure", transcript=LATE_BROWSER_TRANSCRIPT)
    assert _end(client, token, avatar2["session_id"], transcript=LATE_BROWSER_TRANSCRIPT).status_code == 422
    assert client.post(f"/api/v1/sessions/{avatar2['session_id']}/connected", headers=_auth(token)).status_code == 422

    a = await _row(avatar2)
    assert a.state == SessionState.CONNECTED and a.end_reason is None
    assert a.transcript_history == [] and a.browser_transcript == [] and a.report is None
    assert avatar2["room_name"] not in livekit.deleted_rooms
    v = await _row(voice1)
    assert (v.state, v.end_reason) == (first_voice.state, first_voice.end_reason) == (SessionState.ENDED, EndReason.USER_ENDED)
    assert [t.text for t in v.browser_transcript] == [t.text for t in first_voice.browser_transcript]
    assert all(room == voice1["room_name"] for room in livekit.deleted_rooms), "late Voice callbacks only ever release Voice's own room"
    assert scorer.calls == []
    # the new Avatar session completes normally afterwards
    _avatar_ended(client, avatar2["session_id"])
    assert len(scorer.calls) == 1 and deleted_before[0] == voice1["room_name"]


# --- 4. negatives: wrong student / wrong mode / wrong room ----------------------------------------------------------------


async def test_cross_student_and_cross_mode_callbacks_are_rejected_or_ignored(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a_avatar, a_voice = _avatar(client, ta, lesson.id), _voice(client, ta, lesson.id)
    b_voice = _voice(client, tb, lesson.id)
    _avatar_connected(client, a_avatar["session_id"])
    _voice_connected(client, livekit, ta, a_voice)
    _voice_connected(client, livekit, tb, b_voice)

    # B with A's session ids (both modes) on every student route: forbidden, nothing changes.
    for sid in (a_avatar["session_id"], a_voice["session_id"]):
        assert client.post(f"/api/v1/sessions/{sid}/connected", headers=_auth(tb)).status_code == 403
        assert _end(client, tb, sid, transcript=LATE_BROWSER_TRANSCRIPT).status_code == 403
        assert client.get(f"/api/v1/sessions/{sid}", headers=_auth(tb)).status_code == 403
    # "A's room with B's session": no route accepts a room - extra room fields are ignored and only B's OWN room is released.
    resp = client.post(f"/api/v1/sessions/{b_voice['session_id']}/end", headers=_auth(tb),
                       json={"end_reason": "user_ended", "browser_transcript": [], "room_name": a_voice["room_name"], "room": a_voice["room_name"]})
    assert resp.status_code in (200, 422)
    assert a_voice["room_name"] not in livekit.deleted_rooms and a_avatar["room_name"] not in livekit.deleted_rooms
    # Internal worker routes need the key; an unknown id is a 404; a Voice id is ignored (wrong mode).
    assert client.post(f"/api/v1/sessions/{a_avatar['session_id']}/mark-ended", json={"end_reason": "user_ended", "transcript_history": []}).status_code in (401, 403)
    assert client.post("/api/v1/sessions/does-not-exist/mark-connected", headers=INTERNAL).status_code == 404
    _avatar_ended(client, a_voice["session_id"], transcript=LATE_TRANSCRIPT)
    client.post(f"/api/v1/sessions/{a_voice['session_id']}/mark-connected", headers=INTERNAL)
    assert await _state(a_voice) == SessionState.CONNECTED and (await _row(a_voice)).transcript_history == []

    assert await _state(a_avatar) == SessionState.CONNECTED and (await _row(a_avatar)).transcript_history == []
    assert scorer.calls == []


async def test_stale_and_duplicate_callbacks_are_idempotent(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    avatar, voice = _avatar(client, token, lesson.id), _voice(client, token, lesson.id)
    _avatar_connected(client, avatar["session_id"])
    _avatar_ended(client, avatar["session_id"])
    _avatar_ended(client, avatar["session_id"])  # duplicate (worker finalizer + fallback can both fire)
    assert len(scorer.calls) == 1
    _avatar_connected(client, avatar["session_id"])  # stale connected after ended
    assert await _state(avatar) == SessionState.ENDED

    _voice_connected(client, livekit, token, voice)
    _end(client, token, voice["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)
    first = await _row(voice)
    _end(client, token, voice["session_id"], reason="connection_failure", transcript=LATE_BROWSER_TRANSCRIPT)  # duplicate end
    client.post(f"/api/v1/sessions/{voice['session_id']}/connected", headers=_auth(token))  # stale connected
    after = await _row(voice)
    assert (after.state, after.end_reason) == (first.state, first.end_reason)
    assert [t.text for t in after.browser_transcript] == [t.text for t in first.browser_transcript]


# --- 5. rooms, tokens, metadata over many sessions ----------------------------------------------------------------------


async def test_rooms_tokens_and_metadata_are_unique_and_scoped_across_many_sessions(client, livekit):
    lessons = _two_lessons_one_day()
    for lesson in lessons:
        await lesson.insert()
    await CalendarTemplate(name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE, is_active=True,
                           days=[CalendarDayEntry(day=1, topic_ids=[lesson.id for lesson in lessons])]).insert()
    students = [await _login_with_user(client, Role.STUDENT, email=f"s{i}@example.com") for i in range(3)]

    started = []  # (student, lesson, mode, response)
    for (token, student), lesson in zip(students, lessons):
        for mode in ("topic_discussion", "voice_practice", "topic_discussion", "voice_practice", "voice_practice"):  # rapid restarts
            response = _start(client, token, lesson.id, mode=mode).json()
            started.append((student, lesson, mode, response))

    rooms = [r["room_name"] for *_, r in started]
    ids = [r["session_id"] for *_, r in started]
    tokens = [r["participant_token"] for *_, r in started]
    assert len(set(rooms)) == len(rooms) == 15, "no room name is ever reused, even on rapid restart"
    assert len(set(ids)) == len(set(tokens)) == 15
    for student, lesson, mode, r in started:
        prefix = "mariam" if mode == "topic_discussion" else "voice"
        assert r["room_name"] == f"{prefix}-{r['session_id']}"
        claims = _claims(r)
        assert claims["sub"] == f"student-{student.id}" and claims["video"]["room"] == r["room_name"]
        agent, meta = _dispatch(r)
        assert agent == ("tamakkan-mariam-dev" if mode == "topic_discussion" else "tamakkan-voice-dev")
        assert (meta["session_id"], meta["topic_id"]) == (r["session_id"], lesson.id)
        blob = str(meta)
        for other in lessons:
            if other.id != lesson.id:
                assert other.title not in blob and other.avatar_prompt not in blob and other.voice_prompt not in blob
    # Each student's three voice starts: only the newest remains open (only that student's older ones were closed).
    for student, lesson, mode, r in started:
        stored = await LiveSession.get(r["session_id"])
        assert stored.student_id == student.id
    per_student_open_voice = {}
    for student, lesson, mode, r in started:
        if mode == "voice_practice":
            stored = await LiveSession.get(r["session_id"])
            per_student_open_voice.setdefault(student.id, []).append(stored.state == SessionState.CONNECTING)
    assert all(flags.count(True) == 1 and flags[-1] for flags in per_student_open_voice.values())


# --- 6. two tabs, same mode ---------------------------------------------------------------------------------------------


async def test_two_tabs_same_mode_follow_the_current_product_rules(client, livekit):
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    other_voice = _voice(client, tb, lesson.id)
    other_avatar = _avatar(client, tb, lesson.id)

    # Voice: the newest tab wins, and ONLY this student's older voice session is closed.
    v1 = _voice(client, ta, lesson.id)
    _voice_connected(client, livekit, ta, v1)
    v2 = _voice(client, ta, lesson.id)
    assert (await _row(v1)).state == SessionState.ENDED and (await _row(v1)).end_reason == EndReason.USER_ENDED
    assert await _state(v2) == SessionState.CONNECTING
    assert livekit.deleted_rooms == [v1["room_name"]]
    # Avatar: two tabs are independent sessions; neither closes the other.
    a1, a2 = _avatar(client, ta, lesson.id), _avatar(client, ta, lesson.id)
    assert await _state(a1) == SessionState.CONNECTING and await _state(a2) == SessionState.CONNECTING
    assert a1["room_name"] != a2["room_name"]
    _avatar_connected(client, a1["session_id"])
    _avatar_ended(client, a1["session_id"])
    assert await _state(a2) == SessionState.CONNECTING and await _state(v2) == SessionState.CONNECTING
    # The other student is untouched by everything above.
    assert await _state(other_voice) == SessionState.CONNECTING and await _state(other_avatar) == SessionState.CONNECTING
    assert other_voice["room_name"] not in livekit.deleted_rooms


# --- 7. transcripts, reports and progress ----------------------------------------------------------------------------------


async def _topic_states(client, token, topic_id):
    days = client.get("/api/v1/me/challenge-calendar", headers=_auth(token)).json()["lesson_days"]
    return {slot["state"] for day in days for slot in day["topics"] if slot["topic_id"] == topic_id}


async def test_voice_data_never_reaches_avatar_reporting_or_lesson_completion(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()

    voice = _voice(client, token, lesson.id)
    _voice_connected(client, livekit, token, voice)
    row = await _row(voice)
    row.started_at = utcnow() - timedelta(minutes=10)  # long enough to count IF it were a trusted conversation
    await row.save()
    _end(client, token, voice["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)

    stored = await _row(voice)
    assert stored.state == SessionState.ENDED and stored.transcript_history == [] and stored.report is None
    assert len(stored.browser_transcript) == len(SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)
    assert scorer.calls == [], "a Voice end must never reach the Avatar scorer"
    assert "done" not in await _topic_states(client, token, lesson.id), "browser transcript must not complete a lesson"

    # The same lesson IS completed by a genuine Avatar (trusted) conversation - proving the check above is meaningful.
    avatar = _avatar(client, token, lesson.id)
    _avatar_connected(client, avatar["session_id"])
    arow = await _row(avatar)
    arow.started_at = utcnow() - timedelta(minutes=10)
    await arow.save()
    _avatar_ended(client, avatar["session_id"])
    assert len(scorer.calls) == 1 and (await _row(avatar)).report is not None
    assert "done" in await _topic_states(client, token, lesson.id)
    voice_again = await _row(voice)
    assert voice_again.report is None and voice_again.transcript_history == []


async def test_report_generation_itself_refuses_voice_sessions(client, livekit):
    """Defense in depth: even if a Voice record somehow held a trusted-looking transcript, scoring must refuse it."""
    from app.models.live_session import InteractionTurn
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.live_session_repository import LiveSessionRepository
    from app.repositories.topic_content_repository import TopicContentRepository
    from app.repositories.user_repository import UserRepository
    from app.services.enrollment_service import get_enrollment_service
    from app.services.live_session_service import LiveSessionService
    from app.services.progress_service import get_progress_service

    scorer = FakeSessionScorer()
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    voice = _voice(client, token, lesson.id)
    row = await _row(voice)
    row.transcript_history = [InteractionTurn(**turn) for turn in SUFFICIENT_EVIDENCE_TRANSCRIPT]  # corrupted / hand-edited data
    await row.save()

    service = LiveSessionService(LiveSessionRepository(), TopicContentRepository(), UserRepository(), AuditLogRepository(), scorer, get_progress_service(), get_enrollment_service())
    await service.generate_report(voice["session_id"])
    assert scorer.calls == [] and (await _row(voice)).report is None

    avatar = _avatar(client, token, lesson.id)  # the same call DOES work for an Avatar session
    arow = await _row(avatar)
    arow.transcript_history = [InteractionTurn(**turn) for turn in SUFFICIENT_EVIDENCE_TRANSCRIPT]
    await arow.save()
    await service.generate_report(avatar["session_id"])
    assert len(scorer.calls) == 1 and (await _row(avatar)).report is not None


async def test_one_students_transcript_never_appears_in_another_students_session(client, livekit):
    ta, _ = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a, b = _voice(client, ta, lesson.id), _voice(client, tb, lesson.id)
    _voice_connected(client, livekit, ta, a)
    _voice_connected(client, livekit, tb, b)
    _end(client, ta, a["session_id"], transcript=LATE_BROWSER_TRANSCRIPT)
    BROWSER_B = [{"speaker": "student", "text": "ONLY-B-SAYS-THIS", "timestamp": "2026-01-01T00:00:00"}]
    _end(client, tb, b["session_id"], transcript=BROWSER_B)
    row_a, row_b = await _row(a), await _row(b)
    assert all("ONLY-B" not in t.text for t in row_a.browser_transcript)
    assert all("LATE-BROWSER" not in t.text for t in row_b.browser_transcript) and len(row_b.browser_transcript) == len(BROWSER_B)
    # list endpoints are per student too
    mine = client.get("/api/v1/sessions/me", headers=_auth(ta)).json()
    assert {item["id"] for item in mine} == {a["session_id"]}


# --- 8. reconnect --------------------------------------------------------------------------------------------------------------


async def test_reconnect_reuses_the_original_session_and_leaves_others_untouched(client, livekit):
    ta, sa = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    tb, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    lesson = await _lesson()
    a, b = _voice(client, ta, lesson.id), _voice(client, tb, lesson.id)
    _voice_connected(client, livekit, ta, a)
    _voice_connected(client, livekit, tb, b)
    sessions_before = len(await LiveSession.find(LiveSession.student_id == sa.id).to_list())

    # A's connection drops and recovers: the agent leaves and re-joins the SAME room; the browser re-confirms.
    livekit.participants.pop(a["room_name"], None)
    livekit.agent_joins(a["room_name"])
    again = client.post(f"/api/v1/sessions/{a['session_id']}/connected", headers=_auth(ta))
    assert again.status_code == 200 and again.json()["state"] == "connected"
    assert len(await LiveSession.find(LiveSession.student_id == sa.id).to_list()) == sessions_before, "reconnect must not create a session"
    row = await _row(a)
    assert row.livekit_room_name == a["room_name"]
    assert await _state(b) == SessionState.CONNECTED and livekit.deleted_rooms == []

    # A can still end on its original session id, releasing only its own room.
    _end(client, ta, a["session_id"], transcript=LATE_BROWSER_TRANSCRIPT)
    assert livekit.deleted_rooms == [a["room_name"]]
    assert await _state(b) == SessionState.CONNECTED
