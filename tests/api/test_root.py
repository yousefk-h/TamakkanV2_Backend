"""Root creates Administrators and resets their passwords. Root does not
inherit Administrator's own endpoints (flat, exact-match role checks) - see
`app/api/v1/root.py`.
"""

from app.core.security import hash_password
from app.models.user import Role, User


async def _create_and_login(client, role: Role, email: str, password: str = "correct-password") -> str:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_root_can_create_administrator(client):
    token = await _create_and_login(client, Role.ROOT, "root@example.com")
    resp = client.post(
        "/api/v1/root/admins",
        json={"email": "new-admin@example.com", "password": "password123", "full_name": "New Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "administrator"


async def test_administrator_cannot_create_administrator(client):
    """Root's endpoint, not Administrator's - roles are flat, not hierarchical."""
    token = await _create_and_login(client, Role.ADMINISTRATOR, "admin@example.com")
    resp = client.post(
        "/api/v1/root/admins",
        json={"email": "new-admin@example.com", "password": "password123", "full_name": "New Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_student_cannot_create_administrator(client):
    token = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        "/api/v1/root/admins",
        json={"email": "new-admin@example.com", "password": "password123", "full_name": "New Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_root_created_administrator_has_created_by_set(client):
    token = await _create_and_login(client, Role.ROOT, "root@example.com")
    client.post(
        "/api/v1/root/admins",
        json={"email": "new-admin@example.com", "password": "password123", "full_name": "New Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    new_admin = await User.find_one(User.email == "new-admin@example.com")
    root = await User.find_one(User.email == "root@example.com")
    assert new_admin.created_by_id == root.id


async def test_root_can_reset_administrator_password(client):
    root_token = await _create_and_login(client, Role.ROOT, "root@example.com")
    admin = User(
        email="admin@example.com",
        password_hash=hash_password("old-password"),
        full_name="Locked-out Admin",
        role=Role.ADMINISTRATOR,
    )
    await admin.insert()

    resp = client.post(
        f"/api/v1/root/admins/{admin.id}/reset-password",
        json={"new_password": "new-password-from-root"},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert resp.status_code == 200

    old_login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "old-password"})
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "new-password-from-root"}
    )
    assert new_login.status_code == 200


async def test_root_reset_password_rejects_non_administrator_id(client):
    """The target must actually be an Administrator - resetting a Student's
    password is a separate, Administrator-only flow, not Root's.
    """
    root_token = await _create_and_login(client, Role.ROOT, "root@example.com")
    student = User(
        email="student@example.com",
        password_hash=hash_password("x"),
        full_name="A Student",
        role=Role.STUDENT,
    )
    await student.insert()

    resp = client.post(
        f"/api/v1/root/admins/{student.id}/reset-password",
        json={"new_password": "whatever"},
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert resp.status_code == 404
