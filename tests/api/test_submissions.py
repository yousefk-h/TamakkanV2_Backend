"""Student-submitted audio recordings (Kanban card #6, scoped to audio only).

Uses a `FakeAudioStorage` instead of hitting real Cloudinary - there are no
test credentials for it, and the point of `AudioStorage` being a Protocol is
exactly to make that unnecessary.
"""

import io

import pytest

from app.core.application import app
from app.core.security import hash_password
from app.core.storage import UploadFailedError, UploadResult
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.recording_submission import RecordingSubmission
from app.models.topic_content import Level, TopicContent
from app.models.user import PlanType, Role, User
from app.repositories.recording_submission_repository import RecordingSubmissionRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.services.progress_service import get_progress_service
from app.services.recording_submission_service import (
    RecordingSubmissionService,
    get_recording_submission_service,
)
from app.utils.time import utcnow

DEFAULT_ADMIN_ID = "default-test-admin"


class FakeAudioStorage:
    """Records every call it gets and returns a scripted result/error per
    upload, so tests can assert on both the outcome and what was attempted -
    including that a rejected upload's asset actually gets cleaned up.
    """

    def __init__(self, durations=None, fail_times: int = 0):
        self.durations = list(durations) if durations is not None else [30.0]
        self.fail_times = fail_times
        self.upload_calls: list[bytes] = []
        self.public_ids: list[str] = []
        self.deleted_public_ids: list[str] = []
        self._attempt = 0

    async def upload(self, file_bytes: bytes, public_id: str) -> UploadResult:
        self.upload_calls.append(file_bytes)
        self.public_ids.append(public_id)
        self._attempt += 1
        if self._attempt <= self.fail_times:
            raise UploadFailedError("simulated transient failure")
        duration = self.durations.pop(0) if self.durations else 30.0
        return UploadResult(public_id=public_id, secure_url=f"https://cdn.example/{public_id}.mp3", duration_seconds=duration)

    async def delete(self, public_id: str) -> None:
        self.deleted_public_ids.append(public_id)

    def signed_url(self, public_id: str) -> str:
        # Mirrors the real storage: assets use Cloudinary's `authenticated`
        # delivery type, so a playable URL is signed per response rather than
        # read off the stored document.
        return f"https://cdn.example/{public_id}.mp3?__cld_token__=signed"


def _override_storage(fake: FakeAudioStorage):
    app.dependency_overrides[get_recording_submission_service] = lambda: RecordingSubmissionService(
        RecordingSubmissionRepository(), TopicContentRepository(), fake, UserRepository(), get_progress_service()
    )


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_recording_submission_service, None)


async def _login(client, role: Role, email: str, password: str = "correct-password") -> str:
    token, _ = await _login_with_user(client, role, email, password)
    return token


async def _login_with_user(
    client, role: Role, email: str, password: str = "correct-password", **extra
) -> tuple[str, User]:
    # `POST /submissions` now applies the same plan rule as `GET /topics/{id}`,
    # so a Student with no plan can't submit anything. Default them onto a
    # Standard plan at `_make_topic`'s level; the tests that exercise the
    # entitlement gate itself pass `plan=`/`assigned_level=` explicitly.
    if role == Role.STUDENT:
        extra.setdefault("plan", PlanType.STANDARD)
        extra.setdefault("assigned_level", Level.INTERMEDIATE)
        extra.setdefault("created_by_id", DEFAULT_ADMIN_ID)
        # "Today" needs to actually be reached for a submission test to
        # exercise anything past the lock check - see `_make_topic`, which
        # schedules its topic on day 1 of the same admin's active template.
        extra.setdefault("program_start_date", utcnow())
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def _make_topic(level: Level = Level.INTERMEDIATE, owner_admin_id: str = DEFAULT_ADMIN_ID) -> TopicContent:
    """Also creates and activates a one-day Standard calendar template for
    `owner_admin_id`/`level` scheduling this topic on day 1 - a submission
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


def _post(client, token, topic_id, filename="rec.webm", content_type="audio/webm", data=b"fake-audio-bytes"):
    return client.post(
        "/api/v1/submissions",
        data={"topic_id": topic_id},
        files={"file": (filename, io.BytesIO(data), content_type)},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_student_submits_audio_successfully(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    _override_storage(FakeAudioStorage(durations=[45.0]))

    resp = _post(client, token, topic.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["duration_seconds"] == 45.0
    assert body["topic_id"] == topic.id
    assert body["reviewed"] is False


async def test_upload_uses_a_meaningful_cloudinary_public_id(client):
    """The point of this naming scheme: findable in the Cloudinary console by
    student and topic, not just a random id. `_login_with_user`/`_make_topic`
    use the fixed names "Test User" / "Talking about Music", so the expected
    slug is known exactly.
    """
    token, student = await _login_with_user(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    fake = FakeAudioStorage(durations=[30.0])
    _override_storage(fake)

    resp = _post(client, token, topic.id)
    assert resp.status_code == 200

    assert len(fake.public_ids) == 1
    public_id = fake.public_ids[0]
    prefix = f"Tamakkan/test-user-{student.id[:8]}/talking-about-music-"
    assert public_id.startswith(prefix)
    # .../{title}-{YYYYMMDD-HHMMSS}-{uuid4 hex} - a stable readable prefix, then
    # a timestamp, then a full uuid so the uniqueness claim is a guarantee
    # rather than a 32-bit coin flip.
    timestamp, random_suffix = public_id.removeprefix(prefix).rsplit("-", 1)
    assert len(timestamp) == len("20260101-120000")
    assert len(random_suffix) == 32


async def test_a_student_cannot_submit_against_a_topic_outside_their_plan(client):
    """The write-side entitlement gate. Before this existed, a Student who got a
    403 from `GET /topics/{id}` could still submit against that same topic and
    get its title back in `label` - both a write into unentitled content and an
    oracle for it.
    """
    token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", assigned_level=Level.BEGINNER
    )
    topic = await _make_topic()  # intermediate
    fake = FakeAudioStorage()
    _override_storage(fake)

    resp = _post(client, token, topic.id)
    assert resp.status_code == 403
    assert fake.upload_calls == []  # rejected before anything was stored
    assert await RecordingSubmission.find_all().to_list() == []


async def test_a_student_with_no_plan_cannot_submit(client):
    token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", plan=None, assigned_level=None
    )
    topic = await _make_topic()
    fake = FakeAudioStorage()
    _override_storage(fake)

    assert _post(client, token, topic.id).status_code == 403
    assert fake.upload_calls == []


async def test_a_pro_student_can_submit_against_a_hand_picked_topic(client):
    """Pro entitlement is an explicit topic list, not a level - so a Pro student
    may submit against an Advanced topic that no level match would allow.
    """
    topic = TopicContent(title="Advanced Debate", level=Level.ADVANCED)
    await topic.insert()
    template = CalendarTemplate(
        name="T",
        kind=TemplateKind.CUSTOM,
        owner_admin_id=DEFAULT_ADMIN_ID,
        is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    )
    await template.insert()
    token, _ = await _login_with_user(
        client,
        Role.STUDENT,
        "pro@example.com",
        plan=PlanType.PRO,
        assigned_level=None,
        assigned_topic_ids=[topic.id],
        assigned_plan_template_id=template.id,
    )
    _override_storage(FakeAudioStorage(durations=[30.0]))

    assert _post(client, token, topic.id).status_code == 200


async def test_media_url_is_a_signed_url_not_the_stored_one(client):
    """The stored asset uses Cloudinary's `authenticated` delivery type, so the
    response must carry a signed URL rather than a publicly-fetchable one.
    """
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    _override_storage(FakeAudioStorage(durations=[30.0]))

    body = _post(client, token, topic.id).json()
    assert "__cld_token__=signed" in body["media_url"]


async def test_non_student_cannot_submit(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _make_topic()
    _override_storage(FakeAudioStorage())

    resp = _post(client, token, topic.id)
    assert resp.status_code == 403


async def test_unknown_topic_is_404(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    _override_storage(FakeAudioStorage())

    resp = _post(client, token, "not-a-real-topic-id")
    assert resp.status_code == 404


async def test_non_audio_content_type_is_rejected_before_uploading(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    fake = FakeAudioStorage()
    _override_storage(fake)

    resp = _post(client, token, topic.id, content_type="video/mp4")
    assert resp.status_code == 422
    assert fake.upload_calls == []  # never even attempted


async def test_overlong_recording_is_rejected_and_asset_is_cleaned_up(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    fake = FakeAudioStorage(durations=[125.0])
    _override_storage(fake)

    resp = _post(client, token, topic.id)
    assert resp.status_code == 422
    assert len(fake.deleted_public_ids) == 1  # the rejected upload was cleaned up

    all_submissions = await RecordingSubmission.find_all().to_list()
    assert all_submissions == []  # no record written for a rejected submission


async def test_upload_retries_then_succeeds(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    fake = FakeAudioStorage(durations=[20.0], fail_times=2)  # fails twice, succeeds on the 3rd
    _override_storage(fake)

    resp = _post(client, token, topic.id)
    assert resp.status_code == 200
    assert len(fake.upload_calls) == 3


async def test_upload_exhausts_retries_and_returns_502(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    fake = FakeAudioStorage(fail_times=99)  # never succeeds
    _override_storage(fake)

    resp = _post(client, token, topic.id)
    assert resp.status_code == 502
    assert len(fake.upload_calls) == 3  # UPLOAD_ATTEMPTS, not retried forever

    all_submissions = await RecordingSubmission.find_all().to_list()
    assert all_submissions == []


async def test_resubmitting_for_the_same_topic_links_replaces(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    first = _post(client, token, topic.id).json()

    _override_storage(FakeAudioStorage(durations=[40.0]))
    second = _post(client, token, topic.id).json()

    assert first["id"] != second["id"]
    second_doc = await RecordingSubmission.get(second["id"])
    replaced = await second_doc.replaces.fetch()
    assert replaced.id == first["id"]

    student = await User.find_one(User.email == "student@example.com")
    latest = await RecordingSubmissionRepository().get_latest_for_student_and_topic(student.id, topic.id)
    assert latest.id == second["id"]


async def test_resubmitting_for_a_different_topic_does_not_link_replaces(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic_a = await _make_topic()
    topic_b = TopicContent(title="Talking about Travel", level=Level.INTERMEDIATE)
    await topic_b.insert()
    template = await CalendarTemplate.find_one(CalendarTemplate.owner_admin_id == DEFAULT_ADMIN_ID)
    template.days[0].topic_ids.append(topic_b.id)
    await template.save()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, token, topic_a.id)

    _override_storage(FakeAudioStorage(durations=[30.0]))
    second = _post(client, token, topic_b.id).json()

    second_doc = await RecordingSubmission.get(second["id"])
    assert second_doc.replaces is None


# --- Card #8: submissions views --------------------------------------------


async def test_student_lists_only_their_own_submissions(client):
    token_a, student_a = await _login_with_user(client, Role.STUDENT, "student-a@example.com")
    _, student_b = await _login_with_user(client, Role.STUDENT, "student-b@example.com")
    topic = await _make_topic()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, token_a, topic.id)

    token_b = client.post(
        "/api/v1/auth/login", json={"email": "student-b@example.com", "password": "correct-password"}
    ).json()["access_token"]
    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, token_b, topic.id)

    resp = client.get("/api/v1/submissions/me", headers={"Authorization": f"Bearer {token_a}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["student_id"] == student_a.id


async def test_students_own_list_includes_full_history_not_just_latest(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, token, topic.id)
    _override_storage(FakeAudioStorage(durations=[40.0]))
    _post(client, token, topic.id)

    resp = client.get("/api/v1/submissions/me", headers={"Authorization": f"Bearer {token}"})
    assert len(resp.json()) == 2  # both the replaced and the replacing one


async def test_owning_student_can_fetch_a_single_submission(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    topic = await _make_topic()
    _override_storage(FakeAudioStorage(durations=[30.0]))
    created = _post(client, token, topic.id).json()

    resp = client.get(f"/api/v1/submissions/{created['id']}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_a_different_student_cannot_fetch_someone_elses_submission(client):
    token_owner = await _login(client, Role.STUDENT, "owner@example.com")
    token_other = await _login(client, Role.STUDENT, "other@example.com")
    topic = await _make_topic()
    _override_storage(FakeAudioStorage(durations=[30.0]))
    created = _post(client, token_owner, topic.id).json()

    resp = client.get(f"/api/v1/submissions/{created['id']}", headers={"Authorization": f"Bearer {token_other}"})
    assert resp.status_code == 403


async def test_owning_admin_can_fetch_their_students_submission(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    student_token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", created_by_id=admin.id
    )
    topic = await _make_topic(owner_admin_id=admin.id)
    _override_storage(FakeAudioStorage(durations=[30.0]))
    created = _post(client, student_token, topic.id).json()

    resp = client.get(f"/api/v1/submissions/{created['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200


async def test_a_different_admin_cannot_fetch_someone_elses_student_submission(client):
    """The card text says "an Administrator" can play any submission; this
    deliberately narrows that to the owning Administrator only, matching the
    ownership rule already enforced for password resets - otherwise any
    admin could pull the media URL for any student on the platform.
    """
    owning_admin_token, owning_admin = await _login_with_user(client, Role.TEACHER, "owner@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, "attacker@example.com")
    student_token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", created_by_id=owning_admin.id
    )
    topic = await _make_topic(owner_admin_id=owning_admin.id)
    _override_storage(FakeAudioStorage(durations=[30.0]))
    created = _post(client, student_token, topic.id).json()

    resp = client.get(
        f"/api/v1/submissions/{created['id']}", headers={"Authorization": f"Bearer {other_admin_token}"}
    )
    assert resp.status_code == 403


async def test_unknown_submission_id_is_404(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/submissions/not-a-real-id", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_admin_list_is_scoped_to_their_own_students(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, other_admin = await _login_with_user(client, Role.TEACHER, "other-admin@example.com")
    own_student_token, _ = await _login_with_user(
        client, Role.STUDENT, "own-student@example.com", created_by_id=admin.id
    )
    other_student_token, _ = await _login_with_user(
        client, Role.STUDENT, "other-student@example.com", created_by_id=other_admin.id
    )
    topic = await _make_topic(owner_admin_id=admin.id)
    other_template = CalendarTemplate(
        name="T",
        kind=TemplateKind.STANDARD,
        owner_admin_id=other_admin.id,
        level=Level.INTERMEDIATE,
        is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topic.id])],
    )
    await other_template.insert()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, own_student_token, topic.id)
    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, other_student_token, topic.id)

    resp = client.get("/api/v1/submissions", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["student_id"] != ""


async def test_admin_cannot_filter_by_a_student_that_is_not_theirs(client):
    admin_token, _ = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, other_admin = await _login_with_user(client, Role.TEACHER, "other-admin@example.com")
    _, other_student = await _login_with_user(
        client, Role.STUDENT, "other-student@example.com", created_by_id=other_admin.id
    )

    resp = client.get(
        f"/api/v1/submissions?student_id={other_student.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 403


async def test_admin_unreviewed_count_matches_an_independent_count(client):
    """Card #8's literal done-when: the admin unreviewed count must match an
    independent count of `reviewed=false` records. There's no separate
    count endpoint - the frontend counts from the same list this returns -
    so this proves that count is consistent, not that it comes from a
    second, potentially-diverging code path.
    """
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    student_token, student = await _login_with_user(
        client, Role.STUDENT, "student@example.com", created_by_id=admin.id
    )
    topic_a = await _make_topic(owner_admin_id=admin.id)
    topic_b = TopicContent(title="Talking about Travel", level=Level.INTERMEDIATE)
    await topic_b.insert()
    template = await CalendarTemplate.find_one(CalendarTemplate.owner_admin_id == admin.id)
    template.days[0].topic_ids.append(topic_b.id)
    await template.save()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, student_token, topic_a.id)
    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, student_token, topic_b.id)

    all_submissions = await RecordingSubmission.find(RecordingSubmission.student_id == student.id).to_list()
    independent_unreviewed_count = sum(1 for s in all_submissions if not s.reviewed)

    resp = client.get(
        "/api/v1/submissions?reviewed=false", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == independent_unreviewed_count == 2


async def test_admin_topic_filter_matches_the_chip(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    student_token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", created_by_id=admin.id
    )
    topic_a = await _make_topic(owner_admin_id=admin.id)
    topic_b = TopicContent(title="Talking about Travel", level=Level.INTERMEDIATE)
    await topic_b.insert()
    template = await CalendarTemplate.find_one(CalendarTemplate.owner_admin_id == admin.id)
    template.days[0].topic_ids.append(topic_b.id)
    await template.save()

    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, student_token, topic_a.id)
    _override_storage(FakeAudioStorage(durations=[30.0]))
    _post(client, student_token, topic_b.id)

    resp = client.get(
        f"/api/v1/submissions?topic_id={topic_a.id}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["topic_id"] == topic_a.id


async def test_non_admin_cannot_list_admin_submissions_view(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/submissions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# --- POST /submissions/end-of-week-video --------------------------------------


def _post_video(client, token, day, filename="check-in.webm", content_type="video/webm", data=b"fake-video-bytes"):
    return client.post(
        "/api/v1/submissions/end-of-week-video",
        data={"day": day},
        files={"file": (filename, io.BytesIO(data), content_type)},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_student_submits_end_of_week_video_successfully(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    _override_storage(FakeAudioStorage(durations=[90.0]))

    resp = _post_video(client, token, day=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "friday_video"  # wire value unchanged, see SubmissionKind.END_OF_WEEK_VIDEO
    assert body["day"] == 5
    assert body["topic_id"] is None
    assert body["label"] == "End-of-week check-in - Day 5"
    assert body["duration_seconds"] == 90.0


async def test_end_of_week_video_is_not_entitlement_gated(client):
    """Unlike the audio task endpoint, this isn't topic-tied, so a student
    with no plan at all can still submit their weekly check-in.
    """
    token, _ = await _login_with_user(client, Role.STUDENT, "student@example.com", plan=None, assigned_level=None)
    _override_storage(FakeAudioStorage(durations=[60.0]))

    resp = _post_video(client, token, day=5)
    assert resp.status_code == 200


async def test_non_video_content_type_is_rejected_before_uploading(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    fake = FakeAudioStorage()
    _override_storage(fake)

    resp = _post_video(client, token, day=5, content_type="audio/webm")
    assert resp.status_code == 422
    assert fake.upload_calls == []


async def test_overlong_end_of_week_video_is_rejected_and_asset_is_cleaned_up(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    fake = FakeAudioStorage(durations=[130.0])
    _override_storage(fake)

    resp = _post_video(client, token, day=5)
    assert resp.status_code == 422
    assert len(fake.deleted_public_ids) == 1

    all_submissions = await RecordingSubmission.find_all().to_list()
    assert all_submissions == []


async def test_non_student_cannot_submit_end_of_week_video(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    _override_storage(FakeAudioStorage())

    resp = _post_video(client, token, day=5)
    assert resp.status_code == 403


async def test_resubmitting_for_the_same_day_links_replaces(client):
    token = await _login(client, Role.STUDENT, "student@example.com")

    _override_storage(FakeAudioStorage(durations=[60.0]))
    first = _post_video(client, token, day=5).json()

    _override_storage(FakeAudioStorage(durations=[70.0]))
    second = _post_video(client, token, day=5).json()

    assert first["id"] != second["id"]
    second_doc = await RecordingSubmission.get(second["id"])
    replaced = await second_doc.replaces.fetch()
    assert replaced.id == first["id"]

    student = await User.find_one(User.email == "student@example.com")
    latest = await RecordingSubmissionRepository().get_latest_for_student_and_day(student.id, 5)
    assert latest.id == second["id"]


async def test_resubmitting_for_a_different_day_does_not_link_replaces(client):
    token = await _login(client, Role.STUDENT, "student@example.com")

    _override_storage(FakeAudioStorage(durations=[60.0]))
    _post_video(client, token, day=5)

    _override_storage(FakeAudioStorage(durations=[60.0]))
    second = _post_video(client, token, day=12).json()

    second_doc = await RecordingSubmission.get(second["id"])
    assert second_doc.replaces is None


# --- PATCH /submissions/{id}/reviewed ----------------------------------------


async def test_owning_admin_can_mark_a_submission_reviewed(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    student_token, _ = await _login_with_user(client, Role.STUDENT, "student@example.com", created_by_id=admin.id)
    _override_storage(FakeAudioStorage(durations=[60.0]))
    created = _post_video(client, student_token, day=5).json()
    assert created["reviewed"] is False

    resp = client.patch(
        f"/api/v1/submissions/{created['id']}/reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["reviewed"] is True

    # And it can be un-marked, same route.
    resp2 = client.patch(
        f"/api/v1/submissions/{created['id']}/reviewed",
        json={"reviewed": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp2.json()["reviewed"] is False


async def test_a_different_admin_cannot_mark_someone_elses_student_submission_reviewed(client):
    owning_admin_token, owning_admin = await _login_with_user(client, Role.TEACHER, "owner@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, "attacker@example.com")
    student_token, _ = await _login_with_user(
        client, Role.STUDENT, "student@example.com", created_by_id=owning_admin.id
    )
    _override_storage(FakeAudioStorage(durations=[60.0]))
    created = _post_video(client, student_token, day=5).json()

    resp = client.patch(
        f"/api/v1/submissions/{created['id']}/reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {other_admin_token}"},
    )
    assert resp.status_code == 403


async def test_student_cannot_mark_their_own_submission_reviewed(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    _override_storage(FakeAudioStorage(durations=[60.0]))
    created = _post_video(client, token, day=5).json()

    resp = client.patch(
        f"/api/v1/submissions/{created['id']}/reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_mark_reviewed_unknown_submission_is_404(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.patch(
        "/api/v1/submissions/not-a-real-id/reviewed",
        json={"reviewed": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
