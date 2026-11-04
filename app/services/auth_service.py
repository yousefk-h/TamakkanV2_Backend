"""Login, refresh, logout, and password reset. Registration/account creation
is in `user_service` - this class is strictly "prove who you are."
"""

from app.core.email import EmailSender, get_email_sender
from app.core.exceptions import UnauthorizedException
from app.core.security import (
    create_access_token,
    dummy_verify_async,
    hash_password_async,
    require_verified_email,
    verify_password_async,
)
from app.models.user import Role, User
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schema.auth import TokenPair


class AuthService:
    def __init__(
        self,
        user_repository: UserRepository,
        refresh_token_repository: RefreshTokenRepository,
        password_reset_repository: PasswordResetTokenRepository,
        email_sender: EmailSender,
    ) -> None:
        self.user_repository = user_repository
        self.refresh_token_repository = refresh_token_repository
        self.password_reset_repository = password_reset_repository
        self.email_sender = email_sender

    async def _issue_token_pair(self, user: User) -> TokenPair:
        require_verified_email(user)
        access_token = create_access_token(user.id, user.role)
        _record, raw_refresh = await self.refresh_token_repository.create_for_user(user)
        return TokenPair(access_token=access_token, refresh_token=raw_refresh)

    async def login(self, email: str, password: str) -> TokenPair:
        user = await self.user_repository.get_by_email(email)
        # Same generic error whether the email doesn't exist or the password is
        # wrong - never reveal which (FR-003).
        #
        # The miss path deliberately still runs a bcrypt verify against a dummy
        # hash. Skipping it would return in ~4ms where a real account takes
        # ~240ms (bcrypt is slow on purpose), and that 57x gap tells an attacker
        # which emails are registered just as plainly as a different error
        # message would.
        if user is None or not user.is_active:
            await dummy_verify_async(password)
            raise UnauthorizedException("Incorrect email or password")
        if not await verify_password_async(password, user.password_hash):
            raise UnauthorizedException("Incorrect email or password")
        return await self._issue_token_pair(user)

    async def refresh(self, raw_refresh_token: str) -> TokenPair:
        record = await self.refresh_token_repository.get_valid_by_raw_token(raw_refresh_token)
        if record is None:
            raise UnauthorizedException("Invalid or expired refresh token")

        # Re-load through the repository rather than `record.user.fetch()`:
        # the repository filters `is_deleted`, and `Link.fetch()` returns the
        # Link itself (not `None`) when the target is gone, so an `is None`
        # guard on it is dead code that turns a dangling reference into an
        # AttributeError 500 instead of a 401.
        user = await self.user_repository.get_by_id(record.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedException("Invalid or expired refresh token")

        # Rotate: the old refresh token is single-use, so a stolen-and-replayed
        # token stops working the moment the legitimate client refreshes.
        await self.refresh_token_repository.revoke(record)
        return await self._issue_token_pair(user)

    async def logout(self, raw_refresh_token: str) -> None:
        record = await self.refresh_token_repository.get_valid_by_raw_token(raw_refresh_token)
        if record is not None:
            await self.refresh_token_repository.revoke(record)
        # No error on an already-invalid/unknown token - logout is idempotent.

    async def request_password_reset(self, email: str) -> None:
        user = await self.user_repository.get_by_email(email)
        # Students don't get a self-service reset token at all (platform plan
        # §3.5 - they go through their instructor, see
        # `PasswordResetRequestService`). Same generic message either way -
        # "no such account" and "this is a student" must look identical, or
        # this endpoint becomes a way to fingerprint which accounts are
        # students.
        if user is None or user.role == Role.STUDENT or not user.is_active:
            return

        _record, raw_token = await self.password_reset_repository.create_for_user(user)
        # The raw token exists only here - only its hash is stored - so if it
        # isn't handed off now it can never be recovered, and the endpoint
        # would report "a reset link has been sent" having sent nothing.
        await self.email_sender.send(
            to=user.email,
            subject="Reset your Tamakkan password",
            body=f"Use this token to reset your password: {raw_token}",
        )

    async def confirm_password_reset(self, raw_token: str, new_password: str) -> None:
        record = await self.password_reset_repository.get_valid_by_raw_token(raw_token)
        if record is None:
            raise UnauthorizedException("Invalid or expired reset token")

        # Via the repository, not `record.user.fetch()` - see the note in
        # `refresh()`. This is also what enforces `is_deleted` here.
        user = await self.user_repository.get_by_id(record.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedException("Invalid or expired reset token")

        user.password_hash = await hash_password_async(new_password)
        await self.user_repository.save(user)
        await self.password_reset_repository.mark_used(record)
        # A reset is the remedy for "someone else is in my account" - it has to
        # end their sessions too, or the attacker just keeps refreshing.
        await self.refresh_token_repository.revoke_all_for_user(user.id)


def get_auth_service() -> AuthService:
    return AuthService(
        UserRepository(),
        RefreshTokenRepository(),
        PasswordResetTokenRepository(),
        get_email_sender(),
    )
