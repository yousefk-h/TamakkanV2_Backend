"""Stage 3: Avatar -> Cartoon conversation-context handoff (`POST /sessions/{id}/handoff`). Same fixtures/pattern as
`tests/api/test_sessions.py` (fake-but-present LiveKit credentials; no real LiveKit server)."""

import json

import pytest

from app.core.application import app
from app.core.config import get_settings
from app.core.security import hash_password
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import InteractionTurn, LiveSession, PracticeMode, SessionState
from app.models.topic_content import Level, TopicContent
from app.models.user import PlanType, Role, User
from app.services.live_session_service import HANDOFF_CONTEXT_MAX_TURNS, get_live_session_service
from app.utils.time import utcnow

DEFAULT_ADMIN_ID = "default-test-admin"


@pytest.fixture(autouse=True)
def _clear_session_service_override():
    yield
    app.dependency_overrides.pop(get_live_session_service, None)


@pytest.fixture(autouse=True)
def _fake_livekit_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake-secret-thats-long-enough")
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", "fake-internal-agent-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _login_with_user(client, role: Role, email: str = "test@example.com", **extra) -> tuple[str, User]:
    if role == Role.STUDENT:
        extra.setdefault("plan", PlanType.STANDARD)
        extra.setdefault("assigned_level", Level.INTERMEDIATE)
        extra.setdefault("created_by_id", DEFAULT_ADMIN_ID)
        extra.setdefault("program_start_date", utcnow())
    user = User(email=email, password_hash=hash_password("correct-password"), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"})
    return resp.json()["access_token"], user


async def _make_topic(level: Level = Level.INTERMEDIATE, owner_admin_id: str = DEFAULT_ADMIN_ID) -> TopicContent:
    topic = TopicContent(title="Talking about Music", level=level)
    await topic.insert()
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=owner_admin_id, level=level, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    )
    await template.insert()
    return topic


async def _avatar_session(student: User, topic: TopicContent, state=SessionState.CONNECTED, transcript=None) -> LiveSession:
    session = LiveSession(
        student=student, student_id=student.id, topic=topic, topic_id=topic.id,
        mode=PracticeMode.TOPIC_DISCUSSION, assessment_level=topic.level.value, state=state,
        transcript_history=transcript or [],
    )
    await session.insert()
    session.livekit_room_name = f"mariam-{session.id}"
    await session.save()
    return session


async def test_handoff_creates_a_cartoon_session_via_the_existing_start_session_path(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["room_name"].startswith("voice-")
    target = await LiveSession.get(body["session_id"])
    assert target.mode == PracticeMode.VOICE_PRACTICE
    assert target.handoff_source_session_id == source.id
    assert target.continuation_context is not None

    refreshed_source = await LiveSession.get(source.id)
    assert refreshed_source.handed_off_to_session_id == target.id
    # Never ended/touched otherwise by the handoff call.
    assert refreshed_source.state == SessionState.CONNECTED


async def test_handoff_context_is_bounded_to_the_last_n_turns_and_carries_last_question(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    many_turns = [
        InteractionTurn(speaker="mariam" if i % 2 == 0 else "student", text=f"turn {i}", timestamp=utcnow())
        for i in range(20)
    ]
    source = await _avatar_session(student, topic, transcript=many_turns)

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    target = await LiveSession.get(resp.json()["session_id"])

    recent_turns = target.continuation_context["recent_turns"]
    assert len(recent_turns) == HANDOFF_CONTEXT_MAX_TURNS
    assert recent_turns == [{"speaker": t.speaker, "text": t.text} for t in many_turns[-HANDOFF_CONTEXT_MAX_TURNS:]]
    # Turn 18 (even index) is "mariam"; it is the last tutor turn in the bounded window.
    assert target.continuation_context["last_question"] == "turn 18"

    from jose import jwt
    claims = jwt.get_unverified_claims(resp.json()["participant_token"])
    metadata = json.loads(claims["roomConfig"]["agents"][0]["metadata"])
    assert metadata["continuation"]["last_question"] == "turn 18"


async def test_handoff_rejects_a_session_owned_by_another_student(client):
    token, _ = await _login_with_user(client, Role.STUDENT, email="attacker@example.com")
    _, other_student = await _login_with_user(client, Role.STUDENT, email="victim@example.com")
    topic = await _make_topic()
    source = await _avatar_session(other_student, topic)

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403


async def test_handoff_rejects_a_voice_practice_source_session(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = LiveSession(
        student=student, student_id=student.id, topic=topic, topic_id=topic.id,
        mode=PracticeMode.VOICE_PRACTICE, assessment_level=topic.level.value, state=SessionState.CONNECTED,
    )
    await source.insert()

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 422


async def test_handoff_is_idempotent_on_retry_and_never_creates_a_second_target(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)

    first = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})
    second = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    assert len(second.json()["participant_token"]) > 20  # the retry still mints a usable token for the SAME target
    all_voice_sessions = await LiveSession.find(LiveSession.mode == PracticeMode.VOICE_PRACTICE).to_list()
    assert len(all_voice_sessions) == 1


async def test_source_session_untouched_when_target_creation_fails(client, monkeypatch):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)

    # Starves `start_session`'s own config check so target creation fails without a session being created.
    monkeypatch.delenv("AGENT_INTERNAL_API_KEY", raising=False)  # irrelevant to VOICE_PRACTICE's required list
    monkeypatch.setenv("LIVEKIT_URL", "")
    get_settings.cache_clear()

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 503
    refreshed_source = await LiveSession.get(source.id)
    assert refreshed_source.handed_off_to_session_id is None
    assert await LiveSession.find(LiveSession.mode == PracticeMode.VOICE_PRACTICE).to_list() == []


# --- Live mid-call progress flush (`POST /sessions/{id}/flush-progress`) and its effect on the handoff context ---

FAKE_INTERNAL_KEY_HEADER = {"X-Internal-Api-Key": "fake-internal-agent-key"}


async def test_flush_progress_accepts_valid_internal_auth_and_updates_live_recent_turns(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)  # CONNECTED, no transcript yet - the still-live case

    turns = [
        {"speaker": "mariam", "text": "Tell me about your weekend.", "timestamp": utcnow().isoformat()},
        {"speaker": "student", "text": "I went hiking.", "timestamp": utcnow().isoformat()},
    ]
    resp = client.post(
        f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": turns}, headers=FAKE_INTERNAL_KEY_HEADER
    )

    assert resp.status_code == 200
    refreshed = await LiveSession.get(source.id)
    assert [t.speaker for t in refreshed.live_recent_turns] == ["mariam", "student"]
    assert [t.text for t in refreshed.live_recent_turns] == ["Tell me about your weekend.", "I went hiking."]
    # The authoritative transcript is untouched by a mere progress flush.
    assert refreshed.transcript_history == []


async def test_flush_progress_rejects_a_missing_or_wrong_internal_key(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)
    turns = [{"speaker": "student", "text": "hi", "timestamp": utcnow().isoformat()}]

    no_key = client.post(f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": turns})
    wrong_key = client.post(
        f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": turns},
        headers={"X-Internal-Api-Key": "wrong-key"},
    )

    assert no_key.status_code in (401, 403)
    assert wrong_key.status_code in (401, 403)
    refreshed = await LiveSession.get(source.id)
    assert refreshed.live_recent_turns == []


async def test_flush_progress_overwrites_with_latest_bounded_window_preserving_order_and_roles(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic)

    first_batch = [
        {"speaker": "mariam", "text": "turn 1", "timestamp": utcnow().isoformat()},
        {"speaker": "student", "text": "turn 2", "timestamp": utcnow().isoformat()},
    ]
    client.post(f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": first_batch}, headers=FAKE_INTERNAL_KEY_HEADER)

    second_batch = [
        {"speaker": "mariam", "text": "turn 3", "timestamp": utcnow().isoformat()},
        {"speaker": "student", "text": "turn 4", "timestamp": utcnow().isoformat()},
    ]
    resp = client.post(f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": second_batch}, headers=FAKE_INTERNAL_KEY_HEADER)

    assert resp.status_code == 200
    refreshed = await LiveSession.get(source.id)
    # The later flush REPLACES (not appends to) the window - the worker always sends its own current bounded tail.
    assert [t.text for t in refreshed.live_recent_turns] == ["turn 3", "turn 4"]


async def test_flush_progress_ignores_an_already_ended_session(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic, state=SessionState.ENDED)
    turns = [{"speaker": "student", "text": "too late", "timestamp": utcnow().isoformat()}]

    resp = client.post(f"/api/v1/sessions/{source.id}/flush-progress", json={"recent_turns": turns}, headers=FAKE_INTERNAL_KEY_HEADER)

    assert resp.status_code == 200  # best-effort, same "ignore unreachable state" shape as mark-connected
    refreshed = await LiveSession.get(source.id)
    assert refreshed.live_recent_turns == []


async def test_handoff_pulls_from_live_recent_turns_for_a_still_active_session(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic, state=SessionState.CONNECTED)  # no transcript_history yet
    client.post(
        f"/api/v1/sessions/{source.id}/flush-progress",
        json={"recent_turns": [
            {"speaker": "mariam", "text": "What is your favorite food?", "timestamp": utcnow().isoformat()},
            {"speaker": "student", "text": "Pizza.", "timestamp": utcnow().isoformat()},
        ]},
        headers=FAKE_INTERNAL_KEY_HEADER,
    )

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    target = await LiveSession.get(resp.json()["session_id"])
    recent_turns = target.continuation_context["recent_turns"]
    assert recent_turns == [
        {"speaker": "mariam", "text": "What is your favorite food?"},
        {"speaker": "student", "text": "Pizza."},
    ]
    assert target.continuation_context["last_question"] == "What is your favorite food?"


async def test_handoff_still_uses_full_transcript_history_once_the_session_has_ended(client):
    """A session that already ended normally (`mark_ended` wrote its real transcript) is unaffected by this feature -
    even if a stale `live_recent_turns` is still sitting there from before the call ended, the real, final
    `transcript_history` wins once it exists."""
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    full_history = [
        InteractionTurn(speaker="mariam", text="real question", timestamp=utcnow()),
        InteractionTurn(speaker="student", text="real answer", timestamp=utcnow()),
    ]
    source = await _avatar_session(student, topic, state=SessionState.ENDED, transcript=full_history)
    source.live_recent_turns = [InteractionTurn(speaker="mariam", text="stale, pre-end turn", timestamp=utcnow())]
    await source.save()

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    target = await LiveSession.get(resp.json()["session_id"])
    assert target.continuation_context["recent_turns"] == [
        {"speaker": "mariam", "text": "real question"},
        {"speaker": "student", "text": "real answer"},
    ]
    assert target.continuation_context["last_question"] == "real question"


async def test_handoff_reports_honestly_empty_context_when_nothing_was_flushed_yet(client):
    """First seconds of a live call: no `mark_ended` yet, no flush yet. The continuation context must be genuinely
    empty, never fabricated."""
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    source = await _avatar_session(student, topic, state=SessionState.CONNECTED)

    resp = client.post(f"/api/v1/sessions/{source.id}/handoff", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    target = await LiveSession.get(resp.json()["session_id"])
    assert target.continuation_context["recent_turns"] == []
    assert target.continuation_context["last_question"] is None
