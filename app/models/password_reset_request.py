"""A student's request for their instructor to reset their password.

Distinct from `PasswordResetToken` (the self-service, emailed-link flow):
students can't use that flow at all anymore (see `AuthService.request_password_reset`
and `app/api/v1/me.py`) - per the platform plan §3.5's instructor-driven model,
extended here to passwords too. Whoever resolves this sets a new password for
the student directly, the same way `admin_create_student` sets an initial one
- there's no link for the student to click, so no token/expiry here either.

`student_id` duplicates `student.id` as a plain, queryable field: Beanie Link
queries are less certain to behave a given way across versions than a plain
indexed string, and the query this needs ("this student's pending requests")
is simple enough that the small duplication is worth not guessing.
"""

from datetime import datetime
from typing import Optional

from beanie import Indexed, Link

from app.models.base import BaseDocument
from app.models.user import User


class PasswordResetRequest(BaseDocument):
    student: Link[User]
    student_id: Indexed(str)
    resolved: bool = False
    resolved_by: Optional[Link[User]] = None
    resolved_at: Optional[datetime] = None

    class Settings:
        name = "password_reset_requests"
