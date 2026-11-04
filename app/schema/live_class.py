from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.live_class import ClassKind, LiveClass
from app.schema.base import BaseSchema
from app.schema.limits import MAX_LIST_ITEMS, MAX_TITLE_LENGTH, MAX_URL_LENGTH

MIN_DURATION_MINUTES = 10
MAX_DURATION_MINUTES = 180


def _validate_meet_link(value: str) -> str:
    if not value.startswith("https://"):
        raise ValueError("Meet link must be an https:// URL")
    return value


def _to_naive_utc(value: datetime) -> datetime:
    """This codebase's datetimes are naive-UTC throughout (see
    `app.utils.time.utcnow`'s docstring - Mongo/PyMongo have no timezone
    concept). The frontend sends a tz-aware ISO string (`Date.toISOString()`
    always carries "Z"), so an incoming aware value is converted to UTC and
    stripped of tzinfo here, once, rather than every comparison against
    `utcnow()` needing to special-case it.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class CreateLiveClassRequest(BaseModel):
    kind: ClassKind
    title: str = Field(..., min_length=1, max_length=MAX_TITLE_LENGTH)
    scheduled_at: datetime
    duration_minutes: int = Field(..., ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    meet_link: str = Field(..., min_length=1, max_length=MAX_URL_LENGTH)
    # One participant for a 1:1, any number (but still bounded) for a Group -
    # `LiveClassService` enforces the exact-count/plan rules; this is just the
    # request-shape ceiling.
    participant_ids: list[str] = Field(..., min_length=1, max_length=MAX_LIST_ITEMS)

    @field_validator("meet_link")
    @classmethod
    def _check_meet_link(cls, v: str) -> str:
        return _validate_meet_link(v)

    @field_validator("scheduled_at")
    @classmethod
    def _check_scheduled_at(cls, v: datetime) -> datetime:
        return _to_naive_utc(v)


class UpdateLiveClassRequest(BaseModel):
    """Reschedule/retitle/re-link an existing class. Participants aren't
    editable here - changing who's enrolled re-triggers the same plan/count
    validation as creating a class, and there's no card asking for that yet;
    cancel and recreate covers it in the meantime.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=MAX_TITLE_LENGTH)
    scheduled_at: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(None, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    meet_link: Optional[str] = Field(None, min_length=1, max_length=MAX_URL_LENGTH)

    @field_validator("meet_link")
    @classmethod
    def _check_meet_link(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_meet_link(v)

    @field_validator("scheduled_at")
    @classmethod
    def _check_scheduled_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        return v if v is None else _to_naive_utc(v)


class LiveClassPublic(BaseSchema):
    kind: ClassKind
    title: str
    scheduled_at: datetime
    duration_minutes: int
    meet_link: str
    participant_ids: list[str]

    @classmethod
    def from_class(cls, live_class: LiveClass) -> "LiveClassPublic":
        return cls(
            id=live_class.id,
            created_at=live_class.created_at,
            updated_at=live_class.updated_at,
            kind=live_class.kind,
            title=live_class.title,
            scheduled_at=live_class.scheduled_at,
            duration_minutes=live_class.duration_minutes,
            meet_link=live_class.meet_link,
            participant_ids=live_class.participant_ids,
        )
