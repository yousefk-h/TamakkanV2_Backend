"""Password reset tokens - same hashed-at-rest pattern as `RefreshToken`, for
the same reason: never store a secret anyone could replay if the database
leaked.
"""

from datetime import datetime

from beanie import Indexed, Link

from app.models.base import BaseDocument
from app.models.user import User


class PasswordResetToken(BaseDocument):
    user: Link[User]
    # Denormalized, for the same reason as `RefreshToken.user_id`: it's what
    # lets the owner be re-loaded through `UserRepository` (which filters
    # `is_deleted`) instead of `Link.fetch()`, which returns the Link itself -
    # not `None` - when the target document is gone.
    user_id: Indexed(str)
    token_hash: Indexed(str, unique=True)
    expires_at: datetime
    used: bool = False

    class Settings:
        name = "password_reset_tokens"
