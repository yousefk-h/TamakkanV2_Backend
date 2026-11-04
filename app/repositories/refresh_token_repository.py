from datetime import timedelta
from typing import Optional

from app.core.config import get_settings
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow
from app.utils.tokens import generate_token, hash_token


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    def __init__(self) -> None:
        super().__init__(RefreshToken)

    async def create_for_user(self, user: User) -> tuple[RefreshToken, str]:
        """Returns the stored record and the raw token (only ever available
        here - the caller must hand it to the client and never persist it).
        """
        settings = get_settings()
        raw = generate_token()
        record = RefreshToken(
            user=user,
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
        await self.save(record)
        return record, raw

    async def get_valid_by_raw_token(self, raw_token: str) -> Optional[RefreshToken]:
        record = await RefreshToken.find_one(RefreshToken.token_hash == hash_token(raw_token))
        if record is None or record.revoked or record.expires_at < utcnow():
            return None
        return record

    async def revoke(self, record: RefreshToken) -> None:
        record.revoked = True
        await self.save(record)

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Ends every session for one account. Call this from every path that
        changes a password - a password change that leaves existing refresh
        tokens working doesn't actually lock anyone out.

        Returns how many tokens were revoked, so callers/tests can assert on it.
        """
        records = await RefreshToken.find(
            RefreshToken.user_id == user_id, RefreshToken.revoked == False  # noqa: E712
        ).to_list()
        for record in records:
            await self.revoke(record)
        return len(records)
