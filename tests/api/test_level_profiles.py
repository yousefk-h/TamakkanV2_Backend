"""Center-defined levels stay admin-owned and flow into authored lessons."""

from app.core.security import hash_password
from app.models.user import Role, User


async def _login(client, email: str, role: Role):
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_admin_level_is_available_to_teacher_and_snapshotted_on_topic(client):
    admin_headers = await _login(client, "level-admin@example.com", Role.ADMINISTRATOR)
    teacher_headers = await _login(client, "level-teacher@example.com", Role.TEACHER)
    payload = {
        "code": "pre-a1",
        "name": "Foundation",
        "base_level": "beginner",
        "avatar_guidance": "Use very short sentences, concrete vocabulary, and one question at a time.",
        "order_index": 1,
    }
    assert client.post("/api/v1/level-profiles", json=payload, headers=teacher_headers).status_code == 403
    created = client.post("/api/v1/level-profiles", json=payload, headers=admin_headers)
    assert created.status_code == 201, created.text
    profile = created.json()
    assert [item["id"] for item in client.get("/api/v1/level-profiles", headers=teacher_headers).json()] == [profile["id"]]

    topic = client.post(
        "/api/v1/topics",
        json={"title": "Introductions", "level": "advanced", "level_profile_id": profile["id"]},
        headers=teacher_headers,
    )
    assert topic.status_code == 200, topic.text
    assert topic.json()["level"] == "beginner"
    assert topic.json()["level_name"] == "Foundation"
    assert topic.json()["level_guidance"] == payload["avatar_guidance"]

    listed = client.get("/api/v1/topics", headers=teacher_headers).json()[0]
    assert listed["level_profile_id"] == profile["id"]
    assert listed["level_name"] == "Foundation"
