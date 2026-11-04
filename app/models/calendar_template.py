"""A teacher-authored day-by-day lesson calendar - replaces the earlier
per-topic `challenge_day` pin, which conflated "content" (a `TopicContent`)
with "scheduling" (which day it lands on). A calendar is its own artifact so
the same topic can appear on different days in different calendars, and so a
Pro student's custom calendar can be built once and handed to another
student later without touching any `TopicContent` at all.

Two kinds, not two models: a `STANDARD` template is the one active calendar
for a `(owner_admin_id, level)` pair - every Standard student at that level
under that admin follows it. A `CUSTOM` template is freeform (any topics,
any level mixed together), built for one Pro student at a time but kept in
the admin's own reusable library (`owner_admin_id`) so it can be assigned to
a different student later - see `UserService.assign_plan`.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.models.base import BaseDocument
from app.models.topic_content import Level


class TemplateKind(str, Enum):
    STANDARD = "standard"
    CUSTOM = "custom"


class CalendarDayEntry(BaseModel):
    day: int  # 1..CHALLENGE_TOTAL_DAYS, never a day in END_OF_WEEK_CHECKIN_DAYS
    topic_ids: list[str]  # one day, any number of courses - each tracked/rolled over independently
    # topic_id -> teacher-set minutes for that course, only for topics
    # overridden away from the automatic split. Missing from this map means
    # "split whatever's left of the daily budget evenly" - see
    # `ProgressService.get_topic_session_minutes`. Existing documents get {}
    # via this default, so no migration was needed for this field.
    custom_minutes: dict[str, int] = Field(default_factory=dict)


class CalendarTemplate(BaseDocument):
    name: str
    kind: TemplateKind
    # Whichever admin authored this - a Standard template is scoped to this
    # admin's own roster (each admin manages their own students, same as
    # everywhere else - see `User.created_by_id`), and a Custom template is
    # this admin's own reusable library entry.
    owner_admin_id: str
    # Required for STANDARD (which level this is the calendar for); always
    # `None` for CUSTOM, which deliberately mixes any topic from any level.
    level: Optional[Level] = None
    # STANDARD only - exactly one active template per (owner_admin_id, level)
    # at a time; see `CalendarTemplateService.activate`. Always `False` for
    # CUSTOM, which is assigned by id, not by "the active one for X."
    is_active: bool = False
    days: list[CalendarDayEntry] = Field(default_factory=list)

    class Settings:
        name = "calendar_templates"
        indexes = [
            [("owner_admin_id", 1), ("kind", 1), ("level", 1), ("is_active", 1)],
        ]
