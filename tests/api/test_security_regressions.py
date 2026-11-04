"""Regression tests for defects found in the security review.

Each test here corresponds to a specific confirmed vulnerability. If one starts
failing, a real hole has reopened - none of these assert incidental behavior.
"""

from contextlib import contextmanager

import pytest
from jose import jwt
from pydantic import ValidationError

from app.core.application import app
from app.core.config import PLACEHOLDER_JWT_SECRET, Settings
from app.core.security import decode_access_token, hash_password, verify_password
from app.models.user import Role, User
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schema.auth import LoginRequest, PasswordResetConfirm
from app.services.auth_service import AuthService, get_auth_service
from app.utils.time import utcnow


@contextmanager
def _captured_emails(sink: list):
    """Swap in an email sender that records instead of sending.

    Overrides `get_auth_service` rather than `get_email_sender`, because the
    service factory calls `get_email_sender()` directly - it isn't resolved
    through `Depends`, so overriding it alone would silently have no effect and
    the test would pass for the wrong reason.
    """

    class FakeSender:
        async def send(self, to: str, subject: str, body: str) -> None:
            sink.append((to, subject, body))

    app.dependency_overrides[get_auth_service] = lambda: AuthService(
        UserRepository(), RefreshTokenRepository(), PasswordResetTokenRepository(), FakeSender()
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_service, None)

# A password that is inside the old character-based limit but outside bcrypt's
# byte limit: 20 emoji = 20 characters, 80 UTF-8 bytes.
OVERLONG_BY_BYTES = "😀" * 20


async def _make(email: str, role: Role = Role.STUDENT, password: str = "correct-password", **extra) -> User:
    user = User(
        email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra
    )
    await user.insert()
    return user


# --- Account enumeration via the bcrypt byte limit -------------------------


async def test_overlong_password_does_not_leak_account_existence(client):
    """The bug: bcrypt rejects >72 *bytes* while the schema allowed 72
    *characters*, so a multi-byte password reached bcrypt only for accounts
    that exist - raising ValueError -> 500, versus 401 for accounts that don't.
    That difference is a perfect account-existence oracle for an
    unauthenticated caller, defeating the generic-error rule (FR-003).
    """
    await _make("real@example.com")

    hit = client.post("/api/v1/auth/login", json={"email": "real@example.com", "password": OVERLONG_BY_BYTES})
    miss = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": OVERLONG_BY_BYTES}
    )

    assert hit.status_code == miss.status_code
    assert hit.status_code != 500


def test_password_schemas_reject_on_byte_length_not_character_length():
    # 72 characters that fit in 72 bytes is fine; 20 characters that don't, isn't.
    PasswordResetConfirm(token="t", new_password="a" * 72)
    with pytest.raises(ValidationError):
        PasswordResetConfirm(token="t", new_password=OVERLONG_BY_BYTES)
    with pytest.raises(ValidationError):
        LoginRequest(email="a@b.com", password=OVERLONG_BY_BYTES)


def test_hash_and_verify_never_raise_on_an_overlong_password():
    """Schemas are the first line, but these are also called from jobs and
    scripts that don't go through one - they must clamp, not explode.
    """
    hashed = hash_password(OVERLONG_BY_BYTES)
    assert verify_password(OVERLONG_BY_BYTES, hashed) is True
    assert verify_password("something-else", hashed) is False


# --- JWT hardening ----------------------------------------------------------


def test_token_without_exp_is_rejected():
    """python-jose validates `exp` when present but does not require it, so
    without an explicit requirement a signed token that simply omits the claim
    would never expire.
    """
    settings = Settings()
    forged = jwt.encode(
        {"sub": "someone", "role": "root", "type": "access"},  # no exp
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(Exception) as exc:
        decode_access_token(forged)
    assert "Invalid or expired token" in str(exc.value)


def test_token_without_sub_is_rejected_not_a_500():
    settings = Settings()
    forged = jwt.encode(
        {"role": "root", "type": "access", "exp": utcnow().timestamp() + 3600},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(Exception) as exc:
        decode_access_token(forged)
    assert "Invalid or expired token" in str(exc.value)


def test_placeholder_jwt_secret_is_refused_outside_development():
    """A deploy that forgets JWT_SECRET_KEY must fail loudly. Booting happily on
    the repo's published key means anyone who read the repo can forge a token
    for any account whose id they know.
    """
    with pytest.raises(ValidationError):
        Settings(ENVIRONMENT="production", JWT_SECRET_KEY=PLACEHOLDER_JWT_SECRET)

    # Still allowed locally, and a real secret is fine anywhere.
    Settings(ENVIRONMENT="development", JWT_SECRET_KEY=PLACEHOLDER_JWT_SECRET)
    Settings(ENVIRONMENT="production", JWT_SECRET_KEY="a-genuinely-random-secret")


# --- Session revocation on password change ---------------------------------


async def test_change_password_revokes_other_sessions(client):
    await _make("admin@example.com", Role.ADMINISTRATOR)
    first = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    ).json()
    second = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    ).json()

    resp = client.post(
        "/api/v1/me/change-password",
        json={"current_password": "correct-password", "new_password": "a-brand-new-password"},
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert resp.status_code == 200

    # The other device's refresh token must be dead too, not just this one's.
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401


async def test_root_reset_revokes_the_administrators_sessions(client):
    await _make("root@example.com", Role.ROOT)
    admin = await _make("admin@example.com", Role.ADMINISTRATOR)
    root_token = client.post(
        "/api/v1/auth/login", json={"email": "root@example.com", "password": "correct-password"}
    ).json()["access_token"]
    admin_refresh = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    ).json()["refresh_token"]

    client.post(
        f"/api/v1/root/admins/{admin.id}/reset-password",
        json={"new_password": "root-chosen-password"},
        headers={"Authorization": f"Bearer {root_token}"},
    )

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": admin_refresh}).status_code == 401


async def test_revoke_all_for_user_only_touches_that_user(client):
    alice = await _make("alice@example.com", Role.ADMINISTRATOR)
    await _make("bob@example.com", Role.ADMINISTRATOR)
    client.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "correct-password"})
    bob_refresh = client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": "correct-password"}
    ).json()["refresh_token"]

    revoked = await RefreshTokenRepository().revoke_all_for_user(alice.id)
    assert revoked == 1
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": bob_refresh}).status_code == 200


# --- Soft-delete / deactivation gaps ---------------------------------------


async def test_soft_deleted_user_cannot_refresh(client):
    user = await _make("gone@example.com")
    refresh_token = client.post(
        "/api/v1/auth/login", json={"email": "gone@example.com", "password": "correct-password"}
    ).json()["refresh_token"]

    await UserRepository().soft_delete(user)

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401


async def test_deactivated_user_cannot_complete_a_password_reset(client):
    admin = await _make("admin@example.com", Role.ADMINISTRATOR)
    _record, raw = await PasswordResetTokenRepository().create_for_user(admin)

    admin.is_active = False
    await admin.save()

    resp = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": "should-not-apply"},
    )
    assert resp.status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "should-not-apply"}
        ).status_code
        == 401
    )


# --- The reset flow actually delivers something ----------------------------


async def test_password_reset_request_delivers_a_usable_token(client):
    """The raw token exists only at creation time (only its hash is stored), so
    discarding it meant the endpoint reported "a reset link has been sent"
    having sent nothing, and no admin could ever complete a self-service reset.
    """
    sent: list[tuple[str, str, str]] = []
    with _captured_emails(sent):
        await _make("admin@example.com", Role.ADMINISTRATOR)
        resp = client.post("/api/v1/auth/password-reset/request", json={"email": "admin@example.com"})
        assert resp.status_code == 200

    assert len(sent) == 1, "no reset email was produced"
    to, _subject, body = sent[0]
    assert to == "admin@example.com"

    raw_token = body.rsplit(": ", 1)[-1].strip()
    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "reset-via-emailed-token"},
    )
    assert confirmed.status_code == 200
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "reset-via-emailed-token"},
        ).status_code
        == 200
    )


async def test_students_still_get_no_reset_email(client):
    """Students go through their instructor; this endpoint must stay silent for
    them, and look identical to an unknown email.
    """
    sent: list[tuple[str, str, str]] = []
    with _captured_emails(sent):
        await _make("student@example.com", Role.STUDENT)
        known = client.post("/api/v1/auth/password-reset/request", json={"email": "student@example.com"})
        unknown = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})

    assert sent == []
    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json()


# --- Duplicate key surfaces as 409, not 500 --------------------------------


async def test_reusing_a_soft_deleted_email_is_409_not_500(client):
    """`get_by_email` can't see soft-deleted rows, so the pre-check passes and
    the unique index is what rejects the write. That must be a 409.
    """
    admin = await _make("admin@example.com", Role.ADMINISTRATOR)
    token = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    ).json()["access_token"]

    student = await _make("recycled@example.com", Role.STUDENT, created_by_id=admin.id)
    await UserRepository().soft_delete(student)

    resp = client.post(
        "/api/v1/users",
        json={"email": "recycled@example.com", "password": "password123", "full_name": "New"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
