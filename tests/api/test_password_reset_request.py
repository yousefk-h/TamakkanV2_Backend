"""Student-initiated, instructor-resolved password reset - the replacement
for student self-service (see `app/api/v1/me.py`).
"""

from app.core.application import app
from app.core.security import hash_password
from app.models.password_reset_request import PasswordResetRequest
from app.models.user import Role, User
from app.repositories.password_reset_request_repository import PasswordResetRequestRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.password_reset_request_service import (
    PasswordResetRequestService,
    get_password_reset_request_service,
)


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


async def _create_and_login(client, role: Role, email: str, password: str = "correct-password", **extra) -> tuple[str, User]:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def _admin_with_student(client, admin_email: str, student_email: str) -> tuple[str, str, User]:
    """An Administrator plus a Student they actually created - the only pairing
    for which a reset is now permitted.
    """
    admin_token, admin = await _create_and_login(client, Role.TEACHER, admin_email)
    student_token, student = await _create_and_login(
        client, Role.STUDENT, student_email, created_by_id=admin.id
    )
    return admin_token, student_token, student


async def test_student_request_creates_pending_record(client):
    token, student = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.post("/api/v1/me/request-password-reset", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    pending = await PasswordResetRequestRepository().get_pending_for_student(student.id)
    assert len(pending) == 1
    assert pending[0].resolved is False


async def test_non_student_cannot_request_instructor_reset(client):
    token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    resp = client.post("/api/v1/me/request-password-reset", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_request_emails_owning_admin_when_known(client):
    admin_token, admin = await _create_and_login(client, Role.TEACHER, "owner@example.com")
    other_admin_token, other_admin = await _create_and_login(client, Role.TEACHER, "other@example.com")
    student = await User(
        email="student@example.com",
        password_hash=hash_password("x"),
        full_name="A Student",
        role=Role.STUDENT,
        created_by_id=admin.id,
    ).insert()

    fake_sender = FakeEmailSender()
    app.dependency_overrides[get_password_reset_request_service] = lambda: PasswordResetRequestService(
        UserRepository(), PasswordResetRequestRepository(), fake_sender, RefreshTokenRepository()
    )
    try:
        login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "x"})
        token = login.json()["access_token"]
        resp = client.post("/api/v1/me/request-password-reset", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_password_reset_request_service, None)

    assert len(fake_sender.sent) == 1
    assert fake_sender.sent[0][0] == "owner@example.com"


async def test_request_notifies_nobody_when_the_student_has_no_owning_admin(client):
    """This used to fan out to *every* Administrator on the platform, emailing
    them the student's name and address - the message body interpolates both.

    Fail closed instead, matching `resolve`, which already refuses an unowned
    student outright. The request still returns 200: whether an instructor was
    reachable is not the student's business, and the pending request itself is
    recorded either way, so an admin who looks at their queue still finds it.
    """
    await _create_and_login(client, Role.TEACHER, "admin-one@example.com")
    await _create_and_login(client, Role.TEACHER, "admin-two@example.com")
    # No `created_by_id` - an account seeded outside the normal create flow.
    await User(
        email="student@example.com",
        password_hash=hash_password("x"),
        full_name="A Student",
        role=Role.STUDENT,
    ).insert()

    fake_sender = FakeEmailSender()
    app.dependency_overrides[get_password_reset_request_service] = lambda: PasswordResetRequestService(
        UserRepository(), PasswordResetRequestRepository(), fake_sender, RefreshTokenRepository()
    )
    try:
        login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "x"})
        token = login.json()["access_token"]
        resp = client.post(
            "/api/v1/me/request-password-reset", headers={"Authorization": f"Bearer {token}"}
        )
    finally:
        app.dependency_overrides.pop(get_password_reset_request_service, None)

    assert resp.status_code == 200
    assert fake_sender.sent == []


async def test_admin_resolves_request_sets_password_and_marks_resolved(client):
    admin_token, student_token, student = await _admin_with_student(
        client, "admin@example.com", "student@example.com"
    )

    client.post("/api/v1/me/request-password-reset", headers={"Authorization": f"Bearer {student_token}"})

    resp = client.post(
        f"/api/v1/users/{student.id}/reset-password",
        json={"new_password": "new-password-from-admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200

    old_login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"})
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "new-password-from-admin"}
    )
    assert new_login.status_code == 200

    pending = await PasswordResetRequestRepository().get_pending_for_student(student.id)
    assert pending == []

    all_requests = await PasswordResetRequest.find_all().to_list()
    assert all_requests[0].resolved is True
    assert all_requests[0].resolved_at is not None


async def test_student_cannot_reset_own_password_via_admin_endpoint(client):
    token, student = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        f"/api/v1/users/{student.id}/reset-password",
        json={"new_password": "whatever-long-enough"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_admin_cannot_reset_another_admins_student(client):
    """The role guard alone is not enough - without an ownership check any
    instructor can take over any student on the platform, since student ids
    are handed out in `UserPublic` responses.
    """
    attacker_token, _ = await _create_and_login(client, Role.TEACHER, "attacker@example.com")
    _, _, victim = await _admin_with_student(client, "owner@example.com", "victim@example.com")

    resp = client.post(
        f"/api/v1/users/{victim.id}/reset-password",
        json={"new_password": "attacker-chosen-pw"},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert resp.status_code == 403

    # The victim's real password must still work, and the attacker's must not.
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "victim@example.com", "password": "correct-password"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "victim@example.com", "password": "attacker-chosen-pw"}
        ).status_code
        == 401
    )


async def test_unowned_student_cannot_be_reset_by_any_teacher(client):
    """A student with no `created_by_id` belongs to nobody, so no Teacher may reset them - deliberately refused
    rather than treated as everyone's. The Center Admin (`Role.ADMINISTRATOR`) is the one exception - it can
    reset any student in the center, orphaned or not, same as every other student-management action."""
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    _, orphan = await _create_and_login(client, Role.STUDENT, "orphan@example.com")

    resp = client.post(
        f"/api/v1/users/{orphan.id}/reset-password",
        json={"new_password": "some-new-password"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 403


async def test_reset_revokes_the_students_existing_sessions(client):
    """A reset is the remedy for "someone else is in my account" - if it leaves
    outstanding refresh tokens working, the intruder simply keeps refreshing.
    """
    admin_token, _, student = await _admin_with_student(client, "admin@example.com", "student@example.com")
    stolen = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    ).json()["refresh_token"]

    client.post(
        f"/api/v1/users/{student.id}/reset-password",
        json={"new_password": "instructor-set-password"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": stolen}).status_code == 401
