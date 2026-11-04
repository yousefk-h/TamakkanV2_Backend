"""Kanban card #14: plan assignment, the audited session-duration raise, and
server-side entitlement enforcement on `GET /topics/{topic_id}`. Also card
#15's backend slice: the same entitlement rule applied to `GET /topics` (the
list), at the bottom of this file.
"""

from datetime import timedelta

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.topic_content import Level
from app.models.user import Role, User
from app.utils.time import utcnow

TOPIC_PAYLOAD = {"title": "Talking about Music", "level": "intermediate"}


async def _create_and_login(client, role: Role, email: str, password: str = "correct-password") -> tuple[str, User]:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def _create_student(client, admin_token: str, email: str = "student@example.com") -> dict:
    return client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "A Student"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()


async def _create_topic(client, admin_token: str, level: str = "intermediate", title: str = "Talking about Music") -> dict:
    return client.post(
        "/api/v1/topics",
        json={"title": title, "level": level},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()


def _custom_template_payload(topic_ids: list[str]) -> dict:
    # Days 1, 2, 3, ... - never a Friday check-in day (5/12/19/26), which is
    # fine for these small fixtures.
    return {
        "name": "Custom",
        "kind": "custom",
        "days": [{"day": i + 1, "topic_ids": [tid]} for i, tid in enumerate(topic_ids)],
    }


async def _create_custom_template(client, admin_token: str, topic_ids: list[str]) -> dict:
    return client.post(
        "/api/v1/calendar-templates",
        json=_custom_template_payload(topic_ids),
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()


async def _create_and_activate_standard_template(client, admin_token: str, level: str, topic_ids: list[str]) -> dict:
    """A Standard student's entitlement is now schedule-backed, not a bare
    level match - they need an active template covering the topic on a day
    that's already been reached (day 1, since `assign_plan` sets
    `program_start_date` to now)."""
    template = client.post(
        "/api/v1/calendar-templates",
        json={
            "name": "Track",
            "kind": "standard",
            "level": level,
            "days": [{"day": i + 1, "topic_ids": [tid]} for i, tid in enumerate(topic_ids)],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers={"Authorization": f"Bearer {admin_token}"})
    return template


async def _assign_pro_plan(client, admin_token: str, student_id: str, topic_ids: list[str]) -> dict:
    """Creates a custom template out of `topic_ids` and assigns it - the new
    Pro assign-plan shape, replacing the old hand-picked `assigned_topic_ids`
    body.
    """
    template = await _create_custom_template(client, admin_token, topic_ids)
    return client.post(
        f"/api/v1/users/{student_id}/assign-plan",
        json={"plan": "pro", "assigned_plan_template_id": template["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# --- assign-plan --------------------------------------------------------------


async def test_administrator_can_assign_standard_plan(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "standard"
    assert body["assigned_level"] == "intermediate"
    assert body["assigned_topic_ids"] == []


async def test_administrator_can_assign_pro_plan(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    topic = await _create_topic(client, admin_token)

    resp = await _assign_pro_plan(client, admin_token, student["id"], [topic["id"]])
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "pro"
    assert body["assigned_topic_ids"] == [topic["id"]]
    assert body["assigned_plan_template_id"] is not None
    assert body["assigned_level"] is None


async def test_assign_plan_rejects_a_template_id_that_does_not_exist(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "pro", "assigned_plan_template_id": "not-a-real-template-id"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


async def test_assign_plan_rejects_a_template_belonging_to_another_admin(client):
    owner_token, _ = await _create_and_login(client, Role.TEACHER, "owner@example.com")
    other_admin_token, _ = await _create_and_login(client, Role.TEACHER, "other@example.com")
    student = await _create_student(client, owner_token)
    topic = await _create_topic(client, other_admin_token)
    template = await _create_custom_template(client, other_admin_token, [topic["id"]])

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "pro", "assigned_plan_template_id": template["id"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 403


async def test_assign_plan_rejects_a_standard_template(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    standard_template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "Beginner Track", "kind": "standard", "level": "beginner", "days": []},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "pro", "assigned_plan_template_id": standard_template["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_assign_plan_standard_without_level_is_422(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_administrator_cannot_assign_plan_to_another_admins_student(client):
    owner_token, _ = await _create_and_login(client, Role.TEACHER, "owner@example.com")
    attacker_token, _ = await _create_and_login(client, Role.TEACHER, "attacker@example.com")
    student = await _create_student(client, owner_token)

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert resp.status_code == 403


async def test_student_cannot_assign_plan(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    student_token, _ = await _create_and_login(client, Role.STUDENT, "other@example.com")

    resp = client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert resp.status_code == 403


# --- raise-session-limit -------------------------------------------------------


async def test_raise_session_limit_for_a_pro_student_writes_one_audit_entry(client):
    admin_token, admin = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    topic = await _create_topic(client, admin_token)
    await _assign_pro_plan(client, admin_token, student["id"], [topic["id"]])

    resp = client.post(
        f"/api/v1/users/{student['id']}/raise-session-limit",
        json={"duration_override_minutes": 45},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["duration_override_minutes"] == 45

    entries = await AuditLog.find_all().to_list()
    assert len(entries) == 1
    assert entries[0].action == "raise_session_limit"
    assert entries[0].target_id == student["id"]
    assert entries[0].after == {"duration_override_minutes": 45}


async def test_raise_session_limit_rejected_for_a_standard_student(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = client.post(
        f"/api/v1/users/{student['id']}/raise-session-limit",
        json={"duration_override_minutes": 45},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409
    assert await AuditLog.find_all().to_list() == []


async def test_raise_session_limit_rejected_for_a_student_with_no_plan(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)

    resp = client.post(
        f"/api/v1/users/{student['id']}/raise-session-limit",
        json={"duration_override_minutes": 45},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409


async def test_raise_session_limit_cannot_exceed_the_cap(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    topic = await _create_topic(client, admin_token)
    await _assign_pro_plan(client, admin_token, student["id"], [topic["id"]])

    resp = client.post(
        f"/api/v1/users/{student['id']}/raise-session-limit",
        json={"duration_override_minutes": 181},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_administrator_cannot_raise_limit_for_another_admins_student(client):
    owner_token, _ = await _create_and_login(client, Role.TEACHER, "owner@example.com")
    attacker_token, _ = await _create_and_login(client, Role.TEACHER, "attacker@example.com")
    student = await _create_student(client, owner_token)
    topic = await _create_topic(client, owner_token)
    await _assign_pro_plan(client, owner_token, student["id"], [topic["id"]])

    resp = client.post(
        f"/api/v1/users/{student['id']}/raise-session-limit",
        json={"duration_override_minutes": 45},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert resp.status_code == 403


# --- server-side entitlement enforcement on GET /topics/{id} ------------------


async def test_the_reproduced_hole_is_closed_standard_student_cannot_open_a_higher_level_topic(client):
    """Card #14's literal done-when."""
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    advanced_topic = await _create_topic(client, admin_token, level="advanced", title="Advanced Debate")
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get(
        f"/api/v1/topics/{advanced_topic['id']}", headers={"Authorization": f"Bearer {student_token}"}
    )
    assert resp.status_code == 403


async def test_standard_student_can_open_a_topic_at_their_assigned_level(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    intermediate_topic = await _create_topic(client, admin_token, level="intermediate")
    await _create_and_activate_standard_template(client, admin_token, "intermediate", [intermediate_topic["id"]])
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get(
        f"/api/v1/topics/{intermediate_topic['id']}", headers={"Authorization": f"Bearer {student_token}"}
    )
    assert resp.status_code == 200


async def test_standard_student_cannot_open_a_lesson_whose_day_has_not_arrived_yet(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    day1_topic = await _create_topic(client, admin_token, level="intermediate", title="Day One")
    day2_topic = await _create_topic(client, admin_token, level="intermediate", title="Day Two")
    await _create_and_activate_standard_template(
        client, admin_token, "intermediate", [day1_topic["id"], day2_topic["id"]]
    )
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get(f"/api/v1/topics/{day2_topic['id']}", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 403


async def test_a_rolled_over_lesson_is_still_open(client):
    """A lesson missed on its own day isn't locked once it's passed - it
    rolls forward into the backlog and stays open until it's done."""
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    day1_topic = await _create_topic(client, admin_token, level="intermediate", title="Day One")
    await _create_and_activate_standard_template(client, admin_token, "intermediate", [day1_topic["id"]])
    start_date = (utcnow() - timedelta(days=2)).date().isoformat()
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate", "start_date": start_date},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get(f"/api/v1/topics/{day1_topic['id']}", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 200
    # Still owed as of today, so its overall status is `current` (still
    # actionable), not `locked` - `missed` only ever shows for a day that's
    # since passed, not the topic's one true resolved status.
    assert resp.json()["lesson_status"] == "current"


async def test_pro_student_can_open_only_their_hand_picked_topics(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    assigned_topic = await _create_topic(client, admin_token, title="Assigned Topic")
    other_topic = await _create_topic(client, admin_token, title="Other Topic")
    await _assign_pro_plan(client, admin_token, student["id"], [assigned_topic["id"]])

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    allowed = client.get(
        f"/api/v1/topics/{assigned_topic['id']}", headers={"Authorization": f"Bearer {student_token}"}
    )
    assert allowed.status_code == 200

    rejected = client.get(
        f"/api/v1/topics/{other_topic['id']}", headers={"Authorization": f"Bearer {student_token}"}
    )
    assert rejected.status_code == 403


async def test_student_with_no_plan_cannot_open_any_topic(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    topic = await _create_topic(client, admin_token)

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 403


async def test_administrator_is_never_entitlement_gated(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token, level="advanced")

    resp = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200


# --- Card #15 (backend slice): GET /topics (the list) scoped the same way ---


async def test_standard_students_list_shows_only_their_level(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    intermediate_topic = await _create_topic(client, admin_token, level="intermediate", title="Intermediate Topic")
    advanced_topic = await _create_topic(client, admin_token, level="advanced", title="Advanced Topic")
    await _create_and_activate_standard_template(client, admin_token, "intermediate", [intermediate_topic["id"]])
    client.post(
        f"/api/v1/users/{student['id']}/assign-plan",
        json={"plan": "standard", "assigned_level": "intermediate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert ids == {intermediate_topic["id"]}
    assert advanced_topic["id"] not in ids


async def test_pro_students_list_shows_only_their_hand_picked_topics(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    student = await _create_student(client, admin_token)
    assigned_topic = await _create_topic(client, admin_token, title="Assigned Topic")
    other_topic = await _create_topic(client, admin_token, title="Other Topic")
    await _assign_pro_plan(client, admin_token, student["id"], [assigned_topic["id"]])

    login = client.post("/api/v1/auth/login", json={"email": "student@example.com", "password": "password123"})
    student_token = login.json()["access_token"]

    resp = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert ids == {assigned_topic["id"]}
    assert other_topic["id"] not in ids
