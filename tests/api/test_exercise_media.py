"""Teacher lesson media uploads stay scoped and bounded without real provider calls."""

from io import BytesIO
from types import SimpleNamespace

from app.core.security import hash_password
from app.models.user import Role, User


async def _teacher(client, email):
    user = User(email=email, password_hash=hash_password("test-password"), full_name=email, role=Role.TEACHER)
    await user.insert()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "test-password"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_teacher_uploads_owned_exercise_media(client, monkeypatch):
    from app.api.v1 import topics as routes

    class Storage:
        async def upload(self, data, public_id):
            assert data
            return SimpleNamespace(secure_url=f"https://media.example.com/{public_id}")

    monkeypatch.setattr(routes, "get_image_storage", lambda: Storage())
    monkeypatch.setattr(routes, "get_lesson_audio_storage", lambda: Storage())
    owner = await _teacher(client, "media-owner@example.com")
    other = await _teacher(client, "media-other@example.com")
    topic = client.post("/api/v1/topics", json={"title": "Media lesson", "level": "beginner"}, headers=owner).json()
    url = f"/api/v1/topics/{topic['id']}/exercise-media"
    assert client.post(url, files={"file": ("image.png", BytesIO(b"image"), "image/png")}, headers=other).status_code == 403
    image = client.post(url, files={"file": ("image.png", BytesIO(b"image"), "image/png")}, headers=owner)
    assert image.status_code == 200 and image.json()["kind"] == "image"
    audio = client.post(url, files={"file": ("voice.mp3", BytesIO(b"audio"), "audio/mpeg")}, headers=owner)
    assert audio.status_code == 200 and audio.json()["kind"] == "audio"
    assert client.post(url, files={"file": ("bad.txt", BytesIO(b"text"), "text/plain")}, headers=owner).status_code == 422
