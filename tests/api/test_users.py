"""Role-guard proof: a Student calling an Administrator-only endpoint gets
403; an Administrator calling the same endpoint succeeds. (Kanban card #2's
literal Done-when.) Also covers card #4 - listing an instructor's own
students, scoped so one admin never sees another admin's roster.
"""

from app.core.security import hash_password
from app.models.user import Role, User


async def _create_and_login(client, role: Role, email: str, password: str = "correct-password") -> tuple[str, User]:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def test_student_cannot_create_users(client):
    token, _ = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        "/api/v1/users",
        json={"email": "new@example.com", "password": "password123", "full_name": "New Student"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_administrator_can_create_users(client):
    token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    resp = client.post(
        "/api/v1/users",
        json={"email": "new@example.com", "password": "password123", "full_name": "New Student"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "student"


async def test_create_user_duplicate_email_is_conflict(client):
    token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    payload = {"email": "dup@example.com", "password": "password123", "full_name": "Dup"}
    first = client.post("/api/v1/users", json=payload, headers={"Authorization": f"Bearer {token}"})
    second = client.post("/api/v1/users", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200
    assert second.status_code == 409


async def test_newly_created_student_can_sign_in_with_no_plan_assigned(client):
    token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    payload = {"email": "new@example.com", "password": "password123", "full_name": "New Student"}
    created = client.post("/api/v1/users", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert created.json()["plan"] is None
    assert created.json()["assigned_level"] is None

    login = client.post("/api/v1/auth/login", json={"email": "new@example.com", "password": "password123"})
    assert login.status_code == 200


async def test_teacher_lists_only_their_own_students(client):
    """PROMPT-047: this is now a Teacher-isolation rule, not an Administrator one - the Center Admin
    (`Role.ADMINISTRATOR`) deliberately sees every student in the center, see `test_role_separation.py`."""
    admin_one_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-one@example.com")
    admin_two_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-two@example.com")

    client.post(
        "/api/v1/users",
        json={"email": "student-one@example.com", "password": "password123", "full_name": "Student One"},
        headers={"Authorization": f"Bearer {admin_one_token}"},
    )
    client.post(
        "/api/v1/users",
        json={"email": "student-two@example.com", "password": "password123", "full_name": "Student Two"},
        headers={"Authorization": f"Bearer {admin_two_token}"},
    )

    resp = client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_one_token}"})
    assert resp.status_code == 200
    emails = {student["email"] for student in resp.json()}
    assert emails == {"student-one@example.com"}


async def test_student_cannot_list_users(client):
    token, _ = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# --- Teacher student management (edit email, reset password, delete) --------


async def _create_student(client, admin_token: str, email: str) -> dict:
    return client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Test Student"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()


async def test_administrator_can_update_their_own_students_email(client):
    admin_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    student = await _create_student(client, admin_token, "old@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "new@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "new@example.com"

    login = client.post("/api/v1/auth/login", json={"email": "new@example.com", "password": "password123"})
    assert login.status_code == 200


async def test_updating_a_students_email_to_one_already_in_use_is_a_conflict(client):
    admin_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    await _create_student(client, admin_token, "taken@example.com")
    student = await _create_student(client, admin_token, "mine@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "taken@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409


async def test_update_email_payload_cannot_touch_role_or_status(client):
    """`UpdateStudentEmail` has only one field - this proves the server
    ignores (rather than 422s on) extra keys instead of accidentally letting
    them through to the document.
    """
    admin_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    student = await _create_student(client, admin_token, "old@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "new@example.com", "role": "administrator", "is_active": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "student"
    assert resp.json()["is_active"] is True


async def test_teacher_b_cannot_update_teacher_as_students_email(client):
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-b@example.com")
    student = await _create_student(client, admin_a_token, "owned@example.com")

    resp = client.patch(
        f"/api/v1/users/{student['id']}/email",
        json={"email": "hijacked@example.com"},
        headers={"Authorization": f"Bearer {admin_b_token}"},
    )
    assert resp.status_code == 403


async def test_administrator_cannot_update_another_administrators_email(client):
    """`get_own_student` (reused here unchanged) treats a non-Student id as
    not-found rather than forbidden - same fail-closed shape as every other
    per-student admin action in this codebase, including the existing
    reset-password route.
    """
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-a@example.com")
    _, admin_b = await _create_and_login(client, Role.TEACHER, "teacher-b@example.com")

    resp = client.patch(
        f"/api/v1/users/{admin_b.id}/email",
        json={"email": "hijacked@example.com"},
        headers={"Authorization": f"Bearer {admin_a_token}"},
    )
    assert resp.status_code == 404


async def test_administrator_can_reset_their_own_students_password(client):
    admin_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    student = await _create_student(client, admin_token, "student@example.com")

    resp = client.post(
        f"/api/v1/users/{student['id']}/reset-password",
        json={"new_password": "brand-new-password"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert "password" not in resp.json()
    assert "password_hash" not in resp.json()

    login = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "brand-new-password"}
    )
    assert login.status_code == 200


async def test_teacher_b_cannot_reset_teacher_as_students_password(client):
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-b@example.com")
    student = await _create_student(client, admin_a_token, "owned@example.com")

    resp = client.post(
        f"/api/v1/users/{student['id']}/reset-password",
        json={"new_password": "hijacked-password"},
        headers={"Authorization": f"Bearer {admin_b_token}"},
    )
    assert resp.status_code == 403


async def test_administrator_can_delete_their_own_student(client):
    admin_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    student = await _create_student(client, admin_token, "student@example.com")

    resp = client.delete(
        f"/api/v1/users/{student['id']}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 204

    listed = client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert not any(s["id"] == student["id"] for s in listed.json())

    login = client.post(
        "/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"}
    )
    assert login.status_code == 401

    # Soft delete, not physical.
    from app.models.user import User as UserModel

    raw = await UserModel.get(student["id"])
    assert raw is not None
    assert raw.is_deleted is True


async def test_teacher_b_cannot_delete_teacher_as_student(client):
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "teacher-b@example.com")
    student = await _create_student(client, admin_a_token, "owned@example.com")

    resp = client.delete(
        f"/api/v1/users/{student['id']}", headers={"Authorization": f"Bearer {admin_b_token}"}
    )
    assert resp.status_code == 403

    listed = client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_a_token}"})
    assert any(s["id"] == student["id"] for s in listed.json())


async def test_administrator_cannot_delete_another_administrator(client):
    admin_a_token, _ = await _create_and_login(client, Role.ADMINISTRATOR, "teacher-a@example.com")
    _, admin_b = await _create_and_login(client, Role.ADMINISTRATOR, "teacher-b@example.com")

    resp = client.delete(
        f"/api/v1/users/{admin_b.id}", headers={"Authorization": f"Bearer {admin_a_token}"}
    )
    assert resp.status_code == 404
