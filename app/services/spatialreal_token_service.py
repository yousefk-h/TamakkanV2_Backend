"""Server-side SpatialReal session tokens for Avatar Practice.

The browser needs a short-lived SpatialReal *session token* (its `credential`) to load the avatar. It must never see
the API key: this service exchanges the key for a token on the server, and callers return only the token (plus the
public App ID / Avatar ID, so the browser and the hosted agent can never disagree about which app they use).

Endpoint and body are the NEW SpatialReal platform's (https://docs.spatialreal.ai/api-reference/api-reference):

    POST https://api.spatialreal.com/v1/auth/session-token
    X-API-KEY: <api key>          {"expire_at": <unix seconds, at most 24 h ahead>}
    -> {"session_token": "...", "expire_at": <unix seconds>}

The legacy `console.<region>.spatialwalk.cloud/v1/console/session-tokens` endpoint is intentionally not used anywhere.
Neither the API key nor the token is ever logged; failures are logged by status code only.
"""

import time
from dataclasses import dataclass

import httpx
from loguru import logger

from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableException, UpstreamServiceException

SESSION_TOKEN_URL = "https://api.spatialreal.com/v1/auth/session-token"
# One token per avatar session, valid only as long as a session can plausibly take to start (the browser uses it to
# download the avatar; an already-running session is not ended by expiry). SpatialReal's ceiling is 24 h.
DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 24 * 60 * 60 - 60


@dataclass(frozen=True)
class AvatarCredential:
    session_token: str
    expires_at: int
    app_id: str
    avatar_id: str


class SpatialRealTokenService:
    def __init__(
        self,
        *,
        api_key: str,
        app_id: str,
        avatar_id: str,
        url: str = SESSION_TOKEN_URL,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not 60 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 1 minute and just under 24 hours")
        self._api_key = api_key
        self._app_id = app_id
        self._avatar_id = avatar_id
        self._url = url
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._app_id and self._avatar_id)

    async def issue(self) -> AvatarCredential:
        if not self.configured:
            raise ServiceUnavailableException("The avatar service is not configured")
        expire_at = int(time.time()) + self._ttl
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    self._url,
                    headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                    json={"expire_at": expire_at},
                )
        except httpx.HTTPError as exc:
            logger.warning("SpatialReal session-token request failed: {}", type(exc).__name__)
            raise UpstreamServiceException("Could not start the avatar. Please try again.") from None
        if response.status_code != 200:
            # 401 = unknown/revoked key, 400 = bad expiry: an operator problem, never the student's.
            logger.error("SpatialReal rejected the session-token request (HTTP {})", response.status_code)
            raise UpstreamServiceException("Could not start the avatar. Please try again.")
        try:
            token = response.json().get("session_token")
        except ValueError:
            token = None
        if not isinstance(token, str) or not token:
            logger.error("SpatialReal session-token response had no session_token")
            raise UpstreamServiceException("Could not start the avatar. Please try again.")
        return AvatarCredential(session_token=token, expires_at=expire_at, app_id=self._app_id, avatar_id=self._avatar_id)


def get_spatialreal_token_service() -> SpatialRealTokenService:
    """FastAPI dependency (overridable in tests); reads the settings on every call so a config change needs no restart
    of anything but the process that reads the environment."""
    settings = get_settings()
    return SpatialRealTokenService(
        api_key=settings.SPATIALREAL_API_KEY,
        app_id=settings.SPATIALREAL_APP_ID,
        avatar_id=settings.SPATIALREAL_AVATAR_ID,
    )
