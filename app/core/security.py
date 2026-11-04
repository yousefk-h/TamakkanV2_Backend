"""Password hashing, JWT access tokens, and the `get_current_user` /
`require_role` dependencies every protected route uses.

Hashing uses `bcrypt` directly, not `passlib` - passlib is unmaintained and,
as pinned, is actually broken against current `bcrypt` releases (it raises
`ValueError` on its own internal self-test, and its version-sniffing throws
`AttributeError` against bcrypt's newer API). Calling `bcrypt` directly is
simpler anyway - one fewer dependency for the same two functions.

Access tokens are stateless JWTs (short-lived, so a suspended/deactivated
account's access dies on its own within `ACCESS_TOKEN_EXPIRE_MINUTES` - there
is no server-side access-token revocation, which is the standard, honest
tradeoff of stateless tokens). Refresh tokens are the opposite: long-lived, so
they're stored hashed and are genuinely revocable - see `app.models.refresh_token`
and `app.services.auth_service`.
"""

import hmac
from datetime import timedelta

import bcrypt
from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from loguru import logger

from app.core.config import get_settings
from app.core.exceptions import EmailVerificationRequiredException, ForbiddenException, UnauthorizedException
from app.models.user import Role, User
from app.utils.time import utcnow

_bearer_scheme = HTTPBearer()


# bcrypt rejects anything over 72 *bytes*. Schemas validate that (see
# `app.schema.password`), but these two functions are also called from jobs and
# scripts that don't go through a schema, so they clamp defensively rather than
# letting a `ValueError` escape as a 500.
BCRYPT_MAX_BYTES = 72

# A real bcrypt hash of a value nothing can match, used to spend the same ~240ms
# on a login miss as on a hit - see `AuthService.login`.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-constant-time-login", bcrypt.gensalt()).decode("utf-8")


def _clamp(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_clamp(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_clamp(password), password_hash.encode("utf-8"))


def dummy_verify(password: str) -> None:
    """Burn the same bcrypt time as a real verify, then discard the result."""
    bcrypt.checkpw(_clamp(password), _DUMMY_HASH.encode("utf-8"))


# bcrypt at the default cost is ~240ms of pure CPU, and the three functions
# above are blocking. Called straight from an `async def` service they stall the
# *entire* worker's event loop for that whole time - and because the login miss
# path deliberately burns the same 240ms to stay constant-time, that turns an
# anti-enumeration measure into an unauthenticated DoS: a trickle of bad logins
# holds the loop at 100% and freezes every other in-flight request.
#
# So every request-path caller uses these instead. The sync versions stay for
# `app.jobs.seed_root` and tests, which have no event loop to protect.
async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await run_in_threadpool(verify_password, password, password_hash)


async def dummy_verify_async(password: str) -> None:
    await run_in_threadpool(dummy_verify, password)


def create_access_token(user_id: str, role: Role) -> str:
    settings = get_settings()
    expire = utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "role": role.value, "type": "access", "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            # python-jose validates `exp` when present but doesn't require it,
            # so without this a signed token that simply omits the claim never
            # expires. `require_sub` likewise - a token without `sub` would
            # otherwise reach the lookup below and raise KeyError (a 500)
            # instead of a clean 401.
            #
            # NB: the `options={"require": [...]}` list form is silently
            # ignored by python-jose 3.5.0 - only these per-claim flags work.
            options={"require_exp": True, "require_sub": True},
        )
    except JWTError as exc:
        raise UnauthorizedException("Invalid or expired token") from exc
    if payload.get("type") != "access":
        raise UnauthorizedException("Invalid or expired token")
    return payload


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> User:
    payload = decode_access_token(credentials.credentials)
    user = await User.get(payload["sub"])
    if user is None or not user.is_active or user.is_deleted:
        raise UnauthorizedException("Invalid or expired token")
    require_verified_email(user)
    return user


def require_verified_email(user: User) -> None:
    if user.email_verification_status == "pending":
        raise EmailVerificationRequiredException()


def require_role(*roles: Role):
    """Route dependency factory: `Depends(require_role(Role.ADMINISTRATOR))`."""

    async def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ForbiddenException("You don't have permission to perform this action")
        return user

    return _guard


require_administrator = require_role(Role.ADMINISTRATOR)
require_root = require_role(Role.ROOT)
# The day-to-day teaching routes (topics, calendar templates, classes,
# submissions, sessions, analytics, a Teacher's own students): a Teacher does
# this work now, not the Center Admin alone - see app/models/user.py's Role
# enum. An Administrator may call these too; ownership checks inside each
# service (not this route-level gate) are what give the Administrator
# center-wide reach while keeping a Teacher restricted to their own.
require_teacher_or_administrator = require_role(Role.TEACHER, Role.ADMINISTRATOR)


async def require_internal_agent(request: Request, x_internal_api_key: str = Header(default="")) -> None:
    """Gate for the handful of routes the `livekit_agent` worker calls
    directly (it's not a `User` and mints no JWT, so it can't use
    `get_current_user`). `hmac.compare_digest` avoids leaking the secret's
    length/prefix through a timing side-channel, same reasoning as every
    other secret comparison in this app.
    """
    settings = get_settings()
    if not settings.AGENT_INTERNAL_API_KEY or not hmac.compare_digest(x_internal_api_key, settings.AGENT_INTERNAL_API_KEY):
        # A wrong/missing key on the agent's callbacks leaves sessions stuck in "connecting" with no transcript and no
        # report; say so (the key itself is never logged).
        reason = (
            "AGENT_INTERNAL_API_KEY is not set on the backend" if not settings.AGENT_INTERNAL_API_KEY
            else "no key sent" if not x_internal_api_key else "key does not match the backend's"
        )
        logger.warning("internal agent call rejected ({}): {} {}", reason, request.method, request.url.path)
        raise UnauthorizedException("Invalid or missing internal API key")
