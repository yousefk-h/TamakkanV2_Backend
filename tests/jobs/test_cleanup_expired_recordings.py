from datetime import timedelta

from app.core.security import hash_password
from app.jobs.cleanup_expired_recordings import _cleanup
from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.models.topic_content import Level, TopicContent
from app.models.user import Role, User
from app.repositories.recording_submission_repository import RecordingSubmissionRepository
from app.utils.time import utcnow


class FakeAudioStorage:
    def __init__(self):
        self.deleted: list[str] = []

    async def upload(self, file_bytes, public_id):
        raise NotImplementedError("cleanup never uploads")

    async def delete(self, public_id: str) -> None:
        self.deleted.append(public_id)


async def _make_submission(expires_at, media_deleted=False) -> RecordingSubmission:
    student = await User(
        email=f"s-{expires_at.timestamp()}@example.com",
        password_hash=hash_password("x"),
        full_name="Student",
        role=Role.STUDENT,
    ).insert()
    topic = await TopicContent(title="Topic", level=Level.BEGINNER).insert()
    submission = RecordingSubmission(
        student=student,
        student_id=student.id,
        kind=SubmissionKind.AUDIO_TASK,
        topic=topic,
        topic_id=topic.id,
        label="Topic",
        media_url="https://cdn.example/x.mp3",
        cloudinary_public_id=f"submissions/{student.id}",
        duration_seconds=30.0,
        expires_at=expires_at,
        media_deleted=media_deleted,
        reviewed=True,  # proves metadata survives cleanup
    )
    await submission.insert()
    return submission


async def test_cleanup_deletes_media_for_expired_submissions_and_keeps_metadata(monkeypatch):
    expired = await _make_submission(expires_at=utcnow() - timedelta(days=1))
    fake = FakeAudioStorage()
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.get_audio_storage", lambda: fake)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.init_db", _noop)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.close_db", _noop)

    await _cleanup()

    assert fake.deleted == [expired.cloudinary_public_id]
    refreshed = await RecordingSubmission.get(expired.id)
    assert refreshed.media_deleted is True
    assert refreshed.reviewed is True  # metadata untouched
    assert refreshed.label == "Topic"


async def test_cleanup_skips_submissions_not_yet_expired(monkeypatch):
    not_expired = await _make_submission(expires_at=utcnow() + timedelta(days=1))
    fake = FakeAudioStorage()
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.get_audio_storage", lambda: fake)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.init_db", _noop)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.close_db", _noop)

    await _cleanup()

    assert fake.deleted == []
    refreshed = await RecordingSubmission.get(not_expired.id)
    assert refreshed.media_deleted is False


async def test_cleanup_skips_already_deleted_submissions(monkeypatch):
    await _make_submission(expires_at=utcnow() - timedelta(days=1), media_deleted=True)
    fake = FakeAudioStorage()
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.get_audio_storage", lambda: fake)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.init_db", _noop)
    monkeypatch.setattr("app.jobs.cleanup_expired_recordings.close_db", _noop)

    await _cleanup()

    assert fake.deleted == []


async def _noop() -> None:
    """`_cleanup()` calls `init_db()`/`close_db()` to manage its own
    connection when run as a real script - tests already have one open via
    the `mock_db` fixture, so both are patched out to a no-op here.
    """
