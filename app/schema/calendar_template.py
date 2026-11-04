"""Calendar template CRUD. `CalendarTemplateService` owns the checks that
need a database round-trip (topic existence, Standard/Custom shape rules,
activation uniqueness) - what's checked here is purely structural: a day
number in range and not an end-of-week check-in day, and no two entries claiming
the same day within one submission.
"""

from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.core.challenge import CHALLENGE_TOTAL_DAYS, END_OF_WEEK_CHECKIN_DAYS
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.topic_content import Level
from app.schema.base import BaseSchema
from app.schema.limits import MAX_TITLE_LENGTH
from app.schema.user import MAX_SESSION_DURATION_OVERRIDE_MINUTES

Name = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]


def _must_not_be_an_end_of_week_checkin_day(day: int) -> int:
    if day in END_OF_WEEK_CHECKIN_DAYS:
        raise ValueError(f"day {day} is reserved for the end-of-week check-in, not a lesson")
    return day


Day = Annotated[int, Field(ge=1, le=CHALLENGE_TOTAL_DAYS), AfterValidator(_must_not_be_an_end_of_week_checkin_day)]


class CalendarDayEntryIn(BaseModel):
    day: Day
    topic_ids: Annotated[list[str], Field(min_length=1)]
    # topic_id -> teacher-set minutes overriding the automatic even split -
    # see `CalendarDayEntry.custom_minutes`. A key not in `topic_ids` makes
    # no sense (there's no course there to time), so it's rejected below.
    custom_minutes: dict[str, Annotated[int, Field(gt=0, le=MAX_SESSION_DURATION_OVERRIDE_MINUTES)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _validate_no_duplicate_topics(self) -> "CalendarDayEntryIn":
        if len(set(self.topic_ids)) != len(self.topic_ids):
            raise ValueError(f"day {self.day} lists the same topic more than once")
        unknown = set(self.custom_minutes) - set(self.topic_ids)
        if unknown:
            raise ValueError(f"day {self.day}'s custom_minutes reference topic(s) not scheduled that day: {', '.join(sorted(unknown))}")
        return self


DayList = Annotated[list[CalendarDayEntryIn], Field(max_length=CHALLENGE_TOTAL_DAYS)]


def _must_have_no_duplicate_days(days: list[CalendarDayEntryIn]) -> list[CalendarDayEntryIn]:
    seen = {entry.day for entry in days}
    if len(seen) != len(days):
        raise ValueError("each day may only appear once in a calendar")
    return days


class CalendarTemplateCreate(BaseModel):
    name: Name
    kind: TemplateKind
    # Required for STANDARD, forbidden for CUSTOM - see `_validate_level_matches_kind`.
    level: Optional[Level] = None
    days: DayList = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_level_matches_kind(self) -> "CalendarTemplateCreate":
        if self.kind == TemplateKind.STANDARD and self.level is None:
            raise ValueError("a standard template must specify a level")
        if self.kind == TemplateKind.CUSTOM and self.level is not None:
            raise ValueError("a custom template is freeform and must not specify a level")
        _must_have_no_duplicate_days(self.days)
        return self


class CalendarTemplateUpdate(BaseModel):
    """Every field optional - a partial update only touches what's provided.
    `days`, when provided, is a whole-list replace (the day-grid editor
    always submits its full current state, not a sparse diff).
    """

    name: Optional[Name] = None
    days: Optional[DayList] = None

    @model_validator(mode="after")
    def _validate_no_duplicate_days(self) -> "CalendarTemplateUpdate":
        if self.days is not None:
            _must_have_no_duplicate_days(self.days)
        return self


class CalendarTemplatePublic(BaseSchema):
    name: str
    kind: TemplateKind
    owner_admin_id: str
    level: Optional[Level] = None
    is_active: bool
    days: list[CalendarDayEntry]

    @classmethod
    def from_template(cls, template: CalendarTemplate) -> "CalendarTemplatePublic":
        return cls(
            id=template.id,
            created_at=template.created_at,
            updated_at=template.updated_at,
            name=template.name,
            kind=template.kind,
            owner_admin_id=template.owner_admin_id,
            level=template.level,
            is_active=template.is_active,
            days=template.days,
        )
