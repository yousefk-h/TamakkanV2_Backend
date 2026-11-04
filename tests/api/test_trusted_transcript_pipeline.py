"""The AUTHORITATIVE transcript path, for Avatar and Voice Practice.

Production symptom: Avatar sessions stayed `connecting` with no transcript, and Voice sessions ended with only a
`browser_transcript` - so no report was ever scheduled and nothing said why. These tests pin the contract:

- only the agents' internal routes write `transcript_history` (what reports are scored from);
- the student's `browser_transcript` stays untrusted: never copied, never scored;
- Voice Practice has its own internal route (`voice-transcript`), so Mariam's routes keep ignoring Voice sessions;
- every end call logs counts/status and the reason a report was or was not scheduled - never transcript text.
"""
import pytest

from app.core.config import get_settings
from app.models.live_session import EndReason, LiveSession, SessionState
from app.models.user import Role
from app.services.live_session_service import MarkEndedResult, report_schedule_decision
from tests.api.test_sessions import (  # noqa: F401  (fixtures are used implicitly)
    DEFAULT_ADMIN_ID,
    FAKE_INTERNAL_API_KEY,
    SUFFICIENT_EVIDENCE_TRANSCRIPT,
    FakeSessionScorer,
    _capture_logs,
    _login_with_user,
    _override_session_scorer,
)
from tests.api.test_voice_practice import (  # noqa: F401
    BROWSER_TRANSCRIPT,
    _auth,
    _clear_overrides,
    _end,
    _fake_livekit_credentials,
    _lesson,
    _start,
    livekit,
)

INTERNAL = {"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}


class OrderCheckingScorer(FakeSessionScorer):
    """Records how many trusted turns were already PERSISTED at the moment scoring ran."""

    session_id: str = ""
    persisted_turns_at_scoring: int = -1

    async def score(self, transcript):
        self.persisted_turns_at_scoring = len((await LiveSession.get(self.session_id)).transcript_history)
        return await super().score(transcript)


async def _voice(client, scorer=None):
    scorer = scorer or FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    if isinstance(scorer, OrderCheckingScorer):
        scorer.session_id = started["session_id"]
    return token, started, scorer


def _voice_transcript(client, session_id, transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT, reason="user_ended", headers=INTERNAL):
    return client.post(f"/api/v1/sessions/{session_id}/voice-transcript", json={"end_reason": reason, "transcript_history": transcript}, headers=headers)


# --- Avatar -----------------------------------------------------------------------------------------------------------------


async def test_avatar_connect_then_end_callbacks_move_the_session_out_of_connecting_and_persist_the_trusted_transcript(client, livekit):
    scorer = OrderCheckingScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id, mode="topic_discussion").json()
    scorer.session_id = started["session_id"]
    assert (await LiveSession.get(started["session_id"])).state == SessionState.CONNECTING

    assert client.post(f"/api/v1/sessions/{started['session_id']}/mark-connected", headers=INTERNAL).status_code == 200
    assert (await LiveSession.get(started["session_id"])).state == SessionState.CONNECTED
    resp = client.post(f"/api/v1/sessions/{started['session_id']}/mark-ended",
                       json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT}, headers=INTERNAL)
    assert resp.status_code == 200

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED and len(stored.transcript_history) == 6
    assert stored.browser_transcript == []
    assert scorer.persisted_turns_at_scoring == 6, "the transcript is persisted BEFORE report generation runs"
    assert stored.report is not None and len(scorer.calls) == 1


async def test_a_valid_report_is_never_generated_twice(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id, mode="topic_discussion").json()
    client.post(f"/api/v1/sessions/{started['session_id']}/mark-connected", headers=INTERNAL)
    body = {"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT}
    for _ in range(3):
        client.post(f"/api/v1/sessions/{started['session_id']}/mark-ended", json=body, headers=INTERNAL)
    assert len(scorer.calls) == 1


async def test_an_end_with_a_transcript_rescues_a_session_whose_connected_callback_was_lost(client, livekit):
    """Production symptom: sessions stuck in `connecting` with no transcript. If `mark-connected` is lost but `mark-ended`
    arrives, the transcript used to be dropped silently."""
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id, mode="topic_discussion").json()
    assert (await LiveSession.get(started["session_id"])).state == SessionState.CONNECTING  # mark-connected never called

    resp = client.post(f"/api/v1/sessions/{started['session_id']}/mark-ended",
                       json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT}, headers=INTERNAL)
    assert resp.status_code == 200
    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED and len(stored.transcript_history) == 6 and stored.report is not None
    assert len(scorer.calls) == 1


async def test_an_end_without_a_transcript_still_cannot_move_a_connecting_session(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id, mode="topic_discussion").json()
    client.post(f"/api/v1/sessions/{started['session_id']}/mark-ended", json={"end_reason": "user_ended"}, headers=INTERNAL)
    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.CONNECTING and scorer.calls == []


# --- Voice Practice -----------------------------------------------------------------------------------------------------------


async def test_a_voice_session_with_only_a_browser_transcript_is_never_scored(client, livekit):
    token, started, scorer = await _voice(client)
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))
    assert _end(client, token, started["session_id"], transcript=BROWSER_TRANSCRIPT).status_code == 200

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED
    assert len(stored.browser_transcript) == 2 and stored.transcript_history == []
    assert stored.report is None and scorer.calls == []


async def test_the_voice_agents_own_transcript_is_persisted_then_scored(client, livekit):
    token, started, scorer = await _voice(client, OrderCheckingScorer())
    resp = _voice_transcript(client, started["session_id"])
    assert resp.status_code == 200

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED and stored.end_reason == EndReason.USER_ENDED
    assert len(stored.transcript_history) == 6 and stored.browser_transcript == []
    assert scorer.persisted_turns_at_scoring == 6
    assert stored.report is not None and stored.report.analysis.status == "assessed"


async def test_the_voice_agents_student_and_mariam_turns_are_stored_in_order_with_their_roles(client, livekit):
    _, started, _ = await _voice(client)
    assert _voice_transcript(client, started["session_id"]).status_code == 200
    stored = await LiveSession.get(started["session_id"])
    assert [t.speaker for t in stored.transcript_history] == [t["speaker"] for t in SUFFICIENT_EVIDENCE_TRANSCRIPT]
    assert {t.speaker for t in stored.transcript_history} == {"student", "mariam"}
    assert [t.text for t in stored.transcript_history] == [t["text"] for t in SUFFICIENT_EVIDENCE_TRANSCRIPT]
    assert stored.transcript_source == "voice-agent"


async def test_a_too_short_voice_session_is_stored_but_reports_insufficient_evidence_without_scoring(client, livekit):
    token, started, scorer = await _voice(client)
    short = [{"speaker": "student", "text": "Hello", "timestamp": "2026-01-01T00:00:00"}]
    assert _voice_transcript(client, started["session_id"], transcript=short).status_code == 200
    stored = await LiveSession.get(started["session_id"])
    assert len(stored.transcript_history) == 1 and scorer.calls == [], "the evidence bar is unchanged: no scorer call"
    assert stored.report is not None and stored.report.overall_score is None
    assert stored.report.analysis.status == "insufficient_evidence"


async def test_a_scored_voice_report_appears_in_the_admin_reports_list_filtered_by_voice_mode(client, livekit):
    # The test student belongs to DEFAULT_ADMIN_ID (see `_login_with_user`), so that admin is the one who lists the report.
    admin_token, _ = await _login_with_user(client, Role.ADMINISTRATOR, email="reports-admin@example.com", id=DEFAULT_ADMIN_ID)
    token, _ = await _login_with_user(client, Role.STUDENT, email="voice-student@example.com")
    _override_session_scorer(FakeSessionScorer())
    started = _start(client, token, (await _lesson()).id).json()
    assert _voice_transcript(client, started["session_id"]).status_code == 200

    headers = {"Authorization": f"Bearer {admin_token}"}
    voice = client.get("/api/v1/sessions", params={"mode": "voice_practice", "report_status": "scored", "page": 1}, headers=headers).json()
    assert [i["id"] for i in voice["items"]] == [started["session_id"]]
    assert voice["items"][0]["mode"] == "voice_practice" and voice["items"][0]["report"]["overall_score"] is not None
    avatar_only = client.get("/api/v1/sessions", params={"mode": "topic_discussion", "report_status": "any", "page": 1}, headers=headers).json()
    assert avatar_only["items"] == []


async def test_a_late_agent_transcript_is_attached_after_the_browser_ended_without_touching_the_untrusted_one(client, livekit):
    token, started, scorer = await _voice(client)
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))
    _end(client, token, started["session_id"], transcript=BROWSER_TRANSCRIPT)
    after_browser = await LiveSession.get(started["session_id"])
    assert after_browser.report is None and scorer.calls == []

    assert _voice_transcript(client, started["session_id"]).status_code == 200
    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED and stored.end_reason == after_browser.end_reason
    assert [t.text for t in stored.browser_transcript] == [t["text"] for t in BROWSER_TRANSCRIPT], "browser transcript untouched"
    assert [t.text for t in stored.transcript_history] == [t["text"] for t in SUFFICIENT_EVIDENCE_TRANSCRIPT], "trusted = the agent's"
    assert stored.report is not None and len(scorer.calls) == 1


async def test_the_browser_can_end_after_the_agent_transcript_arrived_and_nothing_is_overwritten(client, livekit):
    token, started, scorer = await _voice(client)
    _voice_transcript(client, started["session_id"])
    _end(client, token, started["session_id"], transcript=BROWSER_TRANSCRIPT)
    stored = await LiveSession.get(started["session_id"])
    assert len(stored.transcript_history) == 6 and stored.browser_transcript == []
    assert len(scorer.calls) == 1


async def test_a_repeated_voice_transcript_changes_nothing_and_never_rescores(client, livekit):
    _, started, scorer = await _voice(client)
    _voice_transcript(client, started["session_id"])
    again = _voice_transcript(client, started["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT[:2])
    assert again.status_code == 200
    assert len((await LiveSession.get(started["session_id"])).transcript_history) == 6 and len(scorer.calls) == 1


async def test_an_empty_voice_transcript_schedules_nothing(client, livekit):
    _, started, scorer = await _voice(client)
    assert _voice_transcript(client, started["session_id"], transcript=[]).status_code == 200
    stored = await LiveSession.get(started["session_id"])
    assert stored.transcript_history == [] and stored.report is None and scorer.calls == []


async def test_the_voice_transcript_route_is_internal_and_voice_only(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    voice = _start(client, token, lesson.id).json()
    avatar = _start(client, token, lesson.id, mode="topic_discussion").json()

    for headers in ({}, {"X-Internal-Api-Key": "wrong"}, _auth(token)):
        assert _voice_transcript(client, voice["session_id"], headers=headers).status_code == 401
    assert (await LiveSession.get(voice["session_id"])).transcript_history == []
    assert _voice_transcript(client, "does-not-exist").status_code == 404

    # An Avatar session is not Voice: this route leaves it completely alone.
    assert _voice_transcript(client, avatar["session_id"]).status_code == 200
    stored = await LiveSession.get(avatar["session_id"])
    assert stored.state == SessionState.CONNECTING and stored.transcript_history == [] and scorer.calls == []


async def test_a_voice_session_that_never_connected_is_left_alone(client, livekit):
    token, started, scorer = await _voice(client)
    _end(client, token, started["session_id"])  # never CONNECTED -> CONNECTION_FAILED
    assert (await LiveSession.get(started["session_id"])).state == SessionState.CONNECTION_FAILED
    _voice_transcript(client, started["session_id"])
    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.CONNECTION_FAILED and stored.transcript_history == [] and scorer.calls == []


# --- diagnostics: counts and reasons, never speech -----------------------------------------------------------------------------


def _lines(records):
    return [message for _, message in records]


async def test_end_logs_carry_counts_and_the_scheduling_decision_but_never_transcript_text(client, livekit):
    _override_session_scorer(FakeSessionScorer())
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    ok = _start(client, token, lesson.id, mode="topic_discussion").json()
    empty = _start(client, token, lesson.id, mode="topic_discussion").json()
    voice = _start(client, token, lesson.id).json()
    for sid in (ok["session_id"], empty["session_id"]):
        client.post(f"/api/v1/sessions/{sid}/mark-connected", headers=INTERNAL)

    records, stop = _capture_logs()
    try:
        client.post(f"/api/v1/sessions/{ok['session_id']}/mark-ended", json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT}, headers=INTERNAL)
        client.post(f"/api/v1/sessions/{empty['session_id']}/mark-ended", json={"end_reason": "user_ended"}, headers=INTERNAL)
        _end(client, token, voice["session_id"], transcript=BROWSER_TRANSCRIPT)
    finally:
        stop()
    joined = "\n".join(_lines(records))

    good = next(l for l in _lines(records) if ok["session_id"] in l and "session end [agent]" in l)
    assert "trusted_transcript_turns=6" in good and "report_generation_scheduled=YES" in good
    none = next(l for l in _lines(records) if empty["session_id"] in l and "session end [agent]" in l)
    assert "trusted_transcript_turns=0" in none and "report_generation_scheduled=NO" in none and "no trusted transcript" in none
    browser = next(l for l in _lines(records) if voice["session_id"] in l and "student-browser" in l)
    assert "browser_transcript_turns=2" in browser and "trusted_transcript_turns=0" in browser and "report_generation_scheduled=NO" in browser
    assert "untrusted" in browser
    for turn in SUFFICIENT_EVIDENCE_TRANSCRIPT + BROWSER_TRANSCRIPT:
        assert turn["text"] not in joined, "no transcript text may appear in the logs"
    assert any("report input" in l and "trusted_turns=6" in l and "enough_evidence=True" in l for l in _lines(records))


async def test_a_session_start_logs_which_agent_it_was_dispatched_to(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    records, stop = _capture_logs()
    try:
        started = _start(client, token, lesson.id, mode="topic_discussion").json()
    finally:
        stop()
    assert any(started["session_id"] in l and "dispatched_agent=tamakkan-mariam-dev" in l for l in _lines(records))


async def test_a_rejected_internal_call_is_logged_without_the_key(client, livekit):
    records, stop = _capture_logs()
    try:
        client.post("/api/v1/sessions/abc/mark-connected", headers={"X-Internal-Api-Key": "a-wrong-key-value"})
        client.post("/api/v1/sessions/abc/mark-ended", json={"end_reason": "user_ended"})
    finally:
        stop()
    joined = "\n".join(_lines(records))
    assert "key does not match the backend's" in joined and "no key sent" in joined
    assert "/api/v1/sessions/abc/mark-connected" in joined
    assert "a-wrong-key-value" not in joined and FAKE_INTERNAL_API_KEY not in joined


@pytest.mark.parametrize(
    "transitioned,state,turns,scheduled,fragment",
    [
        (True, SessionState.ENDED, 6, True, "received"),
        (True, SessionState.ENDED, 0, False, "no trusted transcript"),
        (True, SessionState.CONNECTION_FAILED, 6, False, "did not end normally"),
        (False, SessionState.ENDED, 6, False, "duplicate"),
    ],
)
async def test_report_schedule_decision(transitioned, state, turns, scheduled, fragment):
    from types import SimpleNamespace

    session = SimpleNamespace(state=state, transcript_history=["turn"] * turns)
    ok, reason = report_schedule_decision(MarkEndedResult(session=session, transitioned=transitioned))
    assert ok is scheduled and fragment in reason


async def test_the_voice_transcript_is_stamped_with_its_provenance(client, livekit):
    _, started, _ = await _voice(client)
    _voice_transcript(client, started["session_id"])
    assert (await LiveSession.get(started["session_id"])).transcript_source == "voice-agent"
