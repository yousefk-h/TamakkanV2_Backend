"""Program catalog permissions and student enrollment."""

from app.core.security import hash_password
from app.models.user import Role, User
from datetime import timedelta

from app.utils.time import utcnow


async def _login(client, email: str, role: Role, **extra):
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"}).json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


async def test_center_admin_creates_program_and_links_student(client):
    admin, admin_headers = await _login(client, "program-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "program-teacher@example.com", Role.TEACHER)
    student, _ = await _login(client, "program-student@example.com", Role.STUDENT, created_by_id=teacher.id)
    payload = {"code": "general-english", "name": "General English", "language": "English",
               "description": "Everyday communication", "base_levels": ["beginner", "intermediate"], "level_profile_ids": []}
    assert client.post("/api/v1/programs", json=payload, headers=teacher_headers).status_code == 403
    created = client.post("/api/v1/programs", json=payload, headers=admin_headers)
    assert created.status_code == 201, created.text
    program = created.json()
    assert client.get("/api/v1/programs", headers=teacher_headers).json()[0]["name"] == "General English"

    assigned = client.post(f"/api/v1/users/{student.id}/assign-plan", json={"plan": "standard", "program_id": program["id"],
                           "program_level_id": "beginner"}, headers=teacher_headers)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["program_id"] == program["id"]
    rejected = client.post(f"/api/v1/users/{student.id}/assign-plan", json={"plan": "standard", "program_id": program["id"],
                           "program_level_id": "does-not-exist"}, headers=teacher_headers)
    assert rejected.status_code == 422


async def test_one_topic_is_reused_in_two_programs_with_independent_placement(client):
    admin, admin_headers = await _login(client, "reuse-admin@example.com", Role.ADMINISTRATOR)
    teacher, teacher_headers = await _login(client, "reuse-teacher@example.com", Role.TEACHER)

    def create_program(code: str, name: str):
        response = client.post("/api/v1/programs", json={
            "code": code,
            "name": name,
            "language": "English",
            "base_levels": ["beginner", "intermediate"],
            "level_profile_ids": [],
        }, headers=admin_headers)
        assert response.status_code == 201, response.text
        return response.json()

    general = create_program("general-reuse", "General English")
    business = create_program("business-reuse", "Business English")
    topic_response = client.post("/api/v1/topics", json={
        "title": "Introducing yourself",
        "level": "beginner",
        "lesson_exercises": [{
            "kind": "multiple_choice", "prompt": "Choose hello",
            "options": ["Hello", "Goodbye"], "correct_option": 0,
        }],
    }, headers=teacher_headers)
    assert topic_response.status_code == 200
    topic = topic_response.json()

    first = client.post(f"/api/v1/programs/{general['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": "beginner", "status": "published",
    }, headers=teacher_headers)
    second = client.post(f"/api/v1/programs/{business['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": "intermediate", "status": "published",
    }, headers=teacher_headers)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["program_level_id"] == "beginner"
    assert second.json()["program_level_id"] == "intermediate"
    assert first.json()["day"] is None and second.json()["day"] is None

    # Calendar is the only authoritative scheduling write path. ProgramTopic
    # carries stage/publication, never day/order.
    templates = client.get("/api/v1/calendar-templates", headers=admin_headers).json()
    by_program = {item["program_id"]: item for item in templates if item["program_id"]}
    for program, day in [(general, 1), (business, 7)]:
        scheduled = client.patch(
            f"/api/v1/calendar-templates/{by_program[program['id']]['id']}",
            json={"days": [{"day": day, "topic_ids": [topic["id"]], "custom_minutes": {}}]},
            headers=admin_headers,
        )
        assert scheduled.status_code == 200, scheduled.text

    assignments = client.get(
        f"/api/v1/topics/{topic['id']}/program-assignments", headers=teacher_headers
    ).json()
    assert {item["program_id"] for item in assignments} == {general["id"], business["id"]}

    # Editing creates a new library revision. Existing published placements
    # remain pinned until each Program explicitly publishes the new revision.
    changed = client.patch(
        f"/api/v1/topics/{topic['id']}", json={"title": "Changed silently"}, headers=teacher_headers
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["content_revision"] == 2

    student_a, _ = await _login(
        client, "general-student@example.com", Role.STUDENT, created_by_id=teacher.id
    )
    student_b, _ = await _login(
        client, "business-student@example.com", Role.STUDENT, created_by_id=teacher.id
    )
    for student, program, program_level_id, days_ago in [
        (student_a, general, "beginner", 0),
        (student_b, business, "intermediate", 6),
    ]:
        assigned = client.post(f"/api/v1/users/{student.id}/assign-plan", json={
            "plan": "standard", "program_id": program["id"],
            "program_level_id": program_level_id,
            "start_date": (utcnow().date() - timedelta(days=days_ago)).isoformat(),
        }, headers=teacher_headers)
        assert assigned.status_code == 200, assigned.text

    # Use the real assigned students' tokens.
    token_a = client.post("/api/v1/auth/login", json={"email": student_a.email, "password": "correct-password"}).json()["access_token"]
    token_b = client.post("/api/v1/auth/login", json={"email": student_b.email, "password": "correct-password"}).json()["access_token"]
    response_a = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_a}"})
    response_b = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_b}"})
    assert response_a.status_code == 200 and response_a.json()["title"] == "Introducing yourself"
    assert response_b.status_code == 200 and response_b.json()["title"] == "Introducing yourself"
    assert response_a.json()["lesson_exercises"][0]["prompt"] == "Choose hello"

    republished = client.patch(
        f"/api/v1/programs/{business['id']}/topics/{topic['id']}",
        json={"status": "published"}, headers=teacher_headers,
    )
    assert republished.status_code == 200
    assert republished.json()["published_revision"] == 2
    assert client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_a}"}).json()["title"] == "Introducing yourself"
    assert client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_b}"}).json()["title"] == "Changed silently"

    removed = client.delete(
        f"/api/v1/programs/{general['id']}/topics/{topic['id']}", headers=teacher_headers
    )
    assert removed.status_code == 204
    assert client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_a}"}).status_code == 403
    assert client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token_b}"}).status_code == 200
    assert client.get(f"/api/v1/topics/{topic['id']}", headers=teacher_headers).status_code == 200

    # The other placement and schedule remain; removing from General English
    # neither deletes the Topic nor touches Business English.
    remaining = client.get(f"/api/v1/programs/{business['id']}/topics", headers=teacher_headers).json()
    assert [(item["topic_id"], item["program_level_id"]) for item in remaining] == [
        (topic["id"], "intermediate")
    ]

    # Soft removal is reversible and does not collide with the association's
    # unique (program, topic) key.
    reassigned = client.post(f"/api/v1/programs/{general['id']}/topics", json={
        "topic_id": topic["id"], "program_level_id": "beginner", "status": "draft",
    }, headers=teacher_headers)
    assert reassigned.status_code == 201, reassigned.text
    assert reassigned.json()["day"] is None
