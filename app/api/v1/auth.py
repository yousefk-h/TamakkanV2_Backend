from fastapi import APIRouter, BackgroundTasks, Depends, Response

from app.schema.auth import (
    EmailVerificationConfirm,
    EmailVerificationRequest,
    LoginRequest,
    LogoutRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    TokenPair,
)
from app.schema.common import MessageResponse
from app.services.auth_service import AuthService, get_auth_service
from app.services.email_verification_service import EmailVerificationService, get_email_verification_service

router = APIRouter()


@router.post("/auth/email-verification/request", response_model=MessageResponse, status_code=202)
async def request_email_verification(
    body: EmailVerificationRequest, background_tasks: BackgroundTasks, response: Response,
    service: EmailVerificationService = Depends(get_email_verification_service),
) -> MessageResponse:
    response.headers["Cache-Control"] = "no-store"
    background_tasks.add_task(service.request, body.email)
    return MessageResponse(detail="If this account needs verification, an email has been requested. Check your inbox and spam folder.")


@router.post("/auth/email-verification/confirm", response_model=MessageResponse)
async def confirm_email_verification(
    body: EmailVerificationConfirm, response: Response,
    service: EmailVerificationService = Depends(get_email_verification_service),
) -> MessageResponse:
    await service.confirm(body.token)
    response.headers["Cache-Control"] = "no-store"
    return MessageResponse(detail="Email verified. You can now sign in.")


@router.post("/auth/login", response_model=TokenPair)
async def login(body: LoginRequest, service: AuthService = Depends(get_auth_service)) -> TokenPair:
    """Authenticate with email and password. Returns an access token (short-
    lived) and a refresh token (long-lived, revocable).
    """
    return await service.login(body.email, body.password)


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, service: AuthService = Depends(get_auth_service)) -> TokenPair:
    """Exchange a valid refresh token for a new token pair. Rotates the
    refresh token - the one you sent stops working immediately after.
    """
    return await service.refresh(body.refresh_token)


@router.post("/auth/logout", response_model=MessageResponse)
async def logout(body: LogoutRequest, service: AuthService = Depends(get_auth_service)) -> MessageResponse:
    """Revoke a refresh token. Idempotent - succeeds even if the token is
    already invalid, expired, or unknown.
    """
    await service.logout(body.refresh_token)
    return MessageResponse(detail="Logged out.")


@router.post("/auth/password-reset/request", response_model=MessageResponse)
async def request_password_reset(
    body: PasswordResetRequest, service: AuthService = Depends(get_auth_service)
) -> MessageResponse:
    """Request a password-reset email. Always returns the same response -
    whether the account exists, doesn't exist, or belongs to a Student (who
    can't use this flow at all) is never revealed.
    """
    await service.request_password_reset(body.email)
    # Identical response whether or not the account exists (FR-005) - never
    # let this endpoint be used to enumerate registered emails.
    return MessageResponse(detail="If that email is registered, a reset link has been sent.")


@router.post("/auth/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(
    body: PasswordResetConfirm, service: AuthService = Depends(get_auth_service)
) -> MessageResponse:
    """Complete a password reset using the token from the reset email.
    Single-use - a second attempt with the same token is rejected.
    """
    await service.confirm_password_reset(body.token, body.new_password)
    return MessageResponse(detail="Password has been reset.")
