"""Platform-wide Avatar/session defaults an Administrator can change at
runtime (FR-085) - e.g. `max_concurrent_sessions`, launched at 50 and raised
later with no code change.

Genuinely global, singleton state (unlike the per-student fields on `User`),
so it earns its own collection. No fetch-or-create helper yet - that's part of
Kanban card #31, once the config panel that reads/writes it actually exists.
"""

from app.models.base import BaseDocument


class PlatformAvatarConfig(BaseDocument):
    default_idle_timeout_seconds: int = 90
    default_avatar_vendor: str = ""
    max_concurrent_sessions: int = 50
    reconnect_max_attempts: int = 3
    reconnect_backoff_seconds: list[int] = [1, 2, 4]
    recording_enabled_by_default: bool = False  # always False - the live call is never recorded

    class Settings:
        name = "platform_avatar_configs"
