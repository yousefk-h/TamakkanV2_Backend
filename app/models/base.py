"""Shared Document base: UUID4 primary keys (not Mongo's default ObjectId),
plus `created_at`/`updated_at` that every model gets for free.

The id is generated with `uuid4()` but typed and stored as `str`, not `UUID`:
native BSON UUIDs need an explicit `uuid_representation` and are a long-
standing source of bugs when different drivers/tools disagree on which
representation was used (legacy vs. standard) - storing the string form side-
steps that ambiguity entirely, at the cost of nothing we actually need here.
(Confirmed empirically while implementing: `mongomock` doesn't thread
`codec_options`/`uuid_representation` through its BSON encode calls at all, so
native UUID storage doesn't even work against the test double - independent
evidence for the same choice.)

`updated_at` is bumped automatically via a Beanie event hook - callers never
set it themselves, so it can't drift out of sync with what actually changed.

Deliberately *not* `is_root = True` - that's Beanie's mechanism for several
subclasses sharing *one* physical collection (polymorphic/union documents),
which is not what this is. Caught empirically while implementing: with
`is_root = True` set here, every subclass landed in the same collection, and a
unique index from one subclass (`RefreshToken.token_hash`) then rejected a
second, unrelated subclass's document as a "duplicate" `token_hash: None`.
Plain inheritance is all that's needed - each subclass keeps its own
independent collection via its own `Settings.name`, and `BaseDocument` itself
is simply never passed to `init_beanie()`.

`is_deleted` is a plain flag, not a paired `deleted_at` timestamp - only what
was asked for. `BaseRepository.soft_delete()` sets it and `get_by_id()`
excludes it from lookups, but nothing physically removes a document or
enforces uniqueness *excluding* soft-deleted rows (e.g. `User.email`'s unique
index still counts a soft-deleted account, so its email can't be reused by a
new one) - a real limitation, flagged rather than silently solved, since
fixing it needs a partial/sparse index decision that wasn't part of this ask.
"""

from datetime import datetime
from uuid import uuid4

from beanie import Document, Insert, Replace, Save, Update, before_event
from pydantic import Field

from app.utils.time import utcnow


class BaseDocument(Document):
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    is_deleted: bool = False

    @before_event(Insert, Replace, Save, Update)
    def _bump_updated_at(self) -> None:
        self.updated_at = utcnow()
