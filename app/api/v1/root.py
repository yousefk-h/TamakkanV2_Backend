"""Root-only: creating Administrator accounts and resetting their passwords
(e.g. when one is locked out). No dashboard concept for Root - reachable only
via the OpenAPI/Swagger UI for now, same as any other FastAPI route; nothing
here is hidden from it or special-cased.

Root does not inherit Administrator's own endpoints (creating a Student,
etc.) - role checks in this codebase are flat/exact-match, not a hierarchy.
That's a deliberate scope boundary, not an oversight: build it if/when it's
actually asked for.
"""

from fastapi import APIRouter, Depends

from app.core.security import require_root
from app.models.user import User
from app.schema.user import CreateAccountRequest, ResetPasswordRequest, UserPublic
from app.services.user_service import UserService, get_user_service

router = APIRouter()


@router.post("/root/admins", response_model=UserPublic)
async def create_administrator(
    body: CreateAccountRequest,
    root: User = Depends(require_root),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Root only. Create a new Administrator (Instructor) account outright -
    email verification is required before sign-in when enabled.
    """
    user = await service.root_create_administrator(body.email, body.password, body.full_name, created_by=root)
    return UserPublic.from_user(user)


@router.post("/root/admins/{admin_id}/reset-password", response_model=UserPublic)
async def reset_administrator_password(
    admin_id: str,
    body: ResetPasswordRequest,
    _root: User = Depends(require_root),
    service: UserService = Depends(get_user_service),
) -> UserPublic:
    """Root only. Directly set a new password for an Administrator - e.g.
    when they're locked out and can't use the emailed self-service reset.
    """
    user = await service.root_reset_administrator_password(admin_id, body.new_password)
    return UserPublic.from_user(user)
