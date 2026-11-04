"""Length caps and the `youtube_urls` allowlist (see `app/schema/limits.py`).

These matter more than typical validation tests because of *when* FastAPI reads
a body: it parses the request before dependency injection runs, so the
unauthenticated routes below are reachable by anyone who can open a socket, and
`full_name` is Student-writable and leaves the database (into an email body and
into a Cloudinary storage key).
"""

import pytest

from app.core.security import hash_password
from app.models.topic_content import Level
from app.models.user import Role, User
from app.schema.limits import (
    MAX_NAME_LENGTH,
    MAX_LIST_ITEMS,
    MAX_TITLE_LENGTH,
    MAX_TOKEN_LENGTH,
)
from app.services.recording_submission_service import MAX_SLUG_LENGTH, _slugify

TOPIC = {"title": "T", "level": "intermediate"}


async def _login(client, role: Role, email: str, password: str = "correct-password") -> str:
    await User(
        email=email, password_hash=hash_password(password), full_name="Test User", role=role
    ).insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_a_student_cannot_set_an_unbounded_full_name(client):
    """An unbounded name became an unbounded Cloudinary `public_id` that no
    upload could succeed with, and an unbounded email body.
    """
    token = await _login(client, Role.STUDENT, "student@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    too_long = client.patch(
        "/api/v1/me", json={"full_name": "A" * (MAX_NAME_LENGTH + 1)}, headers=headers
    )
    assert too_long.status_code == 422

    at_the_limit = client.patch(
        "/api/v1/me", json={"full_name": "A" * MAX_NAME_LENGTH}, headers=headers
    )
    assert at_the_limit.status_code == 200


async def test_an_empty_full_name_is_rejected(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.patch(
        "/api/v1/me", json={"full_name": ""}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "route",
    ["/api/v1/auth/refresh", "/api/v1/auth/logout"],
)
def test_unauthenticated_token_fields_are_bounded(client, route):
    resp = client.post(route, json={"refresh_token": "x" * (MAX_TOKEN_LENGTH + 1)})
    assert resp.status_code == 422


def test_the_password_reset_confirm_token_is_bounded(client):
    resp = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "x" * (MAX_TOKEN_LENGTH + 1), "new_password": "a-good-password-1"},
    )
    assert resp.status_code == 422


async def test_topic_title_and_lists_are_bounded(client):
    token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post(
        "/api/v1/topics", json={**TOPIC, "title": "T" * (MAX_TITLE_LENGTH + 1)}, headers=headers
    ).status_code == 422
    assert client.post(
        "/api/v1/topics",
        json={**TOPIC, "warm_up_questions": ["q"] * (MAX_LIST_ITEMS + 1)},
        headers=headers,
    ).status_code == 422
    assert client.post("/api/v1/topics", json=TOPIC, headers=headers).status_code == 200


async def test_an_empty_topic_title_is_rejected(client):
    token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    resp = client.post(
        "/api/v1/topics", json={**TOPIC, "title": ""}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "file:///etc/passwd",
        "file://youtube.com/etc/passwd?v=abc123",  # host-only check would pass this
        "https://evil.example/watch?v=abc123",
        "https://youtube.com.evil.example/watch?v=abc123",
        "ftp://youtube.com/watch?v=abc123",
        "not-a-url",
    ],
)
async def test_youtube_urls_reject_anything_that_is_not_youtube(client, url):
    """These strings are handed verbatim to yt-dlp by the pipeline. Rejecting
    them here makes the allowlist this field's contract, rather than a side
    effect of the captions fetcher happening to run first.
    """
    token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={**TOPIC, "youtube_urls": [url]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, f"{url!r} was accepted"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "http://www.youtube.com/watch?v=abc123",
    ],
)
async def test_real_youtube_urls_are_accepted(client, url):
    token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={**TOPIC, "youtube_urls": [url]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"{url!r} was rejected"


async def test_patching_youtube_urls_is_validated_too(client):
    """The pipeline reads `youtube_urls[0]` whether it arrived via create or
    update, so `TopicUpdate` needs the same allowlist as `TopicCreate`.
    """
    token = await _login(client, Role.ADMINISTRATOR, "admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json=TOPIC, headers=headers).json()

    resp = client.patch(
        f"/api/v1/topics/{created['id']}",
        json={"youtube_urls": ["http://169.254.169.254/"]},
        headers=headers,
    )
    assert resp.status_code == 422


def test_slugify_truncates_defensively():
    """Reachable from jobs and scripts that bypass the schema, and its output is
    a storage key rather than a response value.
    """
    assert len(_slugify("a" * 500)) == MAX_SLUG_LENGTH
    assert _slugify("a" * 500) == "a" * MAX_SLUG_LENGTH
