"""Voice Practice: the hosted voice-only LiveKit Builder agent, run as a second
practice mode on the SAME lesson, session and entitlement system as Mariam's
Avatar Practice.

Covers the teacher's shared/separate prompt choice, dispatch (agent name,
room, identity, metadata), session isolation between students, modes and
tabs, the student-driven connected/end lifecycle, and the transcript trust
boundary (a browser-captured transcript is stored as unverified and never
scored). No real LiveKit or Gemini: tokens are local JWT signatures, and the
LiveKit server API (list participants / delete room) is faked.
"""

import json
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from app.core.config import get_settings
from app.core.prompt_safety import wrap_untrusted_lesson_content
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import EndReason, LiveSession, PracticeMode, SessionState
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

LIVEKIT_SECRET = "fake-secret-thats-long-enough"
AGENT_KIND = 4  # livekit ParticipantInfo.Kind.AGENT
STANDARD_KIND = 0


@pytest.fixture(autouse=True)
def _fake_livekit_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", LIVEKIT_SECRET)
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", FAKE_INTERNAL_API_KEY)
    monkeypatch.setenv("LIVEKIT_AGENT_NAME", "tamakkan-mariam-dev")
    monkeypatch.delenv("LIVEKIT_VOICE_AGENT_NAME", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_overrides():
    from app.core.application import app
    from app.services.live_session_service import get_live_session_service

    yield
    app.dependency_overrides.pop(get_live_session_service, None)


class FakeLiveKit:
    """Stands in for LiveKit's server API: which rooms hold which participant
    kinds, and which rooms were deleted."""

    def __init__(self):
        self.participants: dict[str, list[int]] = {}
        self.deleted_rooms: list[str] = []
        self.fail_deletes = False

    def agent_joins(self, room: str) -> None:
        self.participants.setdefault(room, []).append(AGENT_KIND)


@pytest.fixture(autouse=True)
def livekit(monkeypatch):
    fake = FakeLiveKit()

    class _Room:
        async def list_participants(self, request):
            return SimpleNamespace(participants=[SimpleNamespace(kind=k) for k in fake.participants.get(request.room, [])])

        async def delete_room(self, request):
            if fake.fail_deletes:
                raise RuntimeError("LiveKit unreachable")
            fake.deleted_rooms.append(request.room)
            fake.participants.pop(request.room, None)

    class _API:
        def __init__(self, *args, **kwargs):
            self.room = _Room()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import app.services.live_session_service as service_module

    monkeypatch.setattr(service_module.livekit_api, "LiveKitAPI", _API)
    return fake


# --- helpers ------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _lesson(level: Level = Level.INTERMEDIATE, admin_id: str = DEFAULT_ADMIN_ID, **fields) -> TopicContent:
    topic = TopicContent(title=fields.pop("title", f"Lesson {level.value}"), level=level, **fields)
    await topic.insert()
    await CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=admin_id, level=level, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    ).insert()
    return topic


def _start(client, token: str, topic_id: str, mode: str | None = "voice_practice"):
    body = {"topic_id": topic_id} if mode is None else {"topic_id": topic_id, "mode": mode}
    return client.post("/api/v1/sessions/start", json=body, headers=_auth(token))


def _claims(started: dict) -> dict:
    return jose_jwt.decode(started["participant_token"], LIVEKIT_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def _dispatch(started: dict) -> tuple[str, dict]:
    agents = _claims(started)["roomConfig"]["agents"]
    assert len(agents) == 1, "exactly one agent must be dispatched per session"
    return agents[0]["agentName"], json.loads(agents[0]["metadata"])


def _end(client, token, session_id, reason="user_ended", transcript=None):
    return client.post(
        f"/api/v1/sessions/{session_id}/end",
        json={"end_reason": reason, "browser_transcript": transcript or []},
        headers=_auth(token),
    )


BROWSER_TRANSCRIPT = [
    {"speaker": "agent", "text": "Hi! Ready to talk about music?", "timestamp": "2026-01-01T00:00:00"},
    {"speaker": "student", "text": "Yes, I love pop music and classical music.", "timestamp": "2026-01-01T00:00:04"},
]


# --- teacher prompt configuration ----------------------------------------------


async def _admin(client, email="admin@example.com"):
    token, admin = await _login_with_user(client, Role.ADMINISTRATOR, email=email)
    return token, admin


async def test_old_lesson_without_voice_fields_uses_avatar_prompt_for_both_modes(client):
    """A lesson stored before Voice Practice existed has no voice fields at
    all - it must load as "shared" and drive both agents unchanged."""
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson(avatar_prompt="Talk about music.", opening_line="Hello!")
    await TopicContent.get_pymongo_collection().update_one(
        {"_id": topic.id}, {"$unset": {"voice_prompt": "", "voice_prompt_mode": ""}}
    )
    raw = await TopicContent.get_pymongo_collection().find_one({"_id": topic.id})
    assert "voice_prompt" not in raw and "voice_prompt_mode" not in raw

    reloaded = await TopicContent.get(topic.id)
    assert reloaded.voice_prompt_mode == VoicePromptMode.SHARED
    assert reloaded.voice_practice_prompt == "Talk about music."

    _, voice_meta = _dispatch(_start(client, token, topic.id).json())
    _, avatar_meta = _dispatch(_start(client, token, topic.id, mode=None).json())
    # Wrapped in an explicit "lesson reference material" boundary (see
    # app.core.prompt_safety) rather than sent as the raw string.
    assert "Talk about music." in voice_meta["lesson_prompt"]
    assert "Talk about music." in avatar_meta["topic_instructions"]


async def test_shared_prompt_is_created_once_and_sent_to_both_agents(client):
    admin_token, _ = await _admin(client)
    created = client.post(
        "/api/v1/topics",
        json={"title": "Music", "level": "intermediate", "avatar_prompt": "One prompt for both.", "voice_prompt_mode": "shared"},
        headers=_auth(admin_token),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["voice_prompt_mode"] == "shared"
    assert body["voice_prompt"] == ""
    topic = await TopicContent.get(body["id"])
    assert topic.voice_practice_prompt == "One prompt for both."


async def test_separate_prompts_send_each_agent_only_its_own_prompt(client):
    admin_token, admin = await _admin(client)
    created = client.post(
        "/api/v1/topics",
        json={
            "title": "Music", "level": "intermediate",
            "avatar_prompt": "AVATAR-ONLY prompt", "voice_prompt_mode": "separate", "voice_prompt": "VOICE-ONLY prompt",
        },
        headers=_auth(admin_token),
    ).json()
    await CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=admin.id, level=Level.INTERMEDIATE, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[created["id"]])],
    ).insert()
    token, _ = await _login_with_user(client, Role.STUDENT, email="s@example.com", created_by_id=admin.id)

    _, voice_meta = _dispatch(_start(client, token, created["id"]).json())
    _, avatar_meta = _dispatch(_start(client, token, created["id"], mode="topic_discussion").json())

    assert "VOICE-ONLY prompt" in voice_meta["lesson_prompt"]
    assert "AVATAR-ONLY" not in json.dumps(voice_meta)
    assert "AVATAR-ONLY prompt" in avatar_meta["topic_instructions"]
    assert "VOICE-ONLY" not in json.dumps(avatar_meta)


async def test_editing_a_lesson_keeps_prompt_settings_and_switching_back_to_shared_ignores_stale_voice_text(client):
    admin_token, _ = await _admin(client)
    topic_id = client.post(
        "/api/v1/topics",
        json={"title": "Music", "level": "intermediate", "avatar_prompt": "A", "voice_prompt_mode": "separate", "voice_prompt": "V"},
        headers=_auth(admin_token),
    ).json()["id"]

    # Editing unrelated fields (and the avatar prompt) leaves the voice settings alone.
    patched = client.patch(f"/api/v1/topics/{topic_id}", json={"title": "Music 2", "avatar_prompt": "A2"}, headers=_auth(admin_token)).json()
    assert (patched["voice_prompt_mode"], patched["voice_prompt"], patched["avatar_prompt"]) == ("separate", "V", "A2")
    assert (await TopicContent.get(topic_id)).voice_practice_prompt == "V"

    # Switching to shared: the hidden voice text is kept but never used.
    patched = client.patch(f"/api/v1/topics/{topic_id}", json={"voice_prompt_mode": "shared"}, headers=_auth(admin_token)).json()
    assert patched["voice_prompt_mode"] == "shared"
    assert (await TopicContent.get(topic_id)).voice_practice_prompt == "A2"

    # And back to separate restores the teacher's earlier voice prompt.
    client.patch(f"/api/v1/topics/{topic_id}", json={"voice_prompt_mode": "separate"}, headers=_auth(admin_token))
    assert (await TopicContent.get(topic_id)).voice_practice_prompt == "V"


async def test_separate_mode_with_a_blank_voice_prompt_falls_back_to_the_avatar_prompt():
    topic = TopicContent(title="T", level=Level.BEGINNER, avatar_prompt="A", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="   ")
    assert topic.voice_practice_prompt == "A"


async def test_invalid_prompt_mode_and_oversized_voice_prompt_are_rejected(client):
    admin_token, _ = await _admin(client)
    bad_mode = client.post("/api/v1/topics", json={"title": "T", "level": "beginner", "voice_prompt_mode": "both"}, headers=_auth(admin_token))
    too_long = client.post("/api/v1/topics", json={"title": "T", "level": "beginner", "voice_prompt": "x" * 10_001}, headers=_auth(admin_token))
    assert bad_mode.status_code == 422
    assert too_long.status_code == 422


async def test_a_student_never_receives_the_voice_prompt(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson(avatar_prompt="SECRET-A", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="SECRET-V")
    body = client.get(f"/api/v1/topics/{topic.id}", headers=_auth(token)).json()
    assert body["voice_prompt"] is None and body["voice_prompt_mode"] is None
    assert "SECRET" not in json.dumps(body)


# --- dispatch ---------------------------------------------------------------------


async def test_voice_start_dispatches_only_the_voice_agent_with_the_saved_lesson(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson(
        title="Talking about Music", avatar_prompt="avatar", voice_prompt_mode=VoicePromptMode.SEPARATE,
        voice_prompt="voice prompt", opening_line="Hi there!",
    )
    resp = _start(client, token, topic.id)
    assert resp.status_code == 200, resp.text
    started = resp.json()

    agent_name, metadata = _dispatch(started)
    assert agent_name == "tamakkan-voice-dev"
    assert metadata["project"] == "tamakkan"
    assert metadata == {
        "project": "tamakkan",
        "session_id": started["session_id"],
        "topic_id": topic.id,
        "max_duration_minutes": 15,
        "lesson_title": "Talking about Music",
        # Wrapped in an explicit "lesson reference material" boundary (see
        # app.core.prompt_safety) rather than sent as the raw string.
        "lesson_prompt": wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "voice prompt"),
        "opening_line": "Hi there!",
        "level": "intermediate",
        "arabic_support_enabled": None,
        "arabic_intervention_level": "low",
    }
    claims = _claims(started)
    assert claims["sub"] == f"student-{student.id}"
    assert claims["video"]["room"] == started["room_name"] and claims["video"]["roomJoin"] is True
    assert started["room_name"] == f"voice-{started['session_id']}"
    assert started["server_url"] == "wss://fake.livekit.cloud"

    stored = await LiveSession.get(started["session_id"])
    assert stored.mode == PracticeMode.VOICE_PRACTICE
    assert stored.state == SessionState.CONNECTING
    assert stored.livekit_room_name == started["room_name"]
    assert stored.student_id == student.id and stored.topic_id == topic.id


async def test_avatar_dispatch_is_unchanged(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson(avatar_prompt="avatar", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="voice", opening_line="Hi")
    for body_mode in (None, "topic_discussion"):
        started = _start(client, token, topic.id, mode=body_mode).json()
        agent_name, metadata = _dispatch(started)
        assert agent_name == "tamakkan-mariam-dev"
        assert set(metadata) == {
            "session_id", "topic_id", "max_duration_minutes", "topic_instructions", "opening_line", "level",
            "arabic_support_enabled", "arabic_intervention_level",
        }
        assert metadata["topic_instructions"] == wrap_untrusted_lesson_content("TOPIC INSTRUCTIONS", "avatar")
        # Unset on this fixture topic (predates the field) - the Avatar agent
        # treats `None` the same as `False` (see `MariamAgent`), unlike Voice
        # Practice's own legacy-default-on behavior.
        assert metadata["arabic_support_enabled"] is None
        assert metadata["arabic_intervention_level"] == "low"
        assert started["room_name"] == f"mariam-{started['session_id']}"
        assert (await LiveSession.get(started["session_id"])).mode == PracticeMode.TOPIC_DISCUSSION


async def test_voice_agent_name_can_be_overridden_but_never_blank(client, monkeypatch):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson()
    monkeypatch.setenv("LIVEKIT_VOICE_AGENT_NAME", "tamakkan-voice-staging")
    get_settings.cache_clear()
    assert _dispatch(_start(client, token, topic.id).json())[0] == "tamakkan-voice-staging"

    monkeypatch.setenv("LIVEKIT_VOICE_AGENT_NAME", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="LIVEKIT_VOICE_AGENT_NAME"):
        get_settings()


async def test_voice_does_not_need_the_mariam_callback_key_but_does_need_livekit(client, monkeypatch):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson()
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    assert _start(client, token, topic.id).status_code == 200
    assert _start(client, token, topic.id, mode="topic_discussion").status_code == 503

    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    get_settings.cache_clear()
    before = await LiveSession.find_all().count()
    assert _start(client, token, topic.id).status_code == 503
    assert await LiveSession.find_all().count() == before


async def test_client_cannot_supply_prompt_or_level(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _lesson(avatar_prompt="saved prompt")
    resp = client.post(
        "/api/v1/sessions/start",
        json={"topic_id": topic.id, "mode": "voice_practice", "lesson_prompt": "EVIL", "level": "advanced"},
        headers=_auth(token),
    )
    _, metadata = _dispatch(resp.json())
    assert metadata["lesson_prompt"] == wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "saved prompt")
    assert metadata["level"] == "intermediate"
    assert _start(client, token, topic.id, mode="something_else").status_code == 422


# --- entitlement and isolation -----------------------------------------------------


async def test_voice_start_uses_the_same_entitlement_gate(client):
    token, _ = await _login_with_user(client, Role.STUDENT, assigned_level=Level.BEGINNER)
    other_level = await _lesson(level=Level.ADVANCED)
    resp = _start(client, token, other_level.id)
    assert resp.status_code == 403
    assert await LiveSession.find_all().to_list() == []


async def test_mixed_level_students_in_one_class_each_get_only_their_own_lesson(client):
    beginner_token, beginner = await _login_with_user(client, Role.STUDENT, email="b@example.com", assigned_level=Level.BEGINNER)
    advanced_token, advanced = await _login_with_user(client, Role.STUDENT, email="a@example.com", assigned_level=Level.ADVANCED)
    beginner_lesson = await _lesson(level=Level.BEGINNER, avatar_prompt="BEGINNER prompt")
    advanced_lesson = await _lesson(level=Level.ADVANCED, avatar_prompt="ADVANCED prompt")

    _, b_meta = _dispatch(_start(client, beginner_token, beginner_lesson.id).json())
    _, a_meta = _dispatch(_start(client, advanced_token, advanced_lesson.id).json())
    assert b_meta["lesson_prompt"] == wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "BEGINNER prompt")
    assert b_meta["level"] == "beginner"
    assert a_meta["lesson_prompt"] == wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "ADVANCED prompt")
    assert a_meta["level"] == "advanced"
    # Neither can open the other level's lesson.
    assert _start(client, beginner_token, advanced_lesson.id).status_code == 403
    assert _start(client, advanced_token, beginner_lesson.id).status_code == 403


async def test_two_students_start_different_voice_lessons_concurrently_and_stay_isolated(client, livekit):
    """Mandatory isolation test 1 + 5: distinct session/room/identity per
    student, each token bound to its own room, each dispatch carrying only its
    own lesson, and one student's end/transcript never touching the other."""
    t1, s1 = await _login_with_user(client, Role.STUDENT, email="one@example.com")
    t2, s2 = await _login_with_user(client, Role.STUDENT, email="two@example.com")
    lesson1 = TopicContent(title="Music", level=Level.INTERMEDIATE, avatar_prompt="PROMPT-ONE")
    lesson2 = TopicContent(title="Travel", level=Level.INTERMEDIATE, avatar_prompt="PROMPT-TWO")
    await lesson1.insert()
    await lesson2.insert()
    # Both on one day of the class calendar.
    await CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[lesson1.id, lesson2.id])],
    ).insert()
    started1 = _start(client, t1, lesson1.id).json()
    started2 = _start(client, t2, lesson2.id).json()

    assert started1["session_id"] != started2["session_id"]
    assert started1["room_name"] != started2["room_name"]
    c1, c2 = _claims(started1), _claims(started2)
    assert (c1["sub"], c1["video"]["room"]) == (f"student-{s1.id}", started1["room_name"])
    assert (c2["sub"], c2["video"]["room"]) == (f"student-{s2.id}", started2["room_name"])
    _, m1 = _dispatch(started1)
    _, m2 = _dispatch(started2)
    assert m1["session_id"] == started1["session_id"]
    assert m1["lesson_prompt"] == wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "PROMPT-ONE")
    assert m1["lesson_title"] == "Music"
    assert m2["session_id"] == started2["session_id"]
    assert m2["lesson_prompt"] == wrap_untrusted_lesson_content("TEACHER LESSON INSTRUCTIONS", "PROMPT-TWO")
    assert m2["lesson_title"] == "Travel"

    livekit.agent_joins(started1["room_name"])
    livekit.agent_joins(started2["room_name"])
    assert client.post(f"/api/v1/sessions/{started1['session_id']}/connected", headers=_auth(t1)).status_code == 200
    assert client.post(f"/api/v1/sessions/{started2['session_id']}/connected", headers=_auth(t2)).status_code == 200

    # Student 1 cannot end, confirm or read student 2's session.
    assert _end(client, t1, started2["session_id"], transcript=BROWSER_TRANSCRIPT).status_code == 403
    assert client.post(f"/api/v1/sessions/{started2['session_id']}/connected", headers=_auth(t1)).status_code == 403
    assert client.get(f"/api/v1/sessions/{started2['session_id']}", headers=_auth(t1)).status_code == 403

    _end(client, t1, started1["session_id"], transcript=BROWSER_TRANSCRIPT)
    one = await LiveSession.get(started1["session_id"])
    two = await LiveSession.get(started2["session_id"])
    assert one.state == SessionState.ENDED and len(one.browser_transcript) == 2
    assert two.state == SessionState.CONNECTED and two.browser_transcript == []
    assert livekit.deleted_rooms == [started1["room_name"]]


async def test_avatar_and_voice_used_simultaneously_by_two_students_stay_isolated(client, livekit):
    """Mandatory isolation test 2: Mariam's internal callbacks only drive
    Mariam sessions; a voice session's end never touches a Mariam one."""
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    avatar_token, _ = await _login_with_user(client, Role.STUDENT, email="avatar@example.com")
    voice_token, _ = await _login_with_user(client, Role.STUDENT, email="voice@example.com")
    lesson = await _lesson(avatar_prompt="A", voice_prompt_mode=VoicePromptMode.SEPARATE, voice_prompt="V")

    avatar = _start(client, avatar_token, lesson.id, mode="topic_discussion").json()
    voice = _start(client, voice_token, lesson.id).json()
    assert _dispatch(avatar)[0] == "tamakkan-mariam-dev" and _dispatch(voice)[0] == "tamakkan-voice-dev"
    assert avatar["room_name"].startswith("mariam-") and voice["room_name"].startswith("voice-")

    internal = {"X-Internal-API-Key": FAKE_INTERNAL_API_KEY}
    client.post(f"/api/v1/sessions/{avatar['session_id']}/mark-connected", headers=internal)
    livekit.agent_joins(voice["room_name"])
    client.post(f"/api/v1/sessions/{voice['session_id']}/connected", headers=_auth(voice_token))

    # The voice student ends; the Mariam session is untouched.
    _end(client, voice_token, voice["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER)
    assert (await LiveSession.get(avatar["session_id"])).state == SessionState.CONNECTED
    assert avatar["room_name"] not in livekit.deleted_rooms

    # Mariam's worker ends her own session: scored exactly once, only hers.
    client.post(
        f"/api/v1/sessions/{avatar['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers=internal,
    )
    assert len(scorer.calls) == 1
    assert (await LiveSession.get(avatar["session_id"])).report is not None
    voice_after = await LiveSession.get(voice["session_id"])
    assert voice_after.report is None and voice_after.transcript_history == []


async def test_mariam_internal_callbacks_cannot_touch_a_voice_session(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    voice = _start(client, token, (await _lesson()).id).json()
    internal = {"X-Internal-API-Key": FAKE_INTERNAL_API_KEY}

    client.post(f"/api/v1/sessions/{voice['session_id']}/mark-connected", headers=internal)
    assert (await LiveSession.get(voice["session_id"])).state == SessionState.CONNECTING
    client.post(
        f"/api/v1/sessions/{voice['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers=internal,
    )
    stored = await LiveSession.get(voice["session_id"])
    assert stored.state == SessionState.CONNECTING
    assert stored.transcript_history == [] and stored.report is None
    assert scorer.calls == []


async def test_same_student_rapidly_switching_modes_never_leaves_two_voice_rooms_open(client, livekit):
    """Mandatory isolation test 3 (+ two tabs / refresh): every start gets a
    new session and room; starting Voice again closes the student's earlier
    open voice session and releases its room; Mariam sessions are left to
    Mariam's own worker."""
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()

    avatar1 = _start(client, token, lesson.id, mode="topic_discussion").json()
    voice1 = _start(client, token, lesson.id).json()
    avatar2 = _start(client, token, lesson.id, mode="topic_discussion").json()
    livekit.agent_joins(voice1["room_name"])
    client.post(f"/api/v1/sessions/{voice1['session_id']}/connected", headers=_auth(token))
    voice2 = _start(client, token, lesson.id).json()

    rooms = [s["room_name"] for s in (avatar1, voice1, avatar2, voice2)]
    assert len(set(rooms)) == 4
    assert len({s["session_id"] for s in (avatar1, voice1, avatar2, voice2)}) == 4

    first_voice = await LiveSession.get(voice1["session_id"])
    assert first_voice.state == SessionState.ENDED and first_voice.end_reason == EndReason.USER_ENDED
    assert voice1["room_name"] in livekit.deleted_rooms
    assert (await LiveSession.get(voice2["session_id"])).state == SessionState.CONNECTING
    for avatar in (avatar1, avatar2):
        assert (await LiveSession.get(avatar["session_id"])).state == SessionState.CONNECTING
        assert avatar["room_name"] not in livekit.deleted_rooms


async def test_superseding_one_students_voice_session_never_touches_another_students(client, livekit):
    t1, _ = await _login_with_user(client, Role.STUDENT, email="one@example.com")
    t2, _ = await _login_with_user(client, Role.STUDENT, email="two@example.com")
    lesson = await _lesson()
    other = _start(client, t2, lesson.id).json()
    _start(client, t1, lesson.id)
    _start(client, t1, lesson.id)
    assert (await LiveSession.get(other["session_id"])).state == SessionState.CONNECTING
    assert other["room_name"] not in livekit.deleted_rooms


# --- lifecycle ------------------------------------------------------------------------


async def test_connected_is_verified_against_livekit_participants(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    url = f"/api/v1/sessions/{started['session_id']}/connected"

    livekit.participants[started["room_name"]] = [STANDARD_KIND]  # only the student is there
    assert client.post(url, headers=_auth(token)).status_code == 409
    livekit.agent_joins(started["room_name"])
    assert client.post(url, headers=_auth(token)).json()["state"] == "connected"
    assert client.post(url, headers=_auth(token)).json()["state"] == "connected"  # idempotent


async def test_an_agent_in_a_different_room_does_not_count(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    mine = _start(client, token, lesson.id).json()
    livekit.agent_joins("voice-someone-elses-room")
    assert client.post(f"/api/v1/sessions/{mine['session_id']}/connected", headers=_auth(token)).status_code == 409


async def test_student_routes_refuse_avatar_sessions(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    avatar = _start(client, token, (await _lesson()).id, mode="topic_discussion").json()
    assert client.post(f"/api/v1/sessions/{avatar['session_id']}/connected", headers=_auth(token)).status_code == 422
    assert _end(client, token, avatar["session_id"]).status_code == 422
    assert (await LiveSession.get(avatar["session_id"])).state == SessionState.CONNECTING


async def test_end_is_idempotent_and_keeps_the_first_outcome(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))

    first = _end(client, token, started["session_id"], transcript=BROWSER_TRANSCRIPT)
    second = _end(client, token, started["session_id"], reason="connection_failure", transcript=[BROWSER_TRANSCRIPT[0]])
    assert first.status_code == second.status_code == 200
    assert second.json()["state"] == "ended" and second.json()["end_reason"] == "user_ended"
    assert len(second.json()["browser_transcript"]) == 2
    assert first.json()["ended_at"] == second.json()["ended_at"]
    assert livekit.deleted_rooms == [started["room_name"]] * 2  # room release retried, harmlessly


async def test_a_session_never_verified_connected_ends_as_connection_failed(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    body = _end(client, token, started["session_id"], reason="user_ended").json()
    assert (body["state"], body["end_reason"]) == ("connection_failed", "connection_failure")
    assert body["ended_at"] is not None
    assert started["room_name"] in livekit.deleted_rooms


@pytest.mark.parametrize("reason,state", [("agent_ended", "ended"), ("max_duration_reached", "ended"), ("connection_failure", "connection_failed")])
async def test_disconnect_reasons_are_recorded(client, livekit, reason, state):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))
    body = _end(client, token, started["session_id"], reason=reason).json()
    assert body["state"] == state


@pytest.mark.parametrize("reason", ["administrator_terminated", "idle_timeout", "bogus"])
async def test_students_cannot_report_privileged_end_reasons(client, reason):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    assert _end(client, token, started["session_id"], reason=reason).status_code == 422
    assert (await LiveSession.get(started["session_id"])).state == SessionState.CONNECTING


async def test_end_still_records_the_outcome_when_livekit_is_unreachable(client, livekit):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))
    livekit.fail_deletes = True
    assert _end(client, token, started["session_id"]).json()["state"] == "ended"


async def test_admin_force_end_works_for_voice_sessions(client, livekit):
    admin_token, admin = await _admin(client)
    token, _ = await _login_with_user(client, Role.STUDENT, email="s@example.com", created_by_id=admin.id)
    started = _start(client, token, (await _lesson(admin_id=admin.id)).id).json()
    body = client.post(f"/api/v1/sessions/{started['session_id']}/force-end", headers=_auth(admin_token)).json()
    assert (body["state"], body["end_reason"], body["mode"]) == ("ended", "administrator_terminated", "voice_practice")


# --- transcript trust boundary -------------------------------------------------------------

SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER = [
    {**turn, "speaker": "agent" if turn["speaker"] == "mariam" else "student"} for turn in SUFFICIENT_EVIDENCE_TRANSCRIPT
]


async def test_browser_transcript_is_stored_as_unverified_and_never_scored(client, livekit):
    scorer = FakeSessionScorer()
    _override_session_scorer(scorer)
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    livekit.agent_joins(started["room_name"])
    client.post(f"/api/v1/sessions/{started['session_id']}/connected", headers=_auth(token))

    body = _end(client, token, started["session_id"], transcript=SUFFICIENT_EVIDENCE_TRANSCRIPT_BROWSER).json()

    assert body["transcript_history"] == []  # the trusted slot stays empty
    assert [t["speaker"] for t in body["browser_transcript"]][:2] == ["agent", "student"]
    assert body["report"] is None
    assert scorer.calls == []
    # Even an explicit scoring pass refuses a voice session.
    from app.services.live_session_service import get_live_session_service
    from app.core.application import app

    await app.dependency_overrides[get_live_session_service]().generate_report(started["session_id"])
    assert scorer.calls == [] and (await LiveSession.get(started["session_id"])).report is None


async def test_student_cannot_write_the_trusted_transcript_or_use_internal_routes(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    forged = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers=_auth(token),
    )
    assert forged.status_code == 401
    # Extra fields on the student end route are ignored, never stored as trusted.
    client.post(
        f"/api/v1/sessions/{started['session_id']}/end",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers=_auth(token),
    )
    assert (await LiveSession.get(started["session_id"])).transcript_history == []


@pytest.mark.parametrize(
    "transcript",
    [
        [{"speaker": "mariam", "text": "hi", "timestamp": "2026-01-01T00:00:00"}],
        [{"speaker": "student", "text": "", "timestamp": "2026-01-01T00:00:00"}],
        [{"speaker": "student", "text": "x" * 1001, "timestamp": "2026-01-01T00:00:00"}],
        [{"speaker": "student", "text": "hi", "timestamp": "2026-01-01T00:00:00"}] * 601,
    ],
)
async def test_browser_transcript_is_validated(client, transcript):
    token, _ = await _login_with_user(client, Role.STUDENT)
    started = _start(client, token, (await _lesson()).id).json()
    assert _end(client, token, started["session_id"], transcript=transcript).status_code == 422


async def test_voice_sessions_are_listed_with_their_mode(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    lesson = await _lesson()
    _start(client, token, lesson.id, mode="topic_discussion")
    _start(client, token, lesson.id)
    modes = sorted(row["mode"] for row in client.get("/api/v1/sessions/me", headers=_auth(token)).json())
    assert modes == ["topic_discussion", "voice_practice"]
