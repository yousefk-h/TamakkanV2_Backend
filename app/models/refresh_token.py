"""Refresh tokens are stored hashed, not as stateless JWTs, specifically so
logout and rotation are real: a stateless JWT can't be revoked before it
expires, and "logout" that doesn't revoke anything is not really logout.

Access tokens stay stateless (short-lived JWTs, see `app.core.security`) -
only refresh tokens need this, since they're long-lived enough that
revocability actually matters.
"""

from datetime import datetime

from beanie import Indexed, Link

from app.models.base import BaseDocument
from app.models.user import User


class RefreshToken(BaseDocument):
    user: Link[User]
    # Denormalized copy of `user`'s id so every token for one account can be
    # revoked at once (password change, reset, lockout). Beanie Link fields
    # don't support equality queries - querying `RefreshToken.user.id == ...`
    # against the stored DBRef silently matches nothing - so the id has to be
    # its own indexed field. Same pattern as `User.created_by_id`.
    user_id: Indexed(str)
    token_hash: Indexed(str, unique=True)
    expires_at: datetime
    revoked: bool = False

    class Settings:
        name = "refresh_tokens"
