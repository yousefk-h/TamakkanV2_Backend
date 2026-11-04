from app.core.security import hash_password
from app.models.user import Role, User
from app.repositories.password_reset_repository import PasswordResetTokenRepository


async def _create_user(email="student@example.com", password="correct-password", role=Role.STUDENT):
    user = User(email=email, password_hash=hash_password(password), full_name="Test Student", role=role)
    await user.insert()
    return user


async def test_login_success(client):
    await _create_user()
    resp = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


async def test_login_wrong_password_generic_error(client):
    await _create_user()
    resp = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_login_unknown_email_same_generic_error(client):
    resp = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_get_me_requires_auth(client):
    resp = client.get("/api/v1/me")
    assert resp.status_code in (401, 403)


async def test_get_me_with_valid_token(client):
    await _create_user()
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    access_token = login_resp.json()["access_token"]
    resp = client.get("/api/v1/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "student@example.com"


async def test_refresh_rotates_token(client):
    await _create_user()
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    old_refresh = login_resp.json()["refresh_token"]

    refresh_resp = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_resp.status_code == 200
    new_refresh = refresh_resp.json()["refresh_token"]
    assert new_refresh != old_refresh

    # Rotated out - reusing the old one must fail.
    reuse_resp = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_resp.status_code == 401


async def test_logout_revokes_refresh_token(client):
    await _create_user()
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    logout_resp = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 200

    reuse_resp = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse_resp.status_code == 401


async def test_password_reset_request_same_message_regardless_of_email(client):
    await _create_user()
    resp_known = client.post("/api/v1/auth/password-reset/request", json={"email": "student@example.com"})
    resp_unknown = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert resp_known.status_code == resp_unknown.status_code == 200
    assert resp_known.json() == resp_unknown.json()


async def test_password_reset_confirm_changes_password(client):
    user = await _create_user()
    _record, raw_token = await PasswordResetTokenRepository().create_for_user(user)

    resp = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "brand-new-password"},
    )
    assert resp.status_code == 200

    old_login = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "brand-new-password"}
    )
    assert new_login.status_code == 200


async def test_password_reset_token_is_single_use(client):
    user = await _create_user()
    _record, raw_token = await PasswordResetTokenRepository().create_for_user(user)

    first = client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "new-password-1"}
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "new-password-2"}
    )
    assert second.status_code == 401


async def test_administrator_can_change_own_password(client):
    await _create_user(email="admin@example.com", role=Role.ADMINISTRATOR)
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    )
    access_token = login_resp.json()["access_token"]

    resp = client.post(
        "/api/v1/me/change-password",
        json={"current_password": "correct-password", "new_password": "another-new-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 200

    new_login = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "another-new-password"}
    )
    assert new_login.status_code == 200


async def test_administrator_change_password_wrong_current_password(client):
    await _create_user(email="admin@example.com", role=Role.ADMINISTRATOR)
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct-password"}
    )
    access_token = login_resp.json()["access_token"]

    resp = client.post(
        "/api/v1/me/change-password",
        json={"current_password": "totally-wrong", "new_password": "another-new-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 401


async def test_student_cannot_change_own_password(client):
    """Platform plan §3.5: Students don't self-manage their password at all -
    they request their instructor via `/me/request-password-reset` instead.
    """
    await _create_user()
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "correct-password"}
    )
    access_token = login_resp.json()["access_token"]

    resp = client.post(
        "/api/v1/me/change-password",
        json={"current_password": "correct-password", "new_password": "another-new-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 403


async def test_student_cannot_use_forgot_password_email_flow(client):
    """Closes a loophole: a Student could otherwise bypass the
    instructor-mediated reset entirely via the old emailed-link flow. Same
    generic response either way - it must not reveal that the email belongs
    to a Student account.
    """
    await _create_user()
    resp = client.post("/api/v1/auth/password-reset/request", json={"email": "student@example.com"})
    assert resp.status_code == 200

    # No token was created at all - this test's mocked DB starts empty and
    # nothing else in it creates a PasswordResetToken.
    from app.models.password_reset_token import PasswordResetToken

    assert await PasswordResetToken.find_all().to_list() == []
