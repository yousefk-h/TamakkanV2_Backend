"""`BaseRepository.soft_delete()` and its effect on lookups - a soft-deleted
document must behave as gone to `get_by_id`/`get_by_email`, without a
physical delete.
"""

from app.core.security import hash_password
from app.models.user import User
from app.repositories.user_repository import UserRepository


async def _make_user() -> User:
    user = User(email="alex@example.com", password_hash=hash_password("x"), full_name="Alex Morgan")
    await user.insert()
    return user


async def test_soft_delete_excludes_from_get_by_id():
    repository = UserRepository()
    user = await _make_user()

    assert await repository.get_by_id(user.id) is not None
    await repository.soft_delete(user)
    assert await repository.get_by_id(user.id) is None

    # Not a physical delete - the document is still in the collection.
    raw = await User.get(user.id)
    assert raw is not None
    assert raw.is_deleted is True


async def test_soft_delete_excludes_from_get_by_email():
    repository = UserRepository()
    user = await _make_user()

    await repository.soft_delete(user)
    assert await repository.get_by_email("alex@example.com") is None


async def test_soft_deleted_user_cannot_log_in(client):
    repository = UserRepository()
    user = await _make_user()
    await repository.soft_delete(user)

    resp = client.post("/api/v1/auth/login", json={"email": "alex@example.com", "password": "x"})
    assert resp.status_code == 401


async def test_soft_deleted_user_token_rejected(client):
    user = await _make_user()
    login = client.post("/api/v1/auth/login", json={"email": "alex@example.com", "password": "x"})
    access_token = login.json()["access_token"]

    await UserRepository().soft_delete(user)

    resp = client.get("/api/v1/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 401
