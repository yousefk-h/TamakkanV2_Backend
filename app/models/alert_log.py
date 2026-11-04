"""System-level failure alerts for the Administrator dashboard (FR-066/086) -
e.g. a burst of Avatar startup failures. `occurrences` exists so a burst of
identical failures becomes one deduplicated alert, not one row per failure.

Not wired to anything yet - this is schema-only groundwork for Kanban card
#32; the dedup logic itself belongs there, not here.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from app.models.base import BaseDocument
from app.utils.time import utcnow


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertLog(BaseDocument):
    kind: str  # e.g. "avatar_startup_failure_burst", "service_outage"
    severity: AlertSeverity
    occurrences: int = 1
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)
    acknowledged: bool = False

    class Settings:
        name = "alert_logs"
