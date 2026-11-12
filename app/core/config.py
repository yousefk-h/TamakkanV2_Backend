"""Application settings, sourced from environment variables (see `.env.example`)."""

from functools import lru_cache
import os
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in `.env.example`. Fine for local dev and tests; anywhere
# else it means the signing key is whatever anyone who cloned the repo can read,
# so tokens can be forged for any account. See the validator below.
PLACEHOLDER_JWT_SECRET = "change-me-to-a-long-random-value"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PROJECT_NAME: str = "Tamakkan Backend"
    # Local development only: makes startup refuse any production database/LiveKit
    # configuration. See app/core/local_guard.py.
    LOCAL_DEV_MODE: bool = False
    # Docker containers have their own loopback. This explicit local-only flag
    # allows the guarded V2 Mongo service name, never an arbitrary host.
    ALLOW_DOCKER_LOCAL_MONGO: bool = False
    ENVIRONMENT: str = "development"
    API_PREFIX: str = "/api"

    MONGODB_URI: str = "mongodb://localhost:27017"
    # Required, no default: a deployment must name its existing database explicitly,
    # so a missing setting fails at startup instead of silently opening another one.
    MONGODB_DB_NAME: str

    # Auth - filled in as part of the authentication task; declared now so
    # `.env.example` documents everything a new contributor needs to set.
    JWT_SECRET_KEY: str = PLACEHOLDER_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Opt in per deployment. Previously created accounts keep an explicit
    # legacy exemption; new accounts must verify before receiving a session.
    EMAIL_VERIFICATION_ENABLED: bool = False
    EMAIL_VERIFICATION_BASE_URL: str = "http://127.0.0.1:5176"
    EMAIL_VERIFICATION_TTL_HOURS: int = Field(default=24, ge=1, le=168)
    EMAIL_BRAND_NAME: str = "Tamakkan"
    EMAIL_DELIVERY_MODE: Literal["logging", "file", "smtp"] = "logging"
    EMAIL_PREVIEW_DIRECTORY: str = ".tamakkan/mail-preview"
    SMTP_HOST: str = ""
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_SECURITY: Literal["starttls", "ssl"] = "starttls"
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""

    @model_validator(mode="after")
    def _enforce_local_dev_guard(self) -> "Settings":
        if self.LOCAL_DEV_MODE:
            from app.core.local_guard import local_mode_violations

            problems = local_mode_violations(
                mongodb_uri=self.MONGODB_URI,
                mongodb_db_name=self.MONGODB_DB_NAME,
                environment=self.ENVIRONMENT,
                livekit_url=getattr(self, "LIVEKIT_URL", "") or "",
                livekit_agent_name=getattr(self, "LIVEKIT_AGENT_NAME", "") or "",
                email_delivery_mode=self.EMAIL_DELIVERY_MODE,
                allow_docker_local_mongo=self.ALLOW_DOCKER_LOCAL_MONGO,
            )
            if problems:
                raise ValueError("LOCAL_DEV_MODE safeguards: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_email_delivery(self) -> "Settings":
        if self.EMAIL_DELIVERY_MODE == "file" and self.ENVIRONMENT != "development":
            raise ValueError("File email previews are for local development only.")
        if self.EMAIL_DELIVERY_MODE == "smtp":
            from pydantic import TypeAdapter, EmailStr
            if not self.SMTP_HOST or not self.SMTP_FROM_EMAIL:
                raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL are required for SMTP delivery.")
            TypeAdapter(EmailStr).validate_python(self.SMTP_FROM_EMAIL)
            if bool(self.SMTP_USERNAME) != bool(self.SMTP_PASSWORD):
                raise ValueError("Set both SMTP_USERNAME and SMTP_PASSWORD, or neither.")
        if self.EMAIL_VERIFICATION_ENABLED:
            if self.EMAIL_DELIVERY_MODE == "logging":
                raise ValueError("Email verification requires SMTP or a local file preview.")
            url = urlsplit(self.EMAIL_VERIFICATION_BASE_URL)
            local_http = (self.ENVIRONMENT == "development" and url.scheme == "http"
                          and url.hostname in {"localhost", "127.0.0.1", "::1"})
            if (not url.hostname or (url.scheme != "https" and not local_http)
                    or url.username or url.password or url.query or url.fragment
                    or url.path not in {"", "/"}):
                raise ValueError("EMAIL_VERIFICATION_BASE_URL must be a trusted HTTPS origin (localhost HTTP is allowed in development).")
        return self

    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # Hard ceiling on any request body, enforced by `BodySizeLimitMiddleware`
    # before FastAPI parses the body (which happens before auth dependencies
    # run - see that module). Must stay comfortably above the largest legitimate
    # upload - since the end-of-week video check-in landed, that's
    # `MAX_END_OF_WEEK_VIDEO_UPLOAD_BYTES` (80 MB), not the older 25 MB audio one -
    # plus multipart framing overhead.
    MAX_REQUEST_BODY_BYTES: int = 90 * 1024 * 1024

    # Recording submissions (Kanban card #6) - audio only, uploaded to
    # Cloudinary. No defaults: an empty/placeholder credential should fail
    # loudly the first time a submission is attempted, not silently no-op.
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Optional - powers the "video length" badge on a topic (see
    # `app.core.youtube.fetch_video_duration_seconds`). Unset means that
    # badge just never shows a video length; nothing else depends on it.
    YOUTUBE_API_KEY: str = ""

    # Transcript pipeline (Kanban card #11) - same "fail loudly, not silently"
    # reasoning as the Cloudinary settings above.
    GEMINI_API_KEY: str = ""
    # How long a topic may sit in `extracting` before its run is treated as
    # abandoned and the topic can be reclaimed - see
    # `TranscriptPipelineService._extraction_is_abandoned`. Must exceed the
    # slowest realistic generation (queued behind other extractions, plus
    # retries), or a live run could be reclaimed underneath itself.
    TRANSCRIPT_EXTRACTION_TIMEOUT_MINUTES: int = 30
    # Local CPU ASR for uploaded videos when they contain no subtitle track.
    # The model is downloaded/cached by faster-whisper on first use.
    LOCAL_ASR_MODEL: str = "base.en"

    # AI cost controls (Slice B) - see `app.core.ai_cost_control`. Unset
    # pricing means a generation's `estimated_cost_usd` is recorded as
    # `None` rather than a guessed number - these are never invented here
    # and must be set to the project's own currently-verified provider
    # pricing before cost figures can be trusted for anything financial.
    GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD: Optional[float] = None
    GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD: Optional[float] = None
    # A single lesson accumulating this many drafting/regeneration/review
    # attempts within 24h almost certainly means a stuck UI or a runaway
    # script, not normal authoring - conservative, generous default; raise
    # it for a real bulk-authoring workflow rather than removing the guard.
    MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY: int = 40
    # How long an "in_progress" generation claim may stand before a later
    # identical request is allowed to reclaim it (the process that held it
    # crashed/was redeployed) - same reclaim-on-timeout reasoning as
    # `TRANSCRIPT_EXTRACTION_TIMEOUT_MINUTES`, just much shorter, since a
    # drafting call is one request-response, not a multi-stage pipeline.
    AI_GENERATION_STALE_CLAIM_MINUTES: int = 5
    # Hard ceiling on how much approved source text is actually sent into a
    # content/exercise drafting prompt - independent of whatever cap already
    # applies when the text was written/approved (see
    # `app.schema.limits.MAX_TRANSCRIPT_LENGTH`, 300,000 chars). That cap
    # exists to make storage/approval sane; this one exists to keep a single
    # drafting call's input tokens, and therefore its cost, bounded.
    MAX_DRAFTING_SOURCE_CHARS: int = 20_000

    # Live "Discuss with Mariam" sessions (Phase 4) - this backend only mints
    # LiveKit room/access tokens (`LiveSessionService.start_session`); the
    # actual conversation runs in the separate `livekit_agent` worker, a
    # standalone deployable with its own settings (`livekit_agent/config.py`)
    # that does not read this file. No defaults, same "fail loudly, not
    # silently" reasoning as Cloudinary/Gemini.
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""

    # The agent name `start_session` dispatches into the room - must match
    # the `livekit_agent` worker's own `LIVEKIT_AGENT_NAME` (its `AGENT_NAME`
    # in `agent.py`) for LiveKit to route the job to it at all. Defaulted the
    # same on both sides so production needs no override. LiveKit dispatches
    # by this name across *every* worker registered under it, regardless of
    # who deployed that worker - so a developer running `livekit_agent`
    # locally with the production name would have real student sessions
    # dispatched to their own machine instead of (or racing) the deployed
    # worker. Give a local run its own name (e.g. "mariam-agent-local", set
    # identically here and in that worker's `.env`) so the two never overlap.
    LIVEKIT_AGENT_NAME: str = "mariam-agent"
    # Voice Practice: the hosted voice-only agent (LiveKit Agent Builder),
    # dispatched instead of `LIVEKIT_AGENT_NAME` for `mode="voice_practice"`
    # sessions. Same LiveKit project/credentials as Mariam. Defaults to the
    # dev agent so local runs need no env change; override per environment.
    LIVEKIT_VOICE_AGENT_NAME: str = "tamakkan-voice-dev"

    # Shared secret the `livekit_agent` worker sends back on its own
    # HTTP calls into this backend (marking a session connected/ended) - see
    # `app.core.security.require_internal_agent`. That worker has no other
    # way to authenticate: it's not a User and mints no JWT. No default, same
    # "fail loudly, not silently" reasoning as Cloudinary/Gemini - must match
    # `livekit_agent`'s own `AGENT_INTERNAL_API_KEY`.
    AGENT_INTERNAL_API_KEY: str = ""

    # SpatialReal (the NEW platform: api.spatialreal.com). The API key stays on this server: it is exchanged for a
    # short-lived session token per Avatar Practice session (`SpatialRealTokenService`), and only that token plus the
    # two public ids below reach the browser. The id pair must be the same app the `livekit_agent` worker uses
    # (its own SPATIALREAL_* secrets) - the browser renders the avatar that agent drives. Empty = not configured:
    # `POST /sessions/{id}/avatar-token` answers 503 instead of guessing.
    SPATIALREAL_API_KEY: str = ""
    SPATIALREAL_APP_ID: str = ""
    SPATIALREAL_AVATAR_ID: str = ""

    @model_validator(mode="after")
    def _reject_placeholder_secret_outside_development(self) -> "Settings":
        """Refuse to start with the repo's published signing key anywhere but
        local dev. This is a fail-fast on purpose: the alternative is a
        deployment that boots and looks perfectly healthy while every token it
        issues can be forged by anyone who has read the repo.
        """
        if self.ENVIRONMENT != "development" and self.JWT_SECRET_KEY == PLACEHOLDER_JWT_SECRET:
            raise ValueError(
                f"JWT_SECRET_KEY is still the placeholder from .env.example while "
                f"ENVIRONMENT={self.ENVIRONMENT!r}. Set it to a long random value "
                f"(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(64))\"`)."
            )
        return self

    @model_validator(mode="after")
    def _reject_blank_agent_name(self) -> "Settings":
        """`LIVEKIT_AGENT_NAME=` (present but empty) in `.env` is a real,
        easy-to-make mistake distinct from leaving the line out entirely -
        pydantic-settings treats an empty env var as an explicit override,
        not "use the default", so it would dispatch every session to agent
        name "" instead of quietly falling back to "mariam-agent". Fail at
        startup rather than have every `start_session` call silently mint a
        token nothing ever picks up.
        """
        for name in ("LIVEKIT_AGENT_NAME", "LIVEKIT_VOICE_AGENT_NAME"):
            if not getattr(self, name):
                raise ValueError(
                    f"{name} is set to an empty string - remove the line from "
                    ".env entirely to use the default, or set a real agent name."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor - use as a FastAPI dependency so tests can
    override it (`app.dependency_overrides[get_settings] = ...`) instead of
    mutating environment variables.
    """
    return Settings(_env_file=os.environ.get("TAMAKKAN_ENV_FILE", ".env"))
