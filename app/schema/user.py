from datetime import date
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.topic_content import Level
from app.models.user import PlanType, Role, User
from app.schema.auth import Email
from app.schema.base import BaseSchema
from app.schema.limits import MAX_NAME_LENGTH
from app.schema.password import ExistingPassword, NewPassword

FullName = Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]

# A raise is always an explicit, deliberate admin action (Kanban card #14) -
# this cap exists so a fat-fingered value (99999) can't silently become a
# de facto unlimited session; 3 hours is already far beyond a real lesson.
MAX_SESSION_DURATION_OVERRIDE_MINUTES = 180


class UserPublic(BaseSchema):
    """What a route ever returns for a user - never `password_hash`."""

    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    email_verification_status: Literal["verified", "pending", "not_required"] = "not_required"
    email_verification_delivery: Literal["not_sent", "sent", "failed"] = "not_sent"
    plan: Optional[PlanType] = None
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    assigned_level: Optional[Level] = None
    assigned_topic_ids: list[str] = Field(default_factory=list)
    # Pro only - which calendar template this student follows; see
    # `app.models.calendar_template`.
    assigned_plan_template_id: Optional[str] = None
    # Day 1 of this student's calendar - see `User.program_start_date`.
    program_start_date: Optional[date] = None
    max_session_duration_minutes: int
    duration_override_minutes: Optional[int] = None

    @classmethod
    def from_user(cls, user: User) -> "UserPublic":
        return cls(
            id=user.id,
            created_at=user.created_at,
            updated_at=user.updated_at,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=user.is_active,
            email_verification_status=user.email_verification_status,
            email_verification_delivery=user.email_verification_delivery,
            plan=user.plan,
            program_id=user.program_id,
            program_level_id=user.program_level_id,
            assigned_level=user.assigned_level,
            assigned_topic_ids=user.assigned_topic_ids,
            assigned_plan_template_id=user.assigned_plan_template_id,
            program_start_date=user.program_start_date.date() if user.program_start_date else None,
            max_session_duration_minutes=user.max_session_duration_minutes,
            duration_override_minutes=user.duration_override_minutes,
        )


class UserPage(BaseModel):
    """One page of users (Students or Teachers, depending on the endpoint) plus the total that matched the
    filters (not the page size) - the Users dashboard. Same shape as `SessionPage` (`app.schema.live_session`)."""

    items: list[UserPublic]
    total: int
    page: int
    limit: int


class StudentCounts(BaseModel):
    """The Users dashboard's KPI cards - computed in the database, never by loading every student client-side."""

    total_students: int
    active_students: int


class TeacherCounts(BaseModel):
    """The Users dashboard's KPI cards - computed in the database, never by loading every teacher client-side."""

    total_teachers: int
    active_teachers: int


class TeacherDetail(BaseModel):
    """The Teacher Details drawer - the account plus two lightweight counts, each one database `count()`, not a
    scan over every student/topic."""

    teacher: UserPublic
    student_count: int
    topic_count: int


class CreateAccountRequest(BaseModel):
    """Shared by both account-creation-by-a-tier-above endpoints: Root
    creating an Administrator, and an Administrator creating a Student. Same
    shape either way; the role is decided by which endpoint you call, not by
    a field here. The full instructor invite/onboarding flow (Kanban card #4)
    is separate - this is just enough to bootstrap an account.
    """

    email: Email
    password: NewPassword
    full_name: FullName


class UpdateMe(BaseModel):
    # Capped because this one is Student-writable and does not stay in the
    # database: it is interpolated into the password-reset email an
    # Administrator receives, and into the Cloudinary `public_id` of every
    # recording the student submits. See `app.schema.limits`.
    full_name: Optional[FullName] = None


class ChangePassword(BaseModel):
    current_password: ExistingPassword
    new_password: NewPassword


class ResetPasswordRequest(BaseModel):
    """Root resetting an Administrator's password, or an Administrator
    resetting a Student's - the acting user is already privileged (that's
    what the route's role guard checks), so unlike `ChangePassword` there's no
    current-password check here.
    """

    new_password: NewPassword


class UpdateStudentEmail(BaseModel):
    """An Administrator editing one of their own Students' email - just the
    one field, same reasoning as `ResetPasswordRequest`: nothing here can
    reach role, is_active, or any other account field.
    """

    email: Email


class UpdateTeacherEmail(BaseModel):
    """The Center Admin editing a Teacher's email - same narrow shape as `UpdateStudentEmail`."""

    email: Email


class SetTeacherActive(BaseModel):
    """Activate/deactivate a Teacher account - the Users dashboard's Teachers tab. Deactivating revokes every
    existing session, same as a password reset; it does not touch the Teacher's own students/topics."""

    is_active: bool


class AssignPlan(BaseModel):
    """Standard = one educational Program stage (`program_id` plus
    `program_level_id`), with `assigned_level` retained as its denormalized
    engine difficulty and as a legacy fallback. Pro = one `assigned_plan_template_id`
    pointing at a `kind=custom` template from the admin's own library;
    `UserService.assign_plan` derives `assigned_topic_ids` from that
    template's own topic set, so there's one source of truth instead of two
    that could drift. Standard/Pro are access plans, never Program names. One schema, not two endpoints - the shape is validated
    against whichever `plan` is given, since the two are mutually exclusive
    by design, not by coincidence.
    """

    plan: PlanType
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    assigned_level: Optional[Level] = None
    assigned_plan_template_id: Optional[str] = None
    # Day 1 of the student's calendar - omitted means "starting now."
    # Assigning a plan always (re)sets `User.program_start_date`, whether or
    # not this is given; see `UserService.assign_plan`.
    start_date: Optional[date] = None

    @model_validator(mode="after")
    def _validate_shape_matches_plan(self) -> "AssignPlan":
        if self.plan == PlanType.STANDARD:
            if self.program_id and self.program_level_id is None and self.assigned_level is None:
                raise ValueError("program_level_id is required when assigning a Program")
            if not self.program_id and self.assigned_level is None:
                raise ValueError("assigned_level is required for a legacy Standard assignment")
            if self.assigned_plan_template_id is not None:
                raise ValueError("assigned_plan_template_id is not used for a Standard plan")
        else:
            if self.assigned_plan_template_id is None:
                raise ValueError("assigned_plan_template_id is required for a Pro plan")
            if self.assigned_level is not None:
                raise ValueError("assigned_level is not used for a Pro plan")
            if self.program_level_id is not None:
                raise ValueError("program_level_id is not used for a Pro access plan")
        return self


class RaiseSessionLimit(BaseModel):
    """Pro only - Standard's 15-minute cap is fixed platform-wide, not a
    per-student setting to override.
    """

    duration_override_minutes: int = Field(gt=0, le=MAX_SESSION_DURATION_OVERRIDE_MINUTES)
