"""A human-led Group class or 1:1 coaching session - an Administrator-
scheduled slot with a pasted Google Meet link, not an in-app video call.
Unrelated to `LiveSession` (app/models/live_session.py), which is the AI
"Discuss with Mariam" avatar session - the similar name is coincidental.

One-off only, no recurrence: a weekly slot is just the admin creating several
of these. No capacity field either - a Group class is exactly the Standard
students the admin picked when scheduling it, not an open enrollment pool.
Cancelling a class is a plain soft delete (`BaseDocument.is_deleted`) - once
cancelled it should behave as gone everywhere, same as every other soft-
deleted document in this codebase; there's no separate "cancelled but still
visible" state to track.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated

from beanie import Indexed
from pydantic import Field

from app.models.base import BaseDocument


class ClassKind(str, Enum):
    GROUP = "group"
    ONE_TO_ONE = "one_to_one"


class LiveClass(BaseDocument):
    # Which Administrator scheduled this - classes are scoped to their own
    # roster, same as every other admin-owned entity in this app. A plain
    # indexed id, not a Link, for the same reason as `User.created_by_id`:
    # Links can't be queried by equality.
    created_by_id: Annotated[str, Indexed()]

    kind: ClassKind
    title: str
    scheduled_at: datetime
    duration_minutes: int
    meet_link: str

    # The enrolled students - exactly one for a 1:1, any number for a Group.
    # A plain id list, not `list[Link[User]]`, same reasoning as
    # `User.assigned_topic_ids`: never queried by containment reliably via a
    # Link, and there's no need to fetch full User documents through this
    # field - the frontend already has the admin's student roster loaded.
    participant_ids: list[str] = Field(default_factory=list)

    class Settings:
        name = "live_classes"
        indexes = [
            # The admin's own schedule, chronological - the primary read path.
            [("created_by_id", 1), ("scheduled_at", 1)],
            # A student's "my classes" list: which classes include me, then
            # chronological. `participant_ids` is a multikey index here -
            # Mongo's implicit array-contains match on plain equality is what
            # `get_all_for_participant` relies on.
            [("participant_ids", 1), ("scheduled_at", 1)],
        ]
