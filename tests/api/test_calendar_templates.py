"""Calendar template CRUD, activation, and the delete-guards that protect a
template still in use - see `app.services.calendar_template_service`.
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


async def _create_topic(client, token: str, level: str = "beginner", title: str = "Topic") -> dict:
    return client.post("/api/v1/topics", json={"title": title, "level": level}, headers=_headers(token)).json()


async def test_administrator_can_create_a_standard_template(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token, level="beginner")

    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "Beginner Track", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "standard"
    assert body["level"] == "beginner"
    assert body["is_active"] is False
    assert body["days"] == [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {}}]


async def test_a_standard_template_without_a_level_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/calendar-templates", json={"name": "X", "kind": "standard", "days": []}, headers=_headers(token)
    )
    assert resp.status_code == 422


async def test_a_custom_template_with_a_level_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "level": "beginner", "days": []},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_a_custom_template_can_mix_topics_from_any_level(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    beginner_topic = await _create_topic(client, token, level="beginner")
    advanced_topic = await _create_topic(client, token, level="advanced")

    resp = client.post(
        "/api/v1/calendar-templates",
        json={
            "name": "Custom",
            "kind": "custom",
            "days": [{"day": 1, "topic_ids": [beginner_topic["id"]]}, {"day": 2, "topic_ids": [advanced_topic["id"]]}],
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200


async def test_a_standard_template_cannot_reference_a_topic_from_a_different_level(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    advanced_topic = await _create_topic(client, token, level="advanced")

    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [advanced_topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_an_unknown_topic_id_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": ["not-a-real-id"]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_a_friday_check_in_day_cannot_be_assigned_a_topic(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 5, "topic_ids": [topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_two_entries_cannot_claim_the_same_day(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]]}, {"day": 1, "topic_ids": [topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_a_day_cannot_repeat_the_same_topic_twice(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"], topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_a_day_can_hold_more_than_one_course(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    first_topic = await _create_topic(client, token, title="First")
    second_topic = await _create_topic(client, token, title="Second")
    resp = client.post(
        "/api/v1/calendar-templates",
        json={
            "name": "X",
            "kind": "custom",
            "days": [{"day": 1, "topic_ids": [first_topic["id"], second_topic["id"]]}],
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"day": 1, "topic_ids": [first_topic["id"], second_topic["id"]], "custom_minutes": {}}]


async def test_a_course_can_have_a_custom_minutes_override(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {topic["id"]: 5}}]},
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {topic["id"]: 5}}]


async def test_custom_minutes_cannot_reference_a_topic_not_scheduled_that_day(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    scheduled = await _create_topic(client, token, title="Scheduled")
    other = await _create_topic(client, token, title="Other")
    resp = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [scheduled["id"]], "custom_minutes": {other["id"]: 5}}]},
        headers=_headers(token),
    )
    assert resp.status_code == 422


async def test_custom_minutes_must_be_a_positive_number_within_the_cap(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    too_high = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {topic["id"]: 181}}]},
        headers=_headers(token),
    )
    assert too_high.status_code == 422

    zero = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {topic["id"]: 0}}]},
        headers=_headers(token),
    )
    assert zero.status_code == 422


async def test_student_cannot_create_a_calendar_template(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post("/api/v1/calendar-templates", json={"name": "X", "kind": "custom", "days": []}, headers=_headers(token))
    assert resp.status_code == 403


async def test_an_admin_only_sees_their_own_templates(client):
    owner_token = await _login(client, Role.TEACHER, "owner@example.com")
    other_token = await _login(client, Role.TEACHER, "other@example.com")
    client.post("/api/v1/calendar-templates", json={"name": "Mine", "kind": "custom", "days": []}, headers=_headers(owner_token))
    client.post("/api/v1/calendar-templates", json={"name": "Theirs", "kind": "custom", "days": []}, headers=_headers(other_token))

    resp = client.get("/api/v1/calendar-templates", headers=_headers(owner_token))
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert names == {"Mine"}


async def test_list_can_be_filtered_by_kind(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    client.post(
        "/api/v1/calendar-templates",
        json={"name": "Standard One", "kind": "standard", "level": "beginner", "days": []},
        headers=_headers(token),
    )
    client.post("/api/v1/calendar-templates", json={"name": "Custom One", "kind": "custom", "days": []}, headers=_headers(token))

    resp = client.get("/api/v1/calendar-templates?kind=custom", headers=_headers(token))
    assert [t["name"] for t in resp.json()] == ["Custom One"]


async def test_an_admin_cannot_fetch_another_admins_template(client):
    owner_token = await _login(client, Role.TEACHER, "owner@example.com")
    other_token = await _login(client, Role.TEACHER, "other@example.com")
    created = client.post(
        "/api/v1/calendar-templates", json={"name": "Mine", "kind": "custom", "days": []}, headers=_headers(owner_token)
    ).json()

    resp = client.get(f"/api/v1/calendar-templates/{created['id']}", headers=_headers(other_token))
    assert resp.status_code == 403


async def test_updating_days_replaces_the_whole_list(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    first_topic = await _create_topic(client, token, title="First")
    second_topic = await _create_topic(client, token, title="Second")
    created = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [first_topic["id"]]}]},
        headers=_headers(token),
    ).json()

    resp = client.patch(
        f"/api/v1/calendar-templates/{created['id']}",
        json={"days": [{"day": 2, "topic_ids": [second_topic["id"]]}]},
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"day": 2, "topic_ids": [second_topic["id"]], "custom_minutes": {}}]


async def test_updating_only_the_name_leaves_days_untouched(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    created = client.post(
        "/api/v1/calendar-templates",
        json={"name": "Old Name", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers=_headers(token),
    ).json()

    resp = client.patch(f"/api/v1/calendar-templates/{created['id']}", json={"name": "New Name"}, headers=_headers(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New Name"
    assert body["days"] == [{"day": 1, "topic_ids": [topic["id"]], "custom_minutes": {}}]


async def test_activating_a_template_deactivates_the_previously_active_one(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    first = client.post(
        "/api/v1/calendar-templates", json={"name": "First", "kind": "standard", "level": "beginner", "days": []}, headers=_headers(token)
    ).json()
    second = client.post(
        "/api/v1/calendar-templates", json={"name": "Second", "kind": "standard", "level": "beginner", "days": []}, headers=_headers(token)
    ).json()
    client.post(f"/api/v1/calendar-templates/{first['id']}/activate", headers=_headers(token))

    resp = client.post(f"/api/v1/calendar-templates/{second['id']}/activate", headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    first_now = client.get(f"/api/v1/calendar-templates/{first['id']}", headers=_headers(token)).json()
    assert first_now["is_active"] is False


async def test_a_custom_template_cannot_be_activated(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post("/api/v1/calendar-templates", json={"name": "X", "kind": "custom", "days": []}, headers=_headers(token)).json()

    resp = client.post(f"/api/v1/calendar-templates/{created['id']}/activate", headers=_headers(token))
    assert resp.status_code == 422


async def test_an_active_standard_template_cannot_be_deleted(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/calendar-templates", json={"name": "X", "kind": "standard", "level": "beginner", "days": []}, headers=_headers(token)
    ).json()
    client.post(f"/api/v1/calendar-templates/{created['id']}/activate", headers=_headers(token))

    resp = client.delete(f"/api/v1/calendar-templates/{created['id']}", headers=_headers(token))
    assert resp.status_code == 422


async def test_an_inactive_standard_template_can_be_deleted(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/calendar-templates", json={"name": "X", "kind": "standard", "level": "beginner", "days": []}, headers=_headers(token)
    ).json()

    resp = client.delete(f"/api/v1/calendar-templates/{created['id']}", headers=_headers(token))
    assert resp.status_code == 204


async def test_a_custom_template_assigned_to_a_student_cannot_be_deleted(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token)
    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "X", "kind": "custom", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers=_headers(admin_token),
    ).json()
    student = client.post(
        "/api/v1/users",
        json={"email": "student@example.com", "password": "password123", "full_name": "S"},
        headers=_headers(admin_token),
    ).json()
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "pro", "assigned_plan_template_id": template["id"]},
        headers=_headers(admin_token),
    )

    resp = client.delete(f"/api/v1/calendar-templates/{template['id']}", headers=_headers(admin_token))
    assert resp.status_code == 422
