"""Teacher/Student/Admin messaging (Teacher Workspace Increment 3): 1:1
conversations, teacher bulk announcements (Cohort/Program/"my students"),
Admin center broadcast, and the authorization/dedup/duplicate-send rules
around all three. Image/voice attachments use `FakeImageStorage`/
`FakeAudioStorage` instead of real Cloudinary - same reasoning as
`test_submissions.py`'s own `FakeAudioStorage` (both storages are
Protocols exactly so tests don't need real provider credentials).
"""

import pytest

from app.core.application import app
from app.core.storage import ImageUploadResult, UploadFailedError, UploadResult
from app.models.user import Role
from app.services.cohort_service import get_cohort_service
from app.services.messaging_service import MessagingService, get_messaging_service
from tests.api.test_cohorts import _create_cohort, _create_program, _login, _stage_id


class FakeImageStorage:
    """Mirrors `test_submissions.py`'s `FakeAudioStorage` - records every
    call, returns a scripted result/error, so tests can assert both the
    outcome and that a failed/rejected upload's asset gets cleaned up."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.upload_calls: list[bytes] = []
        self.public_ids: list[str] = []
        self.deleted_public_ids: list[str] = []
        self._attempt = 0

    async def upload(self, file_bytes: bytes, public_id: str) -> ImageUploadResult:
        self.upload_calls.append(file_bytes)
        self.public_ids.append(public_id)
        self._attempt += 1
        if self._attempt <= self.fail_times:
            raise UploadFailedError("simulated transient failure")
        return ImageUploadResult(public_id=public_id, secure_url=f"https://cdn.example/{public_id}.jpg")

    async def delete(self, public_id: str) -> None:
        self.deleted_public_ids.append(public_id)

    def signed_url(self, public_id: str) -> str:
        return f"https://cdn.example/{public_id}.jpg?__cld_token__=signed"


class FakeAudioStorage:
    def __init__(self, durations=None, fail_times: int = 0):
        self.durations = list(durations) if durations is not None else [10.0]
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
        duration = self.durations.pop(0) if self.durations else 10.0
        return UploadResult(public_id=public_id, secure_url=f"https://cdn.example/{public_id}.m4a", duration_seconds=duration)

    async def delete(self, public_id: str) -> None:
        self.deleted_public_ids.append(public_id)

    def signed_url(self, public_id: str) -> str:
        return f"https://cdn.example/{public_id}.m4a?__cld_token__=signed"


# A real JPEG magic-number prefix (the file-type sniff checks actual bytes,
# not the claimed Content-Type) followed by filler - large enough to be a
# plausible test payload without approaching the real size limit.
_REAL_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 200


def _override_messaging_storage(image: FakeImageStorage, audio: FakeAudioStorage):
    app.dependency_overrides[get_messaging_service] = lambda: MessagingService(get_cohort_service(), image, audio)


@pytest.fixture(autouse=True)
def _clear_messaging_overrides():
    yield
    app.dependency_overrides.pop(get_messaging_service, None)


async def test_a_teacher_can_message_their_own_student_and_the_student_can_reply(client):
    admin, admin_headers = await _login(client, "msg-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-student@example.com", Role.STUDENT, created_by_id=teacher.id)

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Welcome to class!"}, headers=teacher_headers)
    assert started.status_code == 201, started.text
    conversation_id = started.json()["conversation_id"]
    assert started.json()["is_mine"] is True

    student_inbox = client.get("/api/v1/messages/conversations", headers=student_headers).json()
    assert student_inbox["total"] == 1
    assert student_inbox["items"][0]["last_message_preview"] == "Welcome to class!"
    assert student_inbox["items"][0]["unread_count"] == 1
    assert student_inbox["items"][0]["other_participant_id"] == teacher.id

    thread = client.get(f"/api/v1/messages/conversations/{conversation_id}", headers=student_headers).json()
    assert len(thread["items"]) == 1
    assert thread["items"][0]["is_mine"] is False

    # Reading the thread marks the teacher's message read - a second inbox
    # fetch reflects that for the student (their own sent message is never
    # counted as unread to them).
    after_read = client.get("/api/v1/messages/conversations", headers=student_headers).json()
    assert after_read["items"][0]["unread_count"] == 0

    reply = client.post(f"/api/v1/messages/conversations/{conversation_id}/reply", json={"body": "Thank you!"}, headers=student_headers)
    assert reply.status_code == 201, reply.text

    teacher_inbox = client.get("/api/v1/messages/conversations", headers=teacher_headers).json()
    assert teacher_inbox["items"][0]["unread_count"] == 1
    assert teacher_inbox["items"][0]["last_message_preview"] == "Thank you!"


async def test_a_teacher_cannot_message_a_student_they_do_not_teach(client):
    admin, admin_headers = await _login(client, "msg-perm-admin@example.com", Role.ADMINISTRATOR)
    outsider, outsider_headers = await _login(client, "msg-perm-outsider@example.com", Role.TEACHER)
    student, _ = await _login(client, "msg-perm-student@example.com", Role.STUDENT, created_by_id=admin.id)

    resp = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=outsider_headers)
    assert resp.status_code == 403


async def test_a_student_cannot_start_a_new_conversation(client):
    admin, admin_headers = await _login(client, "msg-student-start-admin@example.com", Role.ADMINISTRATOR)
    teacher, _ = await _login(client, "msg-student-start-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-student-start-student@example.com", Role.STUDENT, created_by_id=admin.id)

    resp = client.post("/api/v1/messages/new", json={"student_id": teacher.id, "body": "Hi"}, headers=student_headers)
    assert resp.status_code == 403


async def test_a_student_cannot_read_another_students_conversation(client):
    admin, admin_headers = await _login(client, "msg-isolation-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-isolation-teacher@example.com", Role.TEACHER)
    student_a, student_a_headers = await _login(client, "msg-isolation-a@example.com", Role.STUDENT, created_by_id=teacher.id)
    student_b, student_b_headers = await _login(client, "msg-isolation-b@example.com", Role.STUDENT, created_by_id=teacher.id)

    started = client.post("/api/v1/messages/new", json={"student_id": student_a.id, "body": "Hi A"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]
    assert client.get(f"/api/v1/messages/conversations/{conversation_id}", headers=student_b_headers).status_code == 403
    assert client.post(f"/api/v1/messages/conversations/{conversation_id}/reply", json={"body": "sneaky"}, headers=student_b_headers).status_code == 403


async def test_bulk_announcement_deduplicates_a_student_enrolled_in_two_cohorts(client):
    admin, admin_headers = await _login(client, "msg-bulk-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-bulk-teacher@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "msg-bulk-program", "Bulk Program")
    beginner = _stage_id(program, "beginner")
    cohort_a = _create_cohort(client, admin_headers, name="Bulk Cohort A", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    cohort_b = _create_cohort(client, admin_headers, name="Bulk Cohort B", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    shared_student, shared_headers = await _login(client, "msg-bulk-shared@example.com", Role.STUDENT, created_by_id=admin.id)
    only_a_student, only_a_headers = await _login(client, "msg-bulk-only-a@example.com", Role.STUDENT, created_by_id=admin.id)
    client.post(f"/api/v1/cohorts/{cohort_a['id']}/students", json={"student_ids": [shared_student.id, only_a_student.id], "program_level_id": beginner}, headers=admin_headers)
    client.post(f"/api/v1/cohorts/{cohort_b['id']}/students", json={"student_ids": [shared_student.id], "program_level_id": beginner}, headers=admin_headers)

    preview = client.get("/api/v1/messages/announcements/recipient-count", params={"scope": "my_students"}, headers=teacher_headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["recipient_count"] == 2  # deduplicated, not 3

    sent = client.post("/api/v1/messages/announcements", json={
        "scope": "my_students", "body": "Class announcement", "client_token": "token-bulk-1",
    }, headers=teacher_headers)
    assert sent.status_code == 201, sent.text
    assert sent.json()["recipient_count"] == 2

    # Each recipient gets exactly one private conversation/message - no
    # duplicate delivery from being in two matching Cohorts.
    shared_inbox = client.get("/api/v1/messages/conversations", headers=shared_headers).json()
    assert shared_inbox["total"] == 1
    assert shared_inbox["items"][0]["last_message_preview"] == "Class announcement"
    only_a_inbox = client.get("/api/v1/messages/conversations", headers=only_a_headers).json()
    assert only_a_inbox["total"] == 1

    # A reply to a bulk announcement is private to the teacher - never
    # visible to the other recipient.
    conv_id = shared_inbox["items"][0]["id"]
    client.post(f"/api/v1/messages/conversations/{conv_id}/reply", json={"body": "private reply"}, headers=shared_headers)
    only_a_conv_id = only_a_inbox["items"][0]["id"]
    only_a_thread = client.get(f"/api/v1/messages/conversations/{only_a_conv_id}", headers=only_a_headers).json()
    assert "private reply" not in str(only_a_thread)


async def test_duplicate_client_token_does_not_resend_an_announcement(client):
    admin, admin_headers = await _login(client, "msg-dupe-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-dupe-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-dupe-student@example.com", Role.STUDENT, created_by_id=admin.id)
    program = _create_program(client, admin_headers, "msg-dupe-program", "Dupe Program")
    beginner = _stage_id(program, "beginner")
    cohort = _create_cohort(client, admin_headers, name="Dupe Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)
    client.post(f"/api/v1/cohorts/{cohort['id']}/students", json={"student_ids": [student.id], "program_level_id": beginner}, headers=admin_headers)

    first = client.post("/api/v1/messages/announcements", json={
        "scope": "my_students", "body": "Once only", "client_token": "token-dupe-1",
    }, headers=teacher_headers)
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/messages/announcements", json={
        "scope": "my_students", "body": "Once only", "client_token": "token-dupe-1",
    }, headers=teacher_headers)
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]

    inbox = client.get("/api/v1/messages/conversations", headers=student_headers).json()
    conv_id = inbox["items"][0]["id"]
    thread = client.get(f"/api/v1/messages/conversations/{conv_id}", headers=student_headers).json()
    assert len(thread["items"]) == 1  # not delivered twice


async def test_only_center_admin_can_broadcast_to_the_whole_center(client):
    admin, admin_headers = await _login(client, "msg-center-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-center-teacher@example.com", Role.TEACHER)

    assert client.post("/api/v1/messages/announcements", json={
        "scope": "center", "body": "Center-wide notice", "client_token": "token-center-teacher",
    }, headers=teacher_headers).status_code == 403

    resp = client.post("/api/v1/messages/announcements", json={
        "scope": "center", "body": "Center-wide notice", "client_token": "token-center-admin",
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text


async def test_a_teacher_cannot_broadcast_to_a_program_they_do_not_teach(client):
    admin, admin_headers = await _login(client, "msg-program-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-program-teacher@example.com", Role.TEACHER)
    outsider, outsider_headers = await _login(client, "msg-program-outsider@example.com", Role.TEACHER)

    program = _create_program(client, admin_headers, "msg-program-program", "Program Scope Program")
    beginner = _stage_id(program, "beginner")
    _create_cohort(client, admin_headers, name="Program Scope Cohort", program=program, stage_ids=[beginner], lead_teacher_id=teacher.id)

    assert client.post("/api/v1/messages/announcements", json={
        "scope": "program", "scope_id": program["id"], "body": "Program notice", "client_token": "token-program-outsider",
    }, headers=outsider_headers).status_code == 403

    resp = client.post("/api/v1/messages/announcements", json={
        "scope": "program", "scope_id": program["id"], "body": "Program notice", "client_token": "token-program-teacher",
    }, headers=teacher_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["recipient_count"] == 0  # no students enrolled yet - still a valid, honest send


async def test_announcement_history_lists_only_the_senders_own_sent_items(client):
    admin, admin_headers = await _login(client, "msg-history-admin@example.com", Role.ADMINISTRATOR)
    teacher_a, teacher_a_headers = await _login(client, "msg-history-teacher-a@example.com", Role.TEACHER)
    teacher_b, teacher_b_headers = await _login(client, "msg-history-teacher-b@example.com", Role.TEACHER)
    student_a, _ = await _login(client, "msg-history-student-a@example.com", Role.STUDENT, created_by_id=teacher_a.id)

    client.post("/api/v1/messages/announcements", json={"scope": "my_students", "body": "A's note", "client_token": "token-history-a"}, headers=teacher_a_headers)

    history_a = client.get("/api/v1/messages/announcements", headers=teacher_a_headers).json()
    assert history_a["total"] == 1
    assert history_a["items"][0]["body"] == "A's note"
    history_b = client.get("/api/v1/messages/announcements", headers=teacher_b_headers).json()
    assert history_b["total"] == 0


# --- Image/voice attachments ------------------------------------------------

async def test_a_student_can_send_an_image_and_the_teacher_can_open_it(client):
    admin, admin_headers = await _login(client, "msg-image-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-image-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-image-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    _override_messaging_storage(FakeImageStorage(), FakeAudioStorage())

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/image",
        files={"file": ("photo.jpg", _REAL_JPEG_BYTES, "image/jpeg")},
        headers=student_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "image"
    assert body["media_url"]
    assert body["media_url"].startswith("https://cdn.example/")

    # The teacher, reading the thread, gets a freshly-signed URL for the same
    # asset - never a raw/stored public_id, never a 403 on their own private
    # conversation.
    thread = client.get(f"/api/v1/messages/conversations/{conversation_id}", headers=teacher_headers).json()
    image_message = next(m for m in thread["items"] if m["kind"] == "image")
    assert image_message["media_url"]
    assert image_message["is_mine"] is False

    # The conversation preview never leaks raw media content as text.
    inbox = client.get("/api/v1/messages/conversations", headers=teacher_headers).json()
    assert inbox["items"][0]["last_message_preview"] == "📷 Photo"


async def test_image_upload_rejects_a_file_that_is_not_actually_an_image(client):
    admin, admin_headers = await _login(client, "msg-badimg-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-badimg-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-badimg-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    fake_image = FakeImageStorage()
    _override_messaging_storage(fake_image, FakeAudioStorage())

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    # Claims to be a JPEG (Content-Type), but the actual bytes are not a real
    # image - the magic-number sniff must catch this before storage is ever
    # touched, not just trust the client's claimed type.
    resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/image",
        files={"file": ("photo.jpg", b"not a real image, just text pretending to be one", "image/jpeg")},
        headers=student_headers,
    )
    assert resp.status_code == 422, resp.text
    assert fake_image.upload_calls == []  # never even attempted


async def test_a_student_can_send_a_voice_message_and_the_teacher_can_play_it(client):
    admin, admin_headers = await _login(client, "msg-voice-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-voice-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-voice-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    _override_messaging_storage(FakeImageStorage(), FakeAudioStorage(durations=[12.0]))

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/audio",
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=student_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "audio"
    assert body["media_url"]
    assert body["media_duration_seconds"] == 12.0

    thread = client.get(f"/api/v1/messages/conversations/{conversation_id}", headers=teacher_headers).json()
    voice_message = next(m for m in thread["items"] if m["kind"] == "audio")
    assert voice_message["media_duration_seconds"] == 12.0
    assert voice_message["media_url"]

    inbox = client.get("/api/v1/messages/conversations", headers=teacher_headers).json()
    assert inbox["items"][0]["last_message_preview"] == "🎤 Voice message"


async def test_a_voice_message_longer_than_the_limit_is_rejected_and_the_orphan_asset_is_deleted(client):
    admin, admin_headers = await _login(client, "msg-longvoice-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-longvoice-teacher@example.com", Role.TEACHER)
    student, student_headers = await _login(client, "msg-longvoice-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    fake_audio = FakeAudioStorage(durations=[90.0])  # over the 60s cap
    _override_messaging_storage(FakeImageStorage(), fake_audio)

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/audio",
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=student_headers,
    )
    assert resp.status_code == 422, resp.text
    assert fake_audio.public_ids == fake_audio.deleted_public_ids  # the uploaded-but-too-long asset was cleaned up

    # Rejected upload never became a real message in the thread.
    thread = client.get(f"/api/v1/messages/conversations/{conversation_id}", headers=teacher_headers).json()
    assert len(thread["items"]) == 1  # only the original "Hi" text message


async def test_a_student_cannot_attach_media_to_another_students_conversation(client):
    admin, admin_headers = await _login(client, "msg-mediaisolation-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-mediaisolation-teacher@example.com", Role.TEACHER)
    student_a, _ = await _login(client, "msg-mediaisolation-a@example.com", Role.STUDENT, created_by_id=teacher.id)
    student_b, student_b_headers = await _login(client, "msg-mediaisolation-b@example.com", Role.STUDENT, created_by_id=teacher.id)
    fake_image = FakeImageStorage()
    _override_messaging_storage(fake_image, FakeAudioStorage())

    started = client.post("/api/v1/messages/new", json={"student_id": student_a.id, "body": "Hi A"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/image",
        files={"file": ("photo.jpg", _REAL_JPEG_BYTES, "image/jpeg")},
        headers=student_b_headers,
    )
    assert resp.status_code == 403, resp.text
    assert fake_image.upload_calls == []  # never reaches storage for an unauthorized conversation


async def test_teachers_may_also_send_image_and_voice_attachments(client):
    """Nothing in the existing permissions model restricts media to
    students only - a Teacher/Admin can attach the same way."""
    admin, admin_headers = await _login(client, "msg-teachermedia-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "msg-teachermedia-teacher@example.com", Role.TEACHER)
    student, _ = await _login(client, "msg-teachermedia-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    _override_messaging_storage(FakeImageStorage(), FakeAudioStorage(durations=[5.0]))

    started = client.post("/api/v1/messages/new", json={"student_id": student.id, "body": "Hi"}, headers=teacher_headers)
    conversation_id = started.json()["conversation_id"]

    image_resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/image",
        files={"file": ("photo.jpg", _REAL_JPEG_BYTES, "image/jpeg")},
        headers=teacher_headers,
    )
    assert image_resp.status_code == 201, image_resp.text

    audio_resp = client.post(
        f"/api/v1/messages/conversations/{conversation_id}/audio",
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=teacher_headers,
    )
    assert audio_resp.status_code == 201, audio_resp.text
