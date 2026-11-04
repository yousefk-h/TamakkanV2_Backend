"""Phase 4: real "Discuss with Mariam" sessions. `POST /sessions/start` only
mints a LiveKit token (a local, pure JWT signature - no network call), so
these tests need fake-but-present LiveKit credentials, not a real LiveKit
server. `force_end` does make one real LiveKit API call (delete the room) -
that call is monkeypatched out.
"""

import json

import pytest

from app.core.application import app
from app.core.config import get_settings
from app.core.security import hash_password
from app.core.session_scoring import SessionScoringError, TamakkanSessionScore
from app.models.learning_analysis import AssessmentFeedback, PracticeGoal
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import EndReason, LiveSession, SessionState
from app.models.topic_content import Level, TopicContent
from app.models.user import PlanType, Role, User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.services.live_session_service import LiveSessionService, get_live_session_service
from app.services.progress_service import get_progress_service
from app.utils.time import utcnow


FAKE_INTERNAL_API_KEY = "fake-internal-agent-key"
# Used as `created_by_id`/`owner_admin_id` for every student/topic pair a
# test doesn't specifically care about ownership for - only the handful of
# tests actually exercising cross-admin scoping pass a real admin's id in
# instead (both to `_login_with_user`'s `created_by_id` and to `_make_topic`'s
# `owner_admin_id`, which must agree for the student's calendar to resolve).
DEFAULT_ADMIN_ID = "default-test-admin"
# >=3 student turns, >=30 words, >=10 distinct words - `has_enough_evidence`'s
# bar for `generate_report` to actually call the scorer at all, rather than
# short-circuiting straight to `insufficient_evidence` with null scores. A
# single "Hello" turn (used elsewhere to test that exact short-circuit) never
# meets this bar.
SUFFICIENT_EVIDENCE_TRANSCRIPT = [
    {"speaker": "mariam", "text": "Hi! Ready to practice?", "timestamp": "2026-01-01T00:00:00"},
    {"speaker": "student", "text": "Yes, I would like to talk about music today because I really enjoy listening to different songs.", "timestamp": "2026-01-01T00:00:05"},
    {"speaker": "mariam", "text": "Great, what kind of music do you like?", "timestamp": "2026-01-01T00:00:10"},
    {"speaker": "student", "text": "I like pop music and sometimes classical music when I want to relax after a long day.", "timestamp": "2026-01-01T00:00:15"},
    {"speaker": "mariam", "text": "That sounds nice, do you play any instrument?", "timestamp": "2026-01-01T00:00:20"},
    {"speaker": "student", "text": "No, I do not play an instrument, but I would love to learn guitar someday.", "timestamp": "2026-01-01T00:00:25"},
]


class FakeSessionScorer:
    """No real Gemini credentials in tests - same shape as
    `tests/api/test_topics.py`'s `FakeContentDrafter`. `feedback`'s lists are
    all empty (only `next_steps` requires >=1 entry) so `validate_feedback`
    has nothing to cross-check against a specific test's transcript text -
    the fake's score content doesn't need to stay in sync with whatever
    transcript a given test writes, only the transcript's evidence volume
    (`has_enough_evidence`) does.
    """

    def __init__(self, score=None, error=None):
        self.score_result = score if score is not None else TamakkanSessionScore(
            fluency_score=70, vocabulary_score=65, grammar_score=80, complexity_score=60, overall_score=72,
            summary="Solid effort with a few grammar slips to work on.",
            feedback=AssessmentFeedback(
                status="assessed", strengths=[], improvement_areas=[], corrections=[], recurring_patterns=[],
                next_steps=[PracticeGoal(focus="Fluency", action="Practice speaking for five minutes daily.",
                                          success_criterion="Complete three practice sessions this week.", minutes=5)],
            ),
        )
        self.error = error
        self.calls: list[str] = []

    async def score(self, transcript: str) -> TamakkanSessionScore:
        self.calls.append(transcript)
        if self.error is not None:
            raise self.error
        return self.score_result


def _override_session_scorer(scorer: FakeSessionScorer):
    app.dependency_overrides[get_live_session_service] = lambda: LiveSessionService(
        LiveSessionRepository(), TopicContentRepository(), UserRepository(), AuditLogRepository(), scorer, get_progress_service()
    )


@pytest.fixture(autouse=True)
def _clear_session_service_override():
    yield
    app.dependency_overrides.pop(get_live_session_service, None)


@pytest.fixture(autouse=True)
def _fake_livekit_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake-secret-thats-long-enough")
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", FAKE_INTERNAL_API_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_room_delete(monkeypatch):
    """`force_end` calls the real LiveKit server API to delete a room - stub
    it so tests never make a network call."""

    class _FakeRoomService:
        def __init__(self):
            self.deleted_rooms: list[str] = []

        async def delete_room(self, request):
            self.deleted_rooms.append(request.room)

    class _FakeLiveKitAPI:
        def __init__(self, *args, **kwargs):
            self.room = _FakeRoomService()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import app.services.live_session_service as service_module

    monkeypatch.setattr(service_module.livekit_api, "LiveKitAPI", _FakeLiveKitAPI)
    yield


async def _login_with_user(
    client, role: Role, email: str = "test@example.com", password: str = "correct-password", **extra
) -> tuple[str, User]:
    if role == Role.STUDENT:
        extra.setdefault("plan", PlanType.STANDARD)
        extra.setdefault("assigned_level", Level.INTERMEDIATE)
        extra.setdefault("created_by_id", DEFAULT_ADMIN_ID)
        # "Today" needs to actually be reached for a session-start test to
        # exercise anything past the lock check - see `_make_topic`, which
        # schedules its topic on day 1 of the same admin's active template.
        extra.setdefault("program_start_date", utcnow())
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def _make_topic(level: Level = Level.INTERMEDIATE, owner_admin_id: str = DEFAULT_ADMIN_ID) -> TopicContent:
    """Also creates and activates a one-day Standard calendar template for
    `owner_admin_id`/`level` scheduling this topic on day 1 - a session-start
    test needs the topic to actually be unlocked (entitled AND its day
    reached), not just entitled the old level-only way. Pass the same id used
    for a student's `created_by_id` when a test cares about which admin owns
    the calendar (cross-admin scoping tests); everything else can rely on the
    shared `DEFAULT_ADMIN_ID` both default to.
    """
    topic = TopicContent(title="Talking about Music", level=level)
    await topic.insert()
    template = CalendarTemplate(
        name="T",
        kind=TemplateKind.STANDARD,
        owner_admin_id=owner_admin_id,
        level=level,
        is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    )
    await template.insert()
    return topic


async def test_student_starts_a_session_for_an_entitled_topic(client):
    token, student = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()

    resp = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["server_url"] == "wss://fake.livekit.cloud"
    assert body["room_name"].startswith("mariam-")
    assert len(body["participant_token"]) > 20
    # The one topic scheduled that day gets the student's whole 15-minute
    # daily budget - see `test_session_duration_splits_across_a_multi_course_day`
    # for the split case.
    assert body["max_duration_minutes"] == 15

    stored = await LiveSession.get(body["session_id"])
    assert stored.student_id == student.id
    assert stored.topic_id == topic.id
    assert stored.state == SessionState.CONNECTING
    assert stored.livekit_room_name == body["room_name"]


@pytest.mark.parametrize("setting", ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "AGENT_INTERNAL_API_KEY"])
@pytest.mark.parametrize("value", ["", "   "])
async def test_missing_session_configuration_returns_503_without_creating_a_session(client, monkeypatch, setting, value):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    monkeypatch.setenv(setting, value)
    get_settings.cache_clear()

    response = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Live speaking sessions are not configured yet. Please contact your administrator."
    assert await LiveSession.find_all().to_list() == []


async def test_dispatch_metadata_and_unassessed_report_are_exposed_to_own_student(client):
    from jose import jwt
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}).json()
    claims = jwt.get_unverified_claims(started["participant_token"])
    metadata = json.loads(claims["roomConfig"]["agents"][0]["metadata"])
    assert metadata["level"] == topic.level.value
    headers = {"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    client.post(f"/api/v1/sessions/{started['session_id']}/mark-connected", headers=headers)
    fake = FakeSessionScorer()
    _override_session_scorer(fake)
    response = client.post(f"/api/v1/sessions/{started['session_id']}/mark-ended", headers=headers, json={
        "end_reason": "user_ended",
        "transcript_history": [{"speaker": "student", "text": "Hello", "timestamp": "2026-01-01T00:00:00"}],
    })
    assert response.status_code == 200
    assert not fake.calls
    result = client.get(f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert result["report"]["overall_score"] is None
    assert result["report"]["analysis"]["status"] == "insufficient_evidence"
    assert result["report"]["analysis"]["target_level"] == topic.level.value


async def test_session_duration_splits_across_a_multi_course_day(client):
    """`POST /sessions/start`'s `max_duration_minutes` is per-course, not the
    student's flat daily budget - see `ProgressService.get_topic_session_minutes`.
    """
    token, student = await _login_with_user(client, Role.STUDENT)
    topic_a = TopicContent(title="A", level=Level.INTERMEDIATE)
    topic_b = TopicContent(title="B", level=Level.INTERMEDIATE)
    await topic_a.insert()
    await topic_b.insert()
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.INTERMEDIATE, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic_a.id, topic_b.id])],
    )
    await template.insert()

    resp = client.post("/api/v1/sessions/start", json={"topic_id": topic_a.id}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["max_duration_minutes"] == 7  # 15 // 2


async def test_student_cannot_start_a_session_outside_their_plan(client):
    token, _ = await _login_with_user(client, Role.STUDENT, assigned_level=Level.BEGINNER)
    topic = await _make_topic(level=Level.ADVANCED)

    resp = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert await LiveSession.find_all().to_list() == []


async def test_unknown_topic_is_404(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    resp = client.post("/api/v1/sessions/start", json={"topic_id": "not-a-real-id"}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_non_student_cannot_start_a_session(client):
    token, _ = await _login_with_user(client, Role.TEACHER)
    topic = await _make_topic()
    resp = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_student_lists_only_their_own_sessions(client):
    token_a, student_a = await _login_with_user(client, Role.STUDENT, email="a@example.com")
    token_b, _ = await _login_with_user(client, Role.STUDENT, email="b@example.com")
    topic = await _make_topic()

    client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token_a}"})
    client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token_b}"})

    resp = client.get("/api/v1/sessions/me", headers={"Authorization": f"Bearer {token_a}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["student_id"] == student_a.id


async def test_owning_student_can_fetch_their_session_with_transcript(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.get(f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == started["session_id"]
    assert body["transcript_history"] == []


async def test_a_different_student_cannot_fetch_someone_elses_session(client):
    token_owner, _ = await _login_with_user(client, Role.STUDENT, email="owner@example.com")
    token_other, _ = await _login_with_user(client, Role.STUDENT, email="other@example.com")
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token_owner}"}
    ).json()

    resp = client.get(f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {token_other}"})
    assert resp.status_code == 403


async def test_owning_admin_can_fetch_their_students_session(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, email="student@example.com", created_by_id=admin.id)
    topic = await _make_topic(owner_admin_id=admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.get(f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200


async def test_a_different_admin_cannot_fetch_someone_elses_student_session(client):
    owning_admin_token, owning_admin = await _login_with_user(client, Role.TEACHER, email="owner@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, email="attacker@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, created_by_id=owning_admin.id)
    topic = await _make_topic(owner_admin_id=owning_admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.get(
        f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {other_admin_token}"}
    )
    assert resp.status_code == 403


async def test_unknown_session_id_is_404(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    resp = client.get("/api/v1/sessions/not-a-real-id", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_admin_list_is_scoped_to_their_own_students(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    _, other_admin = await _login_with_user(client, Role.TEACHER, email="other-admin@example.com")
    own_student_token, _ = await _login_with_user(
        client, Role.STUDENT, email="own-student@example.com", created_by_id=admin.id
    )
    other_student_token, _ = await _login_with_user(
        client, Role.STUDENT, email="other-student@example.com", created_by_id=other_admin.id
    )
    own_topic = await _make_topic(owner_admin_id=admin.id)
    other_topic = await _make_topic(owner_admin_id=other_admin.id)

    client.post("/api/v1/sessions/start", json={"topic_id": own_topic.id}, headers={"Authorization": f"Bearer {own_student_token}"})
    client.post("/api/v1/sessions/start", json={"topic_id": other_topic.id}, headers={"Authorization": f"Bearer {other_student_token}"})

    resp = client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1


async def test_non_admin_cannot_list_admin_sessions_view(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    resp = client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_admin_can_force_end_their_students_session(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, email="student@example.com", created_by_id=admin.id)
    topic = await _make_topic(owner_admin_id=admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/force-end", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ended"
    assert body["end_reason"] == "administrator_terminated"

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED
    assert stored.end_reason == EndReason.ADMIN_TERMINATED


async def test_force_ending_an_already_ended_session_is_rejected(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, email="student@example.com", created_by_id=admin.id)
    topic = await _make_topic(owner_admin_id=admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()
    client.post(f"/api/v1/sessions/{started['session_id']}/force-end", headers={"Authorization": f"Bearer {admin_token}"})

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/force-end", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 422


async def test_admin_cannot_force_end_another_admins_student_session(client):
    owning_admin_token, owning_admin = await _login_with_user(client, Role.TEACHER, email="owner@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, email="attacker@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, created_by_id=owning_admin.id)
    topic = await _make_topic(owner_admin_id=owning_admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/force-end", headers={"Authorization": f"Bearer {other_admin_token}"}
    )
    assert resp.status_code == 403


async def test_student_cannot_force_end_a_session(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.post(f"/api/v1/sessions/{started['session_id']}/force-end", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_start_session_embeds_topic_content_for_the_agent_worker(client):
    """The livekit_agent worker has no database of its own - it reads the
    topic's avatar prompt/opening line/level straight from the job dispatch
    metadata this service builds, not from a lookup of its own. The metadata
    lives inside the room-config claim of the minted (JWT) participant
    token, so decode that directly rather than mocking SDK internals."""
    from jose import jwt as jose_jwt

    token, _ = await _login_with_user(client, Role.STUDENT, assigned_level=Level.ADVANCED)
    topic = TopicContent(
        title="Talking about Music", level=Level.ADVANCED, avatar_prompt="Discuss favorite genres.", opening_line="Hi there!"
    )
    await topic.insert()
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=DEFAULT_ADMIN_ID, level=Level.ADVANCED, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    )
    await template.insert()

    resp = client.post("/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    claims = jose_jwt.decode(
        resp.json()["participant_token"],
        "fake-secret-thats-long-enough",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    dispatch_metadata = json.loads(claims["roomConfig"]["agents"][0]["metadata"])
    assert dispatch_metadata["topic_instructions"] == "Discuss favorite genres."
    assert dispatch_metadata["opening_line"] == "Hi there!"
    assert dispatch_metadata["level"] == "advanced"


async def test_agent_can_mark_a_session_connected_with_the_internal_key(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "connected"

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.CONNECTED


async def test_mark_connected_rejects_a_missing_or_wrong_internal_key(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.post(f"/api/v1/sessions/{started['session_id']}/mark-connected")
    assert resp.status_code == 401

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": "wrong-key"}
    )
    assert resp.status_code == 401


async def test_agent_can_mark_a_session_ended_with_a_reason(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "idle_timeout"},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ended"
    assert body["end_reason"] == "idle_timeout"

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.ENDED
    assert stored.end_reason == EndReason.IDLE_TIMEOUT
    assert stored.ended_at is not None


async def test_mark_ended_with_connection_failure_lands_in_connection_failed(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "connection_failure"},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "connection_failed"


async def test_a_live_session_can_still_fail(client):
    """A call that reached `connected` and then fails (the agent worker's
    abnormal-exit finalizer reports `connection_failure`) must transition to
    `connection_failed`, not stay stranded in `connected`."""
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "connection_failure"},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "connection_failed"

    stored = await LiveSession.get(started["session_id"])
    assert stored.state == SessionState.CONNECTION_FAILED
    assert stored.end_reason == EndReason.CONNECTION_FAILURE


async def test_mark_ended_stores_the_batched_transcript_and_generates_a_report(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    fake = FakeSessionScorer()
    _override_session_scorer(fake)
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    # `TestClient` runs `BackgroundTasks` synchronously within the request,
    # so the report has already landed by the time this response returns -
    # but the response body itself reflects `mark_ended`'s own synchronous
    # half (same "fetch again" caveat as the topics endpoints).
    assert len(body["transcript_history"]) == 6

    assert len(fake.calls) == 1
    sent = json.loads(fake.calls[0])
    assert len(sent["turns"]) == 6
    assert sent["turns"][1] == {"turn_index": 2, "speaker": "student", "text": "Yes, I would like to talk about music today because I really enjoy listening to different songs."}

    fetched = client.get(
        f"/api/v1/sessions/{started['session_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    # `overall_score` is the mean of the four skill scores, recomputed by the
    # service - never trusted verbatim off the model's own output (see
    # `TamakkanSessionScore.consistent_scores` and `generate_report`).
    assert fetched["report"]["overall_score"] == 69
    assert fetched["report"]["fluency_score"] == 70
    assert fetched["report"]["summary"] == "Solid effort with a few grammar slips to work on."
    assert fetched["report"]["reviewed"] is False
    assert fetched["report"]["analysis"]["status"] == "assessed"

    stored = await LiveSession.get(started["session_id"])
    assert len(stored.transcript_history) == 6
    assert stored.report is not None


async def test_mark_ended_with_no_transcript_never_calls_the_scorer(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    fake = FakeSessionScorer()
    _override_session_scorer(fake)
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "idle_timeout"},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200
    assert fake.calls == []

    stored = await LiveSession.get(started["session_id"])
    assert stored.report is None


async def test_mark_ended_with_connection_failure_never_calls_the_scorer_even_with_a_transcript(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    fake = FakeSessionScorer()
    _override_session_scorer(fake)
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={
            "end_reason": "connection_failure",
            "transcript_history": [{"speaker": "student", "text": "Hello", "timestamp": "2026-01-01T00:00:00"}],
        },
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )

    assert fake.calls == []
    stored = await LiveSession.get(started["session_id"])
    assert stored.report is None
    # The transcript is still stored even though the call failed - it's real
    # data, and a future manual look at it shouldn't require this to have
    # succeeded.
    assert len(stored.transcript_history) == 1


async def test_a_repeated_mark_ended_call_does_not_regenerate_the_report(client):
    """The `livekit_agent` worker's own finalizer and its abnormal-exit
    fallback can both call mark-ended for the same session - the second call
    must be a no-op, not a second Gemini call."""
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    fake = FakeSessionScorer()
    _override_session_scorer(fake)
    body = {"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT}
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended", json=body, headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended", json=body, headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    assert len(fake.calls) == 1


async def test_session_scoring_failure_leaves_no_report_without_raising(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    fake = FakeSessionScorer(error=SessionScoringError("gemini is down"))
    _override_session_scorer(fake)
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert resp.status_code == 200  # must not raise into a 500

    stored = await LiveSession.get(started["session_id"])
    assert stored.report is None


async def test_admin_can_mark_a_report_reviewed(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, email="student@example.com", created_by_id=admin.id)
    topic = await _make_topic(owner_admin_id=admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )

    _override_session_scorer(FakeSessionScorer())
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={
            "end_reason": "user_ended",
            "transcript_history": [{"speaker": "student", "text": "Hello", "timestamp": "2026-01-01T00:00:00"}],
        },
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )

    resp = client.patch(
        f"/api/v1/sessions/{started['session_id']}/report-reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["report"]["reviewed"] is True

    stored = await LiveSession.get(started["session_id"])
    assert stored.report.reviewed is True


async def test_reviewing_a_session_with_no_report_yet_is_unprocessable(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, email="admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, email="student@example.com", created_by_id=admin.id)
    topic = await _make_topic(owner_admin_id=admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.patch(
        f"/api/v1/sessions/{started['session_id']}/report-reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_a_different_admin_cannot_review_someone_elses_student_report(client):
    owning_admin_token, owning_admin = await _login_with_user(client, Role.TEACHER, email="owner@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, email="attacker@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, created_by_id=owning_admin.id)
    topic = await _make_topic(owner_admin_id=owning_admin.id)
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    resp = client.patch(
        f"/api/v1/sessions/{started['session_id']}/report-reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {other_admin_token}"},
    )
    assert resp.status_code == 403


async def test_student_cannot_review_a_report(client):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.patch(
        f"/api/v1/sessions/{started['session_id']}/report-reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# --- AI report generation: a missing Gemini key must be explicit, never silent ------------------------------------
# Incident: on the local stack the API ran with an empty GEMINI_API_KEY, so every session that had enough student speech
# ended normally but never got a report - and the only trace was a generic warning with no reason.


def _capture_logs():
    from loguru import logger

    records: list[tuple[str, str]] = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level="INFO")
    return records, lambda: logger.remove(sink)


async def _ended_session_with_transcript(client, scorer, transcript=None):
    token, _ = await _login_with_user(client, Role.STUDENT)
    topic = await _make_topic()
    started = client.post(
        "/api/v1/sessions/start", json={"topic_id": topic.id}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-connected", headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY}
    )
    _override_session_scorer(scorer)
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": SUFFICIENT_EVIDENCE_TRANSCRIPT if transcript is None else transcript},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    return started["session_id"], resp


async def test_a_missing_gemini_key_is_logged_as_an_error_and_the_session_stays_intact(client, monkeypatch):
    import types

    from app.core import session_scoring

    monkeypatch.setattr(session_scoring, "get_settings", lambda: types.SimpleNamespace(GEMINI_API_KEY=""))
    monkeypatch.setattr(session_scoring, "_get_agent", lambda: (_ for _ in ()).throw(AssertionError("the SDK must not even be built")))
    records, stop = _capture_logs()
    try:
        session_id, resp = await _ended_session_with_transcript(client, session_scoring.GeminiSessionScorer())
    finally:
        stop()

    assert resp.status_code == 200  # the agent's callback is never failed by a reporting problem
    stored = await LiveSession.get(session_id)
    assert stored.state == SessionState.ENDED and stored.end_reason == EndReason.USER_ENDED
    assert len(stored.transcript_history) == 6, "the transcript survives a failed report"
    assert stored.report is None
    errors = [message for level, message in records if level == "ERROR"]
    assert any("GEMINI_API_KEY" in message and session_id in message for message in errors), records


async def test_a_failed_assessment_logs_its_cause_class_but_never_the_error_text(client):
    error = SessionScoringError("provider said: the student wrote I like pop music")
    error.__cause__ = TimeoutError()
    records, stop = _capture_logs()
    try:
        session_id, resp = await _ended_session_with_transcript(client, FakeSessionScorer(error=error))
    finally:
        stop()

    assert resp.status_code == 200
    assert (await LiveSession.get(session_id)).report is None
    joined = " | ".join(message for _, message in records)
    assert "TimeoutError" in joined, "the failure's cause class is logged so it can be diagnosed"
    assert "provider said" not in joined and "pop music" not in joined, "no provider output or student speech is logged"


async def test_a_late_empty_transcript_never_overwrites_the_stored_one_or_rescored(client):
    fake = FakeSessionScorer()
    session_id, first = await _ended_session_with_transcript(client, fake)
    assert first.status_code == 200 and len(fake.calls) == 1

    again = client.post(
        f"/api/v1/sessions/{session_id}/mark-ended",
        json={"end_reason": "user_ended", "transcript_history": []},
        headers={"X-Internal-Api-Key": FAKE_INTERNAL_API_KEY},
    )
    assert again.status_code == 200
    stored = await LiveSession.get(session_id)
    assert len(stored.transcript_history) == 6 and stored.report is not None
    assert len(fake.calls) == 1, "a duplicate session-end never scores again"
