"""The first real caller is card #14's audited session-duration raise - see
`app/models/audit_log.py`'s docstring for why nothing wrote here before now.
"""

from app.models.audit_log import AuditLog
from app.repositories.base_repository import BaseRepository


class AuditLogRepository(BaseRepository[AuditLog]):
    def __init__(self) -> None:
        super().__init__(AuditLog)
