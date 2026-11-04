"""This worker's own settings, sourced from environment variables (see
`.env.example` in this same directory).

Deliberately does NOT import `app.core.config`: this package is a separate
deployable (see the package docstring in `agent.py`) - when LiveKit Cloud
builds and deploys it, only this directory's contents are guaranteed to be
present, not the sibling `app/` package. It has no database of its own -
whatever session state it needs to read comes from its job's dispatch
metadata, and whatever it needs to write goes back to the main backend over
plain HTTP (`BACKEND_API_URL`, authenticated with `AGENT_INTERNAL_API_KEY`).
"""

from functools import lru_cache
import os

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The main backend's own base URL (e.g. http://localhost:8000/api/v1) -
    # this worker calls it to report a session connecting/ending. Must match
    # a backend whose own AGENT_INTERNAL_API_KEY equals the value below.
    BACKEND_API_URL: str = "http://localhost:8000/api/v1"
    # Shared secret sent as the `X-Internal-Api-Key` header on every call to
    # the backend above - see `app.core.security.require_internal_agent`.
    AGENT_INTERNAL_API_KEY: str = ""

    # OpenAI's Realtime API is this worker's primary conversation model;
    # Ultravox is the fallback (see `agent.py`'s `RealtimeModelFallbackAdapter`
    # construction) - both keys are required in any real deployment.
    OPENAI_API_KEY: str = ""
    ULTRAVOX_API_KEY: str = ""

    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""

    # The name this worker registers under with LiveKit Cloud - must match
    # the main backend's own `LIVEKIT_AGENT_NAME` (`app.core.config`) for
    # `LiveSessionService.start_session`'s dispatch to reach this worker at
    # all. Defaulted the same on both sides so production needs no override.
    # LiveKit dispatches by this name across *every* worker registered under
    # it, regardless of who deployed that worker - running this worker
    # locally with the production name means real student sessions get
    # dispatched to your own machine instead of (or racing) the deployed
    # worker. Override this to something like "mariam-agent-local" in your
    # own `.env` for local dev, matching the same override on the backend.
    LIVEKIT_AGENT_NAME: str = "mariam-agent"

    SPATIALREAL_API_KEY: str = ""
    SPATIALREAL_APP_ID: str = ""
    # Fixed avatar id for Mariam - not per-student/per-topic configurable in
    # this phase (see the platform plan's "Out of scope": no selectable
    # personas yet).
    SPATIALREAL_AVATAR_ID: str = "ca9c5c22-6dba-4b59-ae3b-d26066f8c017"

    # How long a session may sit with no user input before it's auto-ended
    # (spec FR-036).
    MARIAM_IDLE_TIMEOUT_SECONDS: int = 60

    @model_validator(mode="after")
    def _reject_blank_agent_name(self) -> "Settings":
        """`LIVEKIT_AGENT_NAME=` (present but empty) in `.env` is a real,
        easy-to-make mistake distinct from leaving the line out entirely -
        pydantic-settings treats an empty env var as an explicit override,
        not "use the default", so this worker would register under agent
        name "" instead of quietly falling back to "mariam-agent", and never
        receive a single dispatched job. Fail at startup rather than run
        indefinitely doing nothing.
        """
        if not self.LIVEKIT_AGENT_NAME:
            raise ValueError(
                "LIVEKIT_AGENT_NAME is set to an empty string - remove the line from "
                ".env entirely to use the default, or set a real agent name."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("TAMAKKAN_ENV_FILE", ".env"))
