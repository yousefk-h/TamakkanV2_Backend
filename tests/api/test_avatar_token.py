"""Avatar Practice session tokens for the NEW SpatialReal platform.

The browser's SpatialReal `credential` is minted on the server from the API key (api.spatialreal.com), only for the
caller's OWN open Avatar Practice session, and the key itself never appears in any response. SpatialReal is faked with
httpx's MockTransport: no network, no real key.
"""

import json
import re
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import get_settings
from app.models.live_session import LiveSession, SessionState
from app.models.user import Role
from app.services.spatialreal_token_service import (
    MAX_TTL_SECONDS,
    SESSION_TOKEN_URL,
    SpatialRealTokenService,
    get_spatialreal_token_service,
)
from tests.api.test_sessions import FAKE_INTERNAL_API_KEY, _login_with_user
from tests.api.test_voice_practice import _auth, _lesson, _start

API_KEY = "sk-TEST-ONLY-not-a-real-key-0123456789abcd="
APP_ID = "app_test_1234abcd"
AVATAR_ID = "11111111-2222-3333-4444-555555555555"
FAKE_TOKEN = "eyJ.fake.session-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake-secret-thats-long-enough")
    monkeypatch.setenv("AGENT_INTERNAL_API_KEY", FAKE_INTERNAL_API_KEY)
    monkeypatch.setenv("LIVEKIT_AGENT_NAME", "tamakkan-mariam-dev")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSpatialReal:
    """Records every request the backend sends to SpatialReal and answers like the real endpoint."""

    def __init__(self, status: int = 200, body: dict | str | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = {"session_token": FAKE_TOKEN} if body is None else body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, str):
            return httpx.Response(self.status, content=self.body)
        payload = dict(self.body)
        if self.status == 200 and "expire_at" not in payload:
            payload["expire_at"] = json.loads(request.content)["expire_at"]
        return httpx.Response(self.status, json=payload)


@pytest.fixture
def spatialreal():
    from app.core.application import app

    fake = FakeSpatialReal()

    def _provider():
        return SpatialRealTokenService(api_key=API_KEY, app_id=APP_ID, avatar_id=AVATAR_ID, transport=httpx.MockTransport(fake.handler))

    app.dependency_overrides[get_spatialreal_token_service] = _provider
    yield fake
    app.dependency_overrides.pop(get_spatialreal_token_service, None)


async def _open_avatar_session(client, email="student@example.com"):
    token, student = await _login_with_user(client, Role.STUDENT, email=email)
    topic = await _lesson()
    started = _start(client, token, topic.id, mode=None)
    assert started.status_code == 200, started.text
    return token, student, started.json()["session_id"]


def _mint(client, token, session_id):
    return client.post(f"/api/v1/sessions/{session_id}/avatar-token", headers=_auth(token))


# --- happy path and what is (not) exposed ----------------------------------------------------------------------------


async def test_student_gets_a_session_token_for_their_own_open_avatar_session(client, spatialreal):
    token, _, session_id = await _open_avatar_session(client)
    response = _mint(client, token, session_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_token"] == FAKE_TOKEN
    assert body["app_id"] == APP_ID and body["avatar_id"] == AVATAR_ID
    assert int(time.time()) < body["expires_at"] <= int(time.time()) + MAX_TTL_SECONDS
    assert set(body) == {"session_token", "expires_at", "app_id", "avatar_id"}, "exactly these fields, nothing else"
    assert response.headers["cache-control"] == "no-store"


async def test_the_api_key_never_reaches_the_browser(client, spatialreal):
    token, _ = await _login_with_user(client, Role.STUDENT, email="keycheck@example.com")
    topic = await _lesson()
    started = _start(client, token, topic.id, mode=None)
    assert started.status_code == 200, started.text
    response = _mint(client, token, started.json()["session_id"])
    assert response.status_code == 200
    # nothing the browser receives - the start response, the token response, any header - carries the secret
    everything = started.text + response.text + json.dumps(dict(started.headers)) + json.dumps(dict(response.headers))
    assert API_KEY not in everything and "sk-TEST" not in everything


async def test_the_new_spatialreal_endpoint_is_used_with_the_documented_request(client, spatialreal):
    token, _, session_id = await _open_avatar_session(client)
    _mint(client, token, session_id)
    assert len(spatialreal.requests) == 1
    request = spatialreal.requests[0]
    assert request.method == "POST"
    assert str(request.url) == SESSION_TOKEN_URL == "https://api.spatialreal.com/v1/auth/session-token"
    assert request.headers["x-api-key"] == API_KEY, "the key goes to SpatialReal in X-API-KEY, server to server"
    sent = json.loads(request.content)
    assert set(sent) == {"expire_at"}
    now = int(time.time())
    assert now < sent["expire_at"] <= now + 24 * 3600, "must be in the future and at most 24 h ahead"
    assert sent["expire_at"] <= now + 3600, "kept short: one token covers starting one session"
    assert "spatialwalk" not in str(request.url)


# --- who may ask --------------------------------------------------------------------------------------------------------


async def test_another_student_cannot_mint_a_token_for_someone_elses_session(client, spatialreal):
    _, _, session_id = await _open_avatar_session(client, email="owner@example.com")
    other_token, _ = await _login_with_user(client, Role.STUDENT, email="intruder@example.com")
    response = _mint(client, other_token, session_id)
    assert response.status_code == 403
    assert spatialreal.requests == [], "SpatialReal is never contacted for a request that fails ownership"
    assert API_KEY not in response.text


async def test_unknown_session_is_404_and_anonymous_is_401(client, spatialreal):
    token, _ = await _login_with_user(client, Role.STUDENT, email="s@example.com")
    assert _mint(client, token, "does-not-exist").status_code == 404
    anonymous = client.post("/api/v1/sessions/x/avatar-token")
    assert anonymous.status_code == 401
    assert spatialreal.requests == []


async def test_an_administrator_cannot_use_the_student_endpoint(client, spatialreal):
    _, _, session_id = await _open_avatar_session(client)
    admin_token, _ = await _login_with_user(client, Role.ADMINISTRATOR, email="boss@example.com")
    assert _mint(client, admin_token, session_id).status_code == 403
    assert spatialreal.requests == []


async def test_only_open_avatar_sessions_can_mint(client, spatialreal):
    token, _ = await _login_with_user(client, Role.STUDENT, email="modes@example.com")
    topic = await _lesson()
    avatar = _start(client, token, topic.id, mode=None)
    voice = _start(client, token, topic.id, mode="voice_practice")  # same lesson, the other practice mode
    assert avatar.status_code == 200 and voice.status_code == 200, (avatar.text, voice.text)
    session_id = avatar.json()["session_id"]
    # a Voice Practice session has no avatar
    assert _mint(client, token, voice.json()["session_id"]).status_code == 422
    # the open Avatar session can mint ...
    assert _mint(client, token, session_id).status_code == 200
    # ... an ended one needs no avatar
    session = await LiveSession.get(session_id)
    session.state = SessionState.ENDED
    await session.save()
    assert _mint(client, token, session_id).status_code == 422
    assert len(spatialreal.requests) == 1, "SpatialReal was contacted only for the one valid request"


# --- failure handling never leaks the key or SpatialReal's internals -------------------------------------------------


@pytest.mark.parametrize("status, body", [(401, {"error": "invalid api key"}), (400, {"error": "expire_at must be in the future"}), (500, "oops")])
async def test_an_upstream_error_is_a_generic_502(client, spatialreal, status, body):
    spatialreal.status, spatialreal.body = status, body
    token, _, session_id = await _open_avatar_session(client)
    response = _mint(client, token, session_id)
    assert response.status_code == 502
    assert API_KEY not in response.text and "invalid api key" not in response.text and "spatialreal" not in response.text.lower()


async def test_a_response_without_a_token_is_a_502(client, spatialreal):
    spatialreal.body = {"unexpected": True}
    token, _, session_id = await _open_avatar_session(client)
    assert _mint(client, token, session_id).status_code == 502


async def test_unconfigured_server_answers_503_not_a_guess(client, monkeypatch):
    from app.core.application import app

    app.dependency_overrides.pop(get_spatialreal_token_service, None)
    for name in ("SPATIALREAL_API_KEY", "SPATIALREAL_APP_ID", "SPATIALREAL_AVATAR_ID"):
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    token, _, session_id = await _open_avatar_session(client)
    assert _mint(client, token, session_id).status_code == 503


# --- the service on its own ---------------------------------------------------------------------------------------------


async def test_service_rejects_unsafe_ttls_and_reports_unconfigured():
    for ttl in (0, 59, MAX_TTL_SECONDS + 1, 10**7):
        with pytest.raises(ValueError):
            SpatialRealTokenService(api_key="k", app_id="a", avatar_id="v", ttl_seconds=ttl)
    assert not SpatialRealTokenService(api_key="", app_id="a", avatar_id="v").configured
    assert SpatialRealTokenService(api_key="k", app_id="a", avatar_id="v").configured


async def test_a_network_failure_is_a_502_without_details(client):
    from app.core.application import app

    def boom(_request):
        raise httpx.ConnectError("name or service not known")

    app.dependency_overrides[get_spatialreal_token_service] = lambda: SpatialRealTokenService(
        api_key=API_KEY, app_id=APP_ID, avatar_id=AVATAR_ID, transport=httpx.MockTransport(boom)
    )
    try:
        token, _, session_id = await _open_avatar_session(client)
        response = _mint(client, token, session_id)
        assert response.status_code == 502 and "name or service" not in response.text
    finally:
        app.dependency_overrides.pop(get_spatialreal_token_service, None)


# --- no legacy platform in the backend runtime ----------------------------------------------------------------------------


def test_the_legacy_spatialwalk_platform_is_not_referenced_by_backend_runtime_code():
    root = Path(__file__).resolve().parents[2] / "app"

    def code(text: str) -> str:  # prose may explain what is NOT used; only code counts
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        return re.sub(r"(?m)^\s*#.*$", "", text)

    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if re.search(r"spatialwalk|console\.[a-z-]+\.spatial|v1/console/session-tokens", code(p.read_text(encoding="utf-8")), re.I)
    ]
    assert offenders == []
