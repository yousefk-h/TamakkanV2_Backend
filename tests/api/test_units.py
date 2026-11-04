"""Topic organization by Unit + order (card #17) - CRUD, teacher isolation
(same rule as Topics), and interaction with Topic ownership.
"""

from app.core.security import hash_password
from app.models.user import Role, User


async def _login(client, role: Role, email: str, password: str = "correct-password") -> str:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_administrator_can_create_and_list_their_own_unit(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "Unit 1 - Introductions", "order_index": 0}, headers=_headers(token)
    )
    assert resp.status_code == 200
    unit = resp.json()
    assert unit["name"] == "Unit 1 - Introductions"
    assert unit["created_by_id"] is not None

    listed = client.get("/api/v1/units?level=beginner", headers=_headers(token)).json()
    assert any(u["id"] == unit["id"] for u in listed)


async def test_teacher_b_does_not_see_teacher_as_private_unit(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    unit_a = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "A's unit"}, headers=_headers(token_a)
    ).json()

    listed_b = client.get("/api/v1/units", headers=_headers(token_b)).json()
    assert not any(u["id"] == unit_a["id"] for u in listed_b)


async def test_teacher_b_cannot_update_or_delete_teacher_as_unit(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    unit_a = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "A's unit"}, headers=_headers(token_a)
    ).json()

    update = client.patch(f"/api/v1/units/{unit_a['id']}", json={"name": "Hijacked"}, headers=_headers(token_b))
    assert update.status_code == 403
    delete = client.delete(f"/api/v1/units/{unit_a['id']}", headers=_headers(token_b))
    assert delete.status_code == 403


async def test_root_sees_every_unit_unfiltered(client):
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    root_token = await _login(client, Role.ROOT, "root@example.com")
    unit = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "A unit"}, headers=_headers(teacher_token)
    ).json()

    listed = client.get("/api/v1/units", headers=_headers(root_token)).json()
    assert any(u["id"] == unit["id"] for u in listed)


async def test_a_topic_can_be_assigned_to_the_teachers_own_unit(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    unit = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "Unit 1"}, headers=_headers(token)
    ).json()

    topic = client.post(
        "/api/v1/topics",
        json={"title": "Introducing Yourself", "level": "beginner", "unit_id": unit["id"], "order_index": 1},
        headers=_headers(token),
    )
    assert topic.status_code == 200
    assert topic.json()["unit_id"] == unit["id"]
    assert topic.json()["order_index"] == 1


async def test_a_topic_cannot_be_assigned_to_another_teachers_unit(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    unit_a = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "A's unit"}, headers=_headers(token_a)
    ).json()

    resp = client.post(
        "/api/v1/topics",
        json={"title": "Hijack attempt", "level": "beginner", "unit_id": unit_a["id"]},
        headers=_headers(token_b),
    )
    assert resp.status_code == 403


async def test_a_topic_cannot_be_assigned_to_a_unit_of_a_different_level(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    unit = client.post(
        "/api/v1/units", json={"level": "beginner", "name": "Unit 1"}, headers=_headers(token)
    ).json()

    resp = client.post(
        "/api/v1/topics",
        json={"title": "Mismatched level", "level": "advanced", "unit_id": unit["id"]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_an_unknown_unit_id_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={"title": "Dangling unit", "level": "beginner", "unit_id": "not-a-real-unit"},
        headers=_headers(token),
    )
    assert resp.status_code == 404


async def test_legacy_topic_with_no_unit_has_a_null_unit_id(client):
    """A pre-existing topic (created before this feature) has no unit
    assigned - the Teacher UI groups these under "Unassigned" rather than
    hiding them.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = client.post(
        "/api/v1/topics", json={"title": "No unit yet", "level": "beginner"}, headers=_headers(token)
    ).json()
    assert topic["unit_id"] is None
    assert topic["order_index"] == 0
