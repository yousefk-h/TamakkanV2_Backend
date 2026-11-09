"""Four roles: Root sits above everyone and exists specifically to create
Administrator accounts and reset their passwords when they're locked out (no
dashboard concept - reachable only via the API/Swagger for now).

Administrator is the Center Admin: unrestricted within this one Tamakkan
deployment (one center) - sees/manages every Teacher, Student and Topic in it,
same as Root but scoped to this center rather than the whole system. This is
new (PROMPT-047); before it, "Administrator" *was* the teacher role, and most
ownership checks in this codebase were written comparing against an
Administrator's own id. Those checks now compare against a TEACHER's id
instead - an Administrator bypasses them explicitly (see
`UserService.get_own_student`, `TopicContentService.get_own_topic`, etc.) -
so Teacher isolation (one teacher's students/topics hidden from another) is
preserved exactly as it was, just renamed and with the Center Admin carved out
above it.

Role checks stay flat/exact-match (`require_role(Role.TEACHER)` does not also
accept Administrator or Root) - a route that both should reach uses
`require_teacher_or_administrator` (`app.core.security`) explicitly, to avoid
quietly building a role-hierarchy/inheritance system nobody asked for.

Plan/entitlement fields live directly on `User` rather than in a separate
`SessionEntitlement` collection: they're strictly 1:1 with a student and have
no lifecycle of their own, so a join would just be overhead. (An earlier,
higher-level version of the platform plan sketched a separate collection for
this - this is a deliberate simplification made while implementing, not an
oversight.)
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional

from beanie import Indexed
from pydantic import EmailStr, Field

from app.models.base import BaseDocument
from app.models.topic_content import Level


class Role(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMINISTRATOR = "administrator"
    ROOT = "root"


class PlanType(str, Enum):
    STANDARD = "standard"
    PRO = "pro"


class User(BaseDocument):
    email: Indexed(EmailStr, unique=True)
    password_hash: str
    full_name: str
    role: Role = Role.STUDENT
    is_active: bool = True

    # Missing fields on existing accounts mean 'not required', never 'verified'.
    email_verification_required: bool = False
    email_verified_at: Optional[datetime] = None
    email_verification_hash: Annotated[Optional[str], Indexed()] = None
    email_verification_email: Optional[str] = None
    email_verification_expires_at: Optional[datetime] = None
    email_verification_sent_at: Optional[datetime] = None
    email_verification_window_at: Optional[datetime] = None
    email_verification_send_count: int = 0
    email_verification_delivery: Literal["not_sent", "sent", "failed"] = "not_sent"

    @property
    def email_verification_status(self) -> Literal["verified", "pending", "not_required"]:
        if self.email_verified_at is not None:
            return "verified"
        return "pending" if self.email_verification_required else "not_required"

    # Who created this account (Root for an Administrator, an Administrator
    # for a Student) - None for a seeded Root account, which has no creator.
    # It decides which instructor may reset a student's password, and who gets
    # notified when one asks.
    #
    # A plain indexed id, deliberately not a `Link[User]`. A Link can't be
    # queried by equality (`User.created_by.id == x` matches nothing against
    # the stored DBRef), and `Link.fetch()` returns the Link itself rather than
    # `None` when the target is gone, so a `is None` guard on it is dead code
    # that turns a dangling reference into an AttributeError. Carrying both a
    # Link and an id meant two sources of truth for one fact, with different
    # readers, and nothing keeping them in step.
    #
    # `Optional[Indexed(str)]` (function-call form) looks equivalent to this
    # but isn't - Beanie's index detector only recognizes a bare `Indexed(...)`
    # type or this `Annotated` form, not a `Union` wrapping one, so that form
    # silently builds no index at all.
    created_by_id: Annotated[Optional[str], Indexed()] = None

    # --- Student-only fields (role == STUDENT); left at their defaults and
    # unused for Administrator accounts. ---
    plan: Optional[PlanType] = None
    program_id: Annotated[Optional[str], Indexed()] = None
    program_level_id: Annotated[Optional[str], Indexed()] = None
    # Denormalized engine difficulty for compatibility/scoring. The Program
    # stage id above is authoritative for Program enrollment.
    assigned_level: Optional[Level] = None
    # Pro: hand-picked, any level. A plain id list, not `list[Link[TopicContent]]`
    # - same reasoning as `created_by_id` above: Links can't be queried/checked
    # for containment reliably, and this field was never actually read or
    # written anywhere before card #14, so there was no existing behavior to
    # preserve by keeping the Link form.
    assigned_topic_ids: list[str] = Field(default_factory=list)
    # Pro only: which `CalendarTemplate` (kind=custom) this student follows -
    # `assigned_topic_ids` above is derived from it at assign-plan time (the
    # template's own topic set), so the two can't drift apart. `None` means
    # this Pro student hasn't been given a calendar yet.
    assigned_plan_template_id: Annotated[Optional[str], Indexed()] = None
    # Day 1 of this student's calendar - set by the assigning admin
    # (`UserService.assign_plan`), not derived from when they actually start
    # practicing. `None` means no plan has ever been assigned (or the
    # program hasn't started - `ProgressService` treats a future date as
    # "everything still locked" rather than a special case).
    program_start_date: Optional[datetime] = None
    max_session_duration_minutes: int = 15
    duration_override_minutes: Optional[int] = None  # Pro only, instructor-granted, audited

    class Settings:
        name = "users"
        indexes = [
            # An admin's own roster, filtered by role and excluding soft-deleted
            # rows - the one query every student-list/search/count path makes.
            [("created_by_id", 1), ("role", 1), ("is_deleted", 1)],
        ]
