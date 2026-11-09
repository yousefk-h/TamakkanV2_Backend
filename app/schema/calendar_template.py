"""Calendar template CRUD. `CalendarTemplateService` owns the checks that
need a database round-trip (topic existence, Standard/Custom shape rules,
activation uniqueness) - what's checked here is purely structural: a day
number in range and not an end-of-week check-in day, and no two entries claiming
the same day within one submission.
"""

from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator

from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.topic_content import Level
from app.schema.base import BaseSchema
from app.schema.limits import MAX_TITLE_LENGTH
from app.schema.user import MAX_SESSION_DURATION_OVERRIDE_MINUTES

Name = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]


Day = Annotated[int, Field(ge=1, le=90)]


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


DayList = Annotated[list[CalendarDayEntryIn], Field(max_length=90)]


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
    duration_days: Annotated[int, Field(ge=1, le=90)] = 30
    # API clients that predate configurable calendars keep the original
    # weekly check-in/rest pattern. The new builder always sends the teacher's
    # explicit selection (including an empty list).
    rest_days: list[Day] = Field(default_factory=lambda: [5, 12, 19, 26])
    days: DayList = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_level_matches_kind(self) -> "CalendarTemplateCreate":
        if self.kind == TemplateKind.STANDARD and self.level is None:
            raise ValueError("a standard template must specify a level")
        if self.kind == TemplateKind.CUSTOM and self.level is not None:
            raise ValueError("a custom template is freeform and must not specify a level")
        _must_have_no_duplicate_days(self.days)
        _validate_schedule_shape(self.duration_days, self.rest_days, self.days)
        return self


class CalendarTemplateUpdate(BaseModel):
    """Every field optional - a partial update only touches what's provided.
    `days`, when provided, is a whole-list replace (the day-grid editor
    always submits its full current state, not a sparse diff).
    """

    name: Optional[Name] = None
    duration_days: Optional[Annotated[int, Field(ge=1, le=90)]] = None
    rest_days: Optional[list[Day]] = None
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
    program_id: Optional[str] = None
    program_level_id: Optional[str] = None
    cohort_id: Optional[str] = None
    level: Optional[Level] = None
    is_active: bool
    duration_days: int
    rest_days: list[int]
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
            program_id=template.program_id,
            program_level_id=template.program_level_id,
            cohort_id=template.cohort_id,
            level=template.level,
            is_active=template.is_active,
            duration_days=template.duration_days,
            rest_days=template.rest_days,
            days=template.days,
        )


def _validate_schedule_shape(duration_days: int, rest_days: list[int], days: list[CalendarDayEntryIn]) -> None:
    if len(rest_days) != len(set(rest_days)):
        raise ValueError("a rest day may only be listed once")
    outside = [day for day in rest_days if day > duration_days]
    if outside:
        raise ValueError(f"rest days must be within the {duration_days}-day program")
    invalid_lessons = [entry.day for entry in days if entry.day > duration_days or entry.day in rest_days]
    if invalid_lessons:
        raise ValueError("lesson days must be inside the program and cannot be rest days")
