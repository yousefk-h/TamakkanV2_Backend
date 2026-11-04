import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings
from app.core.exceptions import UnprocessableException
from app.core.security import create_access_token, hash_password
from app.models.user import Role, User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.email_verification_service import get_email_verification_service
from app.utils.time import utcnow

REQUEST = "/api/v1/auth/email-verification/request"
CONFIRM = "/api/v1/auth/email-verification/confirm"
PASSWORD = "correct-password"


class Mailbox:
    def __init__(self):
        self.messages = []
        self.fail = False

    async def send(self, to, subject, body):
        if self.fail:
            raise OSError("Provider unavailable")
        self.messages.append({"to": to, "subject": subject, "body": body})

    def token(self):
        url = next(line for line in self.messages[-1]["body"].splitlines() if line.startswith("http"))
        return parse_qs(urlsplit(url).fragment)["token"][0]


@pytest.fixture
def verification(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, EMAIL_VERIFICATION_ENABLED=True,
                        EMAIL_DELIVERY_MODE="file", EMAIL_BRAND_NAME="Tamakkan",
                        EMAIL_PREVIEW_DIRECTORY=str(tmp_path))
    mailbox = Mailbox()
    for module in ("app.services.user_service", "app.services.email_verification_service", "app.core.email"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.email_verification_service.get_email_sender", lambda: mailbox)
    return settings, mailbox


async def create(client, role=Role.STUDENT):
    creator = User(email="creator@example.com", full_name="Creator", password_hash=hash_password(PASSWORD),
                   role=Role.ADMINISTRATOR if role == Role.STUDENT else Role.ROOT)
    await creator.insert()
    response = client.post("/api/v1/users" if role == Role.STUDENT else "/api/v1/root/admins",
                           headers={"Authorization": f"Bearer {create_access_token(creator.id, creator.role)}"},
                           json={"email": "new@example.com", "password": PASSWORD, "full_name": "New Learner"})
    assert response.status_code == 200, response.text
    return response, await User.get(response.json()["id"])


async def age_send(user):
    await User.get_pymongo_collection().update_one(
        {"_id": user.id}, {"$set": {"email_verification_sent_at": utcnow() - timedelta(seconds=61)}},
    )


@pytest.mark.parametrize("role", [Role.STUDENT, Role.ADMINISTRATOR])
async def test_creation_requires_verification_and_sends_only_email_proof(client, verification, role):
    _, mailbox = verification
    response, user = await create(client, role)
    data = response.json()
    assert data["email_verification_status"] == "pending"
    assert data["email_verification_delivery"] == "sent"
    assert not {"email_verification_hash", "email_verification_email", "password_hash"} & data.keys()
    assert mailbox.messages[0]["to"] == user.email
    assert "Tamakkan" in mailbox.messages[0]["subject"]
    assert PASSWORD not in mailbox.messages[0]["body"]
    token = mailbox.token()
    assert len(token) == 43
    assert token not in user.model_dump_json()
    assert user.email_verification_hash and user.email_verification_expires_at > utcnow()


async def test_pending_login_wrong_password_and_protected_access(client, verification):
    _, user = await create(client)
    wrong = client.post("/api/v1/auth/login", json={"email": user.email, "password": "wrong"})
    assert wrong.status_code == 401
    assert "code" not in wrong.json()
    pending = client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert pending.status_code == 403
    assert pending.json()["code"] == "email_verification_required"
    assert "access_token" not in pending.json()
    access = create_access_token(user.id, user.role)
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"}).status_code == 403
    _, refresh = await RefreshTokenRepository().create_for_user(user)
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 403


async def test_confirmation_is_single_use_then_login_and_refresh_work(client, verification):
    _, mailbox = verification
    _, user = await create(client)
    token = mailbox.token()
    response = client.post(CONFIRM, json={"token": token})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert client.post(CONFIRM, json={"token": token}).status_code == 422
    verified = await User.get(user.id)
    assert verified.email_verified_at and verified.email_verification_hash is None
    login = client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert login.status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}).status_code == 200
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["email_verification_status"] == "verified"


@pytest.mark.parametrize("change", ["expired", "inactive", "deleted", "changed_email"])
async def test_invalid_account_or_link_cannot_be_verified(client, verification, change):
    _, mailbox = verification
    _, user = await create(client)
    updates = {"expired": {"email_verification_expires_at": utcnow() - timedelta(seconds=1)},
               "inactive": {"is_active": False}, "deleted": {"is_deleted": True},
               "changed_email": {"email": "changed@example.com"}}[change]
    await User.get_pymongo_collection().update_one({"_id": user.id}, {"$set": updates})
    assert client.post(CONFIRM, json={"token": mailbox.token()}).status_code == 422
    assert (await User.get(user.id)).email_verified_at is None


async def test_resend_response_is_generic_and_cooldown_prevents_duplicates(client, verification):
    _, mailbox = verification
    _, user = await create(client)
    legacy = User(email="legacy@example.com", full_name="Legacy", password_hash=hash_password(PASSWORD))
    await legacy.insert()
    responses = [client.post(REQUEST, json={"email": email})
                 for email in (user.email, "unknown@example.com", legacy.email)]
    assert all(response.status_code == 202 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert len(mailbox.messages) == 1
    assert responses[0].headers["cache-control"] == "no-store"


async def test_resend_replaces_old_link_and_has_hourly_limit(client, verification):
    _, mailbox = verification
    _, user = await create(client)
    first = mailbox.token()
    for _ in range(7):
        await age_send(user)
        assert client.post(REQUEST, json={"email": user.email}).status_code == 202
    assert len(mailbox.messages) == 5
    assert client.post(CONFIRM, json={"token": first}).status_code == 422
    assert client.post(CONFIRM, json={"token": mailbox.token()}).status_code == 200
    await age_send(user)
    client.post(REQUEST, json={"email": user.email})
    assert len(mailbox.messages) == 5


async def test_resend_budget_resets_after_hour(client, verification):
    _, mailbox = verification
    _, user = await create(client)
    await User.get_pymongo_collection().update_one({"_id": user.id}, {"$set": {
        "email_verification_window_at": utcnow() - timedelta(hours=2),
        "email_verification_sent_at": utcnow() - timedelta(hours=2), "email_verification_send_count": 5}})
    client.post(REQUEST, json={"email": user.email})
    assert len(mailbox.messages) == 2
    assert (await User.get(user.id)).email_verification_send_count == 1


async def test_concurrent_confirmations_only_one_succeeds(client, verification):
    _, mailbox = verification
    await create(client)
    service = get_email_verification_service()
    results = await asyncio.gather(*(service.confirm(mailbox.token()) for _ in range(5)), return_exceptions=True)
    assert results.count(None) == 1
    assert sum(isinstance(result, UnprocessableException) for result in results) == 4


async def test_concurrent_resends_only_send_once(client, verification):
    _, mailbox = verification
    _, user = await create(client)
    await age_send(user)
    service = get_email_verification_service()
    await asyncio.gather(*(service.request(user.email) for _ in range(5)))
    assert len(mailbox.messages) == 2


async def test_failed_email_keeps_account_pending_and_can_be_retried(client, verification):
    _, mailbox = verification
    mailbox.fail = True
    response, user = await create(client)
    assert response.json()["email_verification_delivery"] == "failed"
    assert user.email_verification_status == "pending"
    assert user.email_verification_hash is None
    mailbox.fail = False
    await age_send(user)
    assert client.post(REQUEST, json={"email": user.email}).status_code == 202
    assert client.post(CONFIRM, json={"token": mailbox.token()}).status_code == 200


async def test_stale_profile_save_does_not_restore_a_consumed_token(client, verification):
    _, mailbox = verification
    _, stale = await create(client)
    token = mailbox.token()
    assert client.post(CONFIRM, json={"token": token}).status_code == 200
    stale.full_name = "Updated profile"
    saved = await UserRepository().save(stale)
    assert saved.email_verification_status == "verified"
    assert saved.email_verification_hash is None
    assert saved.full_name == "Updated profile"
    assert client.post(CONFIRM, json={"token": token}).status_code == 422


async def test_legacy_users_are_not_falsely_marked_verified_or_locked_out(client, verification):
    user = User(email="legacy@example.com", full_name="Existing account", password_hash=hash_password(PASSWORD))
    await user.insert()
    # Mimic documents written before the new fields existed.
    fields = {key: "" for key in user.model_dump() if key.startswith("email_verification_") or key == "email_verified_at"}
    await User.get_pymongo_collection().update_one({"_id": user.id}, {"$unset": fields})
    response = client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200
    assert (await User.get(user.id)).email_verification_status == "not_required"


async def test_feature_disabled_preserves_original_new_account_flow(client, verification):
    settings, mailbox = verification
    settings.EMAIL_VERIFICATION_ENABLED = False
    response, user = await create(client)
    assert response.json()["email_verification_status"] == "not_required"
    assert not mailbox.messages
    assert client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 200


async def test_random_token_cannot_verify_and_get_never_consumes_link(client, verification):
    _, mailbox = verification
    await create(client)
    assert client.get(CONFIRM).status_code == 405
    assert client.post(CONFIRM, json={"token": "x" * 43}).status_code == 422
    assert client.post(CONFIRM, json={"token": mailbox.token()}).status_code == 200
