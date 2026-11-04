"""Every model constructs, persists, and reads back correctly - Kanban card
#1's literal Done-when. Uses the mocked DB from `tests/conftest.py`.
"""

from app.models.alert_log import AlertLog, AlertSeverity
from app.models.audit_log import AuditLog
from app.models.live_session import LiveSession, SessionState
from app.models.password_reset_request import PasswordResetRequest
from app.models.password_reset_token import PasswordResetToken
from app.models.platform_avatar_config import PlatformAvatarConfig
from app.models.refresh_token import RefreshToken
from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.models.topic_content import Level, TopicContent, TranscriptState
from app.models.user import Role, User
from app.utils.time import utcnow


async def _make_user() -> User:
    user = User(email="alex@example.com", password_hash="hashed", full_name="Alex Morgan")
    await user.insert()
    return user


async def test_user_roundtrip():
    user = await _make_user()
    fetched = await User.get(user.id)
    assert fetched.email == "alex@example.com"
    assert fetched.role == Role.STUDENT
    assert fetched.is_active is True


async def test_refresh_token_roundtrip():
    user = await _make_user()
    record = RefreshToken(user=user, user_id=user.id, token_hash="hash123", expires_at=utcnow())
    await record.insert()
    fetched = await RefreshToken.get(record.id)
    assert fetched.token_hash == "hash123"
    assert fetched.revoked is False
    linked_user = await fetched.user.fetch()
    assert linked_user.email == "alex@example.com"


async def test_password_reset_token_roundtrip():
    user = await _make_user()
    record = PasswordResetToken(user=user, user_id=user.id, token_hash="resethash", expires_at=utcnow())
    await record.insert()
    fetched = await PasswordResetToken.get(record.id)
    assert fetched.used is False
    assert (await fetched.user.fetch()).email == "alex@example.com"


async def test_topic_content_roundtrip():
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    await topic.insert()
    fetched = await TopicContent.get(topic.id)
    assert fetched.title == "Talking about Music"
    assert fetched.transcript_state == TranscriptState.QUEUED
    assert fetched.vocabulary == []


async def test_live_session_roundtrip():
    user = await _make_user()
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    await topic.insert()

    session = LiveSession(student=user, student_id=user.id, topic=topic, topic_id=topic.id)
    await session.insert()
    fetched = await LiveSession.get(session.id)
    assert fetched.state == SessionState.NOT_STARTED
    assert fetched.end_reason is None
    assert (await fetched.student.fetch()).email == "alex@example.com"


async def test_recording_submission_roundtrip():
    user = await _make_user()
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    await topic.insert()
    submission = RecordingSubmission(
        student=user,
        student_id=user.id,
        kind=SubmissionKind.AUDIO_TASK,
        topic=topic,
        topic_id=topic.id,
        label="Talking about Music",
        media_url="https://res.cloudinary.com/example/audio.mp3",
        cloudinary_public_id="submissions/abc123",
        duration_seconds=42.0,
    )
    await submission.insert()
    fetched = await RecordingSubmission.get(submission.id)
    assert fetched.reviewed is False
    assert fetched.media_deleted is False
    assert fetched.expires_at > fetched.submitted_at


async def test_audit_log_roundtrip():
    user = await _make_user()
    log = AuditLog(actor=user, action="force_end_session", target_type="LiveSession", target_id="abc123")
    await log.insert()
    fetched = await AuditLog.get(log.id)
    assert fetched.action == "force_end_session"
    assert (await fetched.actor.fetch()).email == "alex@example.com"


async def test_alert_log_roundtrip():
    alert = AlertLog(kind="avatar_startup_failure_burst", severity=AlertSeverity.CRITICAL, occurrences=5)
    await alert.insert()
    fetched = await AlertLog.get(alert.id)
    assert fetched.occurrences == 5
    assert fetched.acknowledged is False


async def test_platform_avatar_config_roundtrip():
    config = PlatformAvatarConfig()
    await config.insert()
    fetched = await PlatformAvatarConfig.get(config.id)
    assert fetched.max_concurrent_sessions == 50
    assert fetched.recording_enabled_by_default is False


async def test_password_reset_request_roundtrip():
    student = await _make_user()
    request = PasswordResetRequest(student=student, student_id=student.id)
    await request.insert()
    fetched = await PasswordResetRequest.get(request.id)
    assert fetched.resolved is False
    assert fetched.resolved_by is None
    assert (await fetched.student.fetch()).email == "alex@example.com"


async def test_every_model_gets_is_deleted_false_by_default():
    user = await _make_user()
    assert user.is_deleted is False


async def test_created_by_id_roundtrips():
    admin = User(email="admin@example.com", password_hash="hashed", full_name="Admin", role=Role.ADMINISTRATOR)
    await admin.insert()
    student = User(
        email="student@example.com",
        password_hash="hashed",
        full_name="Student",
        created_by_id=admin.id,
    )
    await student.insert()

    fetched = await User.get(student.id)
    assert fetched.created_by_id == admin.id
