"""Server-side search, status filter, pagination and counts for the admin's own Students (`GET /users`, `GET
/users/counts`) - the Center Admin Users dashboard's Students tab.

Everything is filtered in the database, scoped to the calling admin's own roster - same contract shape as `GET
/sessions` (`UserPage`: {items, total, page, limit}). Without `page`/`limit` the endpoint still returns the plain
array other pages rely on.
"""

from app.core.security import hash_password
from app.models.user import Role, User


async def _create_and_login(client, role: Role, email: str, password: str = "correct-password") -> tuple[str, User]:
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"], user


async def _create_student(client, admin_token: str, email: str, full_name: str = "Test Student") -> dict:
    return client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": full_name},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()


async def _deactivate(student_id: str) -> None:
    user = await User.get(student_id)
    user.is_active = False
    await user.save()


# --- compatibility -----------------------------------------------------------------------------------------------


async def test_without_paging_params_the_response_is_still_the_plain_array(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    await _create_student(client, admin_token, "a@example.com")

    resp = client.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --- isolation ---------------------------------------------------------------------------------------------------


async def test_center_admin_sees_only_their_own_students_paginated(client):
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "admin-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "admin-b@example.com")
    await _create_student(client, admin_a_token, "mine@example.com")
    await _create_student(client, admin_b_token, "theirs@example.com")

    resp = client.get("/api/v1/users", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {admin_a_token}"})
    body = resp.json()
    assert body["total"] == 1
    assert {s["email"] for s in body["items"]} == {"mine@example.com"}


async def test_student_cannot_list_the_paged_endpoint(client):
    token, _ = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/users", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_a_specific_other_admins_student_cannot_be_reached_via_search(client):
    """Direct access to another center's user is rejected - searching can never widen the result past your own roster."""
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "admin-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "admin-b@example.com")
    await _create_student(client, admin_b_token, "carol@example.com", full_name="Carol Clark")

    resp = client.get("/api/v1/users", params={"search": "Carol"}, headers={"Authorization": f"Bearer {admin_a_token}"})
    assert resp.json() == []


# --- pagination --------------------------------------------------------------------------------------------------


async def test_the_paged_response_carries_items_total_page_and_limit(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    for i in range(5):
        await _create_student(client, admin_token, f"student{i}@example.com")

    body = client.get(
        "/api/v1/users", params={"page": 1, "limit": 2}, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    assert set(body) == {"items", "total", "page", "limit"}
    assert body["total"] == 5 and body["page"] == 1 and body["limit"] == 2
    assert len(body["items"]) == 2


async def test_page_two_returns_the_remaining_items(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    for i in range(5):
        await _create_student(client, admin_token, f"student{i}@example.com")

    page1 = client.get("/api/v1/users", params={"page": 1, "limit": 2}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    page2 = client.get("/api/v1/users", params={"page": 2, "limit": 2}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    page3 = client.get("/api/v1/users", params={"page": 3, "limit": 2}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    ids = {item["id"] for page in (page1, page2, page3) for item in page["items"]}
    assert len(ids) == 5
    assert len(page3["items"]) == 1


# --- search ------------------------------------------------------------------------------------------------------


async def test_search_matches_name_or_email_case_insensitively(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    alice = await _create_student(client, admin_token, "alice@school.example", full_name="Alice Anderson")
    await _create_student(client, admin_token, "bob@college.example", full_name="Bob Brown")

    by_name = client.get("/api/v1/users", params={"search": "alice"}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert {s["id"] for s in by_name} == {alice["id"]}

    by_email = client.get("/api/v1/users", params={"search": "SCHOOL.EXAMPLE"}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert {s["id"] for s in by_email} == {alice["id"]}


async def test_search_with_no_match_returns_empty(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    await _create_student(client, admin_token, "alice@school.example", full_name="Alice Anderson")

    resp = client.get("/api/v1/users", params={"search": "nobody-like-this"}, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.json() == []


# --- status filter -------------------------------------------------------------------------------------------------


async def test_status_filter_splits_active_and_inactive(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    active = await _create_student(client, admin_token, "active@example.com")
    inactive = await _create_student(client, admin_token, "inactive@example.com")
    await _deactivate(inactive["id"])

    active_only = client.get("/api/v1/users", params={"status": "active"}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    inactive_only = client.get("/api/v1/users", params={"status": "inactive"}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert {s["id"] for s in active_only} == {active["id"]}
    assert {s["id"] for s in inactive_only} == {inactive["id"]}


# --- counts ------------------------------------------------------------------------------------------------------


async def test_counts_reflect_only_this_admins_students(client):
    admin_a_token, _ = await _create_and_login(client, Role.TEACHER, "admin-a@example.com")
    admin_b_token, _ = await _create_and_login(client, Role.TEACHER, "admin-b@example.com")
    await _create_student(client, admin_a_token, "one@example.com")
    inactive = await _create_student(client, admin_a_token, "two@example.com")
    await _deactivate(inactive["id"])
    await _create_student(client, admin_b_token, "other-centers@example.com")

    counts = client.get("/api/v1/users/counts", headers={"Authorization": f"Bearer {admin_a_token}"}).json()
    assert counts == {"total_students": 2, "active_students": 1}


async def test_student_cannot_read_counts(client):
    token, _ = await _create_and_login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/users/counts", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# --- no sensitive data ------------------------------------------------------------------------------------------


async def test_paged_response_never_includes_password_fields(client):
    admin_token, _ = await _create_and_login(client, Role.TEACHER, "admin@example.com")
    await _create_student(client, admin_token, "student@example.com")

    body = client.get("/api/v1/users", params={"page": 1, "limit": 20}, headers={"Authorization": f"Bearer {admin_token}"}).json()
    item = body["items"][0]
    assert "password_hash" not in item
    assert "password" not in item
