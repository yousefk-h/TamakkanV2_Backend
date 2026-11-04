"""Every privileged/administrator action gets one audit entry - role changes,
force-ending a session, config changes, "preview as student", entitlement
overrides, and so on.

`action` is a plain string, not a fixed enum, deliberately: new privileged
actions get added across many unrelated features over time (see the Kanban
board), and coupling this model to every feature's enum would be the wrong
kind of coupling. First real caller: card #14's audited session-duration
raise, via `AuditLogRepository` - `action="raise_session_limit"`. More
callers (force-end-session, card #31; "preview as student", card #5) will
each write their own `action` string as they land.
"""

from typing import Any, Optional

from beanie import Link

from app.models.base import BaseDocument
from app.models.user import User


class AuditLog(BaseDocument):
    actor: Link[User]
    action: str
    target_type: str
    target_id: str
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    # No separate `at` field - `created_at` (from BaseDocument) already means
    # exactly this: the moment the audited action happened.

    class Settings:
        name = "audit_logs"
