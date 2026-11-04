"""Phase 3: Group classes & 1:1 coaching. A class is either Group (any number
of the admin's own Standard-plan students) or 1:1 (exactly one of the
admin's own Pro-plan students) - see `app.services.live_class_service`.
"""

from datetime import timedelta

from app.core.security import hash_password
from app.models.live_class import LiveClass
from app.models.user import PlanType, Role, User
from app.utils.time import utcnow

FUTURE = (utcnow() + timedelta(days=3)).isoformat()
PAST = (utcnow() - timedelta(days=3)).isoformat()


async def _login_with_user(client, role: Role, email: str, password: str = "correct-password", **extra) -> tuple[str, User]:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


def _create_body(**overrides):
    body = {
        "kind": "group",
        "title": "Debate: Social Media Impact",
        "scheduled_at": FUTURE,
        "duration_minutes": 45,
        "meet_link": "https://meet.google.com/abc-defg-hij",
        "participant_ids": [],
    }
    body.update(overrides)
    return body


async def test_admin_creates_a_group_class_for_standard_students(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student_a = await _login_with_user(
        client, Role.STUDENT, "a@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    _, student_b = await _login_with_user(
        client, Role.STUDENT, "b@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student_a.id, student_b.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "group"
    assert sorted(body["participant_ids"]) == sorted([student_a.id, student_b.id])


async def test_admin_creates_a_1to1_class_for_a_pro_student(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "pro@example.com", created_by_id=admin.id, plan=PlanType.PRO
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(kind="one_to_one", participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["participant_ids"] == [student.id]


async def test_1to1_class_rejects_more_than_one_student(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, a = await _login_with_user(client, Role.STUDENT, "a@example.com", created_by_id=admin.id, plan=PlanType.PRO)
    _, b = await _login_with_user(client, Role.STUDENT, "b@example.com", created_by_id=admin.id, plan=PlanType.PRO)

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(kind="one_to_one", participant_ids=[a.id, b.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_group_class_rejects_a_pro_student(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "pro@example.com", created_by_id=admin.id, plan=PlanType.PRO
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(kind="group", participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_1to1_class_rejects_a_standard_student(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(kind="one_to_one", participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_class_rejects_a_student_that_is_not_the_admins_own(client):
    admin_token, _ = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, other_admin = await _login_with_user(client, Role.TEACHER, "other-admin@example.com")
    _, other_student = await _login_with_user(
        client, Role.STUDENT, "other-student@example.com", created_by_id=other_admin.id, plan=PlanType.STANDARD
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[other_student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 403


async def test_class_rejects_no_participants(client):
    admin_token, _ = await _login_with_user(client, Role.TEACHER, "admin@example.com")

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_class_rejects_a_past_scheduled_time(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(scheduled_at=PAST, participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_accepts_a_timezone_aware_scheduled_at(client):
    """The frontend sends `Date.toISOString()`, which always carries a "Z"
    offset - this app's stored/compared datetimes are naive-UTC throughout
    (see `app.utils.time.utcnow`), so an aware value must be converted and
    stripped, not just accepted as-is and later crash on comparison.
    """
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    aware_future = (utcnow() + timedelta(days=3)).isoformat() + "Z"

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(scheduled_at=aware_future, participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200


async def test_meet_link_must_be_https(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(meet_link="http://meet.google.com/abc-defg-hij", participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_non_admin_cannot_create_a_class(client):
    token, _ = await _login_with_user(client, Role.STUDENT, "student@example.com")

    resp = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_admin_list_is_scoped_to_their_own_classes(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    other_admin_token, other_admin = await _login_with_user(client, Role.TEACHER, "other@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    _, other_student = await _login_with_user(
        client, Role.STUDENT, "other-std@example.com", created_by_id=other_admin.id, plan=PlanType.STANDARD
    )

    client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[other_student.id]),
        headers={"Authorization": f"Bearer {other_admin_token}"},
    )

    resp = client.get("/api/v1/classes", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["participant_ids"] == [student.id]


async def test_student_sees_only_classes_they_are_enrolled_in(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    student_token, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    _, other_student = await _login_with_user(
        client, Role.STUDENT, "other-std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )

    client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[other_student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = client.get("/api/v1/classes/me", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["participant_ids"] == [student.id]


async def test_non_student_cannot_list_my_classes(client):
    token, _ = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    resp = client.get("/api/v1/classes/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_admin_can_reschedule_their_own_class(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    created = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()

    new_time = (utcnow() + timedelta(days=10)).isoformat()
    resp = client.patch(
        f"/api/v1/classes/{created['id']}",
        json={"scheduled_at": new_time, "title": "Rescheduled"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Rescheduled"


async def test_admin_cannot_update_another_admins_class(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, "other@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    created = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()

    resp = client.patch(
        f"/api/v1/classes/{created['id']}",
        json={"title": "Hijacked"},
        headers={"Authorization": f"Bearer {other_admin_token}"},
    )
    assert resp.status_code == 404


async def test_admin_can_cancel_their_own_class(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    created = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()

    resp = client.delete(f"/api/v1/classes/{created['id']}", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 204

    stored = await LiveClass.get(created["id"])
    assert stored.is_deleted is True

    listing = client.get("/api/v1/classes", headers={"Authorization": f"Bearer {admin_token}"})
    assert listing.json() == []


async def test_admin_cannot_cancel_another_admins_class(client):
    admin_token, admin = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    other_admin_token, _ = await _login_with_user(client, Role.TEACHER, "other@example.com")
    _, student = await _login_with_user(
        client, Role.STUDENT, "std@example.com", created_by_id=admin.id, plan=PlanType.STANDARD
    )
    created = client.post(
        "/api/v1/classes",
        json=_create_body(participant_ids=[student.id]),
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()

    resp = client.delete(f"/api/v1/classes/{created['id']}", headers={"Authorization": f"Bearer {other_admin_token}"})
    assert resp.status_code == 404


async def test_unknown_class_id_is_404(client):
    admin_token, _ = await _login_with_user(client, Role.TEACHER, "admin@example.com")
    resp = client.delete("/api/v1/classes/not-a-real-id", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 404
