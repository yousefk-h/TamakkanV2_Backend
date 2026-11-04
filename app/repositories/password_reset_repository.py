from datetime import timedelta
from typing import Optional

from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow
from app.utils.tokens import generate_token, hash_token

RESET_TOKEN_TTL_MINUTES = 30


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    def __init__(self) -> None:
        super().__init__(PasswordResetToken)

    async def create_for_user(self, user: User) -> tuple[PasswordResetToken, str]:
        raw = generate_token()
        record = PasswordResetToken(
            user=user,
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
        )
        await self.save(record)
        return record, raw

    async def get_valid_by_raw_token(self, raw_token: str) -> Optional[PasswordResetToken]:
        record = await PasswordResetToken.find_one(PasswordResetToken.token_hash == hash_token(raw_token))
        if record is None or record.used or record.expires_at < utcnow():
            return None
        return record

    async def mark_used(self, record: PasswordResetToken) -> None:
        record.used = True
        await self.save(record)
