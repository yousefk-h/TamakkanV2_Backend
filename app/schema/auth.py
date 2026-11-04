"""Auth request/response bodies.

Every field here arrives on an *unauthenticated* route, so the length caps are
not cosmetic: FastAPI parses the body before dependencies run, so these are
reached by anyone who can open a socket.
"""

from typing import Annotated

from pydantic import BaseModel, EmailStr, Field

from app.schema.limits import MAX_EMAIL_LENGTH, MAX_TOKEN_LENGTH
from app.schema.password import ExistingPassword, NewPassword

Email = Annotated[EmailStr, Field(max_length=MAX_EMAIL_LENGTH)]
OpaqueToken = Annotated[str, Field(max_length=MAX_TOKEN_LENGTH)]


class LoginRequest(BaseModel):
    email: Email
    password: ExistingPassword


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: OpaqueToken


class LogoutRequest(BaseModel):
    refresh_token: OpaqueToken


class PasswordResetRequest(BaseModel):
    email: Email


class PasswordResetConfirm(BaseModel):
    token: OpaqueToken
    new_password: NewPassword


class EmailVerificationRequest(BaseModel):
    email: Email


class EmailVerificationConfirm(BaseModel):
    token: Annotated[str, Field(min_length=32, max_length=256)]
