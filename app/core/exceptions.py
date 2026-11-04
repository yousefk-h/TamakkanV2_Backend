"""Custom application exceptions and their FastAPI error-response mapping.

Raise one of these from a service or repository; route handlers don't need
try/except blocks - `register_exception_handlers()` converts them into the
right HTTP response once, in one place.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from pymongo.errors import DuplicateKeyError


class AppException(Exception):
    """Base class for every exception this app raises on purpose."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundException(AppException):
    status_code = status.HTTP_404_NOT_FOUND


class UnauthorizedException(AppException):
    status_code = status.HTTP_401_UNAUTHORIZED


class ForbiddenException(AppException):
    status_code = status.HTTP_403_FORBIDDEN


class EmailVerificationRequiredException(ForbiddenException):
    def __init__(self) -> None:
        super().__init__("Verify your email address before signing in.")


class ConflictException(AppException):
    status_code = status.HTTP_409_CONFLICT


class UnprocessableException(AppException):
    """The request was well-formed but its content violates a rule that isn't
    expressible as a Pydantic schema constraint - e.g. an uploaded recording
    that turns out to be longer than the 2-minute limit once its actual
    duration is known.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT


class UpstreamServiceException(AppException):
    """A downstream service (Cloudinary, ...) failed after retries. Distinct
    from a plain 500: the request itself was fine, and the honest answer is
    "try again," not "something in this codebase is broken."
    """

    status_code = status.HTTP_502_BAD_GATEWAY


class ServiceUnavailableException(AppException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        content = {"detail": exc.message}
        if isinstance(exc, EmailVerificationRequiredException):
            content["code"] = "email_verification_required"
        return JSONResponse(status_code=exc.status_code, content=content, headers={"Cache-Control": "no-store"})

    @app.exception_handler(DuplicateKeyError)
    async def handle_duplicate_key(_: Request, exc: DuplicateKeyError) -> JSONResponse:
        """A unique index rejected a write. Services pre-check where they can
        (`get_by_email` before creating a user), but that check can't see
        soft-deleted rows and can't be atomic against a concurrent request, so
        the index is the real guarantee - and hitting it is a 409, not a 500.

        Deliberately generic: the raw error names the collection, index, and
        conflicting value, which is more than a caller should learn - but
        that's exactly the detail worth keeping server-side.
        """
        logger.warning("Duplicate key on write: {}", exc)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "That value is already in use."},
        )
