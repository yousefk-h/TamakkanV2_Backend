"""Topic queries, plus the two *partial* write primitives the transcript
pipeline needs.

`BaseRepository.save()` is a whole-document replace from an in-memory snapshot.
That's fine for a short request/response, but wrong for anything holding a
document across slow work: the pipeline loads a topic, spends minutes on
network calls, then saves - reverting every edit made in the meantime. Worse,
because the transition is validated against the *snapshot's* state, a late run
could move an `approved_for_ai` topic back to `pending_review` and null the
`approved_transcript` the AI depends on, defeating a state the transition table
calls terminal.

So writes here are scoped `$set`s, and `transition_state` is a compare-and-set:
the expected current state is part of the query, so a stale run matches nothing
and is discarded instead of clobbering. Both bypass Beanie's `before_event`
hook (it only fires on document-level saves), so they set `updated_at` by hand.
"""

import re
from typing import Any, Optional

from beanie.odm.operators.update.general import Set

from app.models.topic_content import Level, TopicContent, TranscriptState
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class TopicContentRepository(BaseRepository[TopicContent]):
    def __init__(self) -> None:
        super().__init__(TopicContent)

    async def list_all(
        self,
        transcript_state: Optional[TranscriptState] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        skip: int = 0,
        owner_id: Optional[str] = None,
        search: Optional[str] = None,
        level: Optional[Level] = None,
    ) -> list[TopicContent]:
        """`owner_id=None` (Root/internal use) returns every topic, unfiltered
        by author - unchanged from before teacher isolation. Given, it scopes
        to that Administrator's own topics *plus* legacy ones authored before
        `created_by_id` existed (`None`) - those stay visible read-only to
        every Administrator until Root re-owns them, see
        `TopicContentService.get_own_topic`.
        """
        query = self._base_query(transcript_state)
        if owner_id is not None:
            query["created_by_id"] = {"$in": [owner_id, None]}
        if level is not None:
            query["level"] = level.value
        if search and search.strip():
            literal = re.escape(search.strip())
            query["$or"] = [
                {"title": {"$regex": literal, "$options": "i"}},
                {"vocabulary.term": {"$regex": literal, "$options": "i"}},
                {"lesson_exercises.prompt": {"$regex": literal, "$options": "i"}},
            ]
        return await self._paged(query, limit, skip)

    async def count_created_by(self, owner_id: str) -> int:
        """One Teacher's own authored topic count (legacy/shared ones excluded) - the Teacher Details drawer."""
        return await TopicContent.find(TopicContent.created_by_id == owner_id, TopicContent.is_deleted == False).count()  # noqa: E712

    async def get_many_by_ids(self, topic_ids: list[str]) -> list[TopicContent]:
        """Unordered batch fetch, soft-deletes excluded - `ProgressService`'s
        vocabulary tally needs the `vocabulary` list off whichever topics a
        student's sessions reference, and there's no existing "give me these
        specific ones" primitive here.
        """
        if not topic_ids:
            return []
        return await TopicContent.find({"_id": {"$in": topic_ids}, "is_deleted": False}).to_list()

    @staticmethod
    def _base_query(transcript_state: Optional[TranscriptState]) -> dict:
        query: dict = {"is_deleted": False}
        if transcript_state is not None:
            query["transcript_state"] = transcript_state.value
        return query

    @staticmethod
    async def _paged(query: dict, limit: int, skip: int) -> list[TopicContent]:
        # Newest first, and an explicit sort rather than Mongo's natural order:
        # paging is only coherent against a deterministic ordering.
        return await TopicContent.find(query).sort("-created_at").skip(skip).limit(limit).to_list()

    async def update_fields(self, topic_id: str, values: dict[str, Any]) -> bool:
        """`$set` exactly `values` and nothing else. Returns False if the topic
        is gone or soft-deleted.
        """
        result = await TopicContent.find_one(
            TopicContent.id == topic_id,
            TopicContent.is_deleted == False,  # noqa: E712
        ).update(Set({**values, "updated_at": utcnow()}))
        return result.matched_count == 1

    async def transition_state(
        self,
        topic_id: str,
        expected: TranscriptState,
        target: TranscriptState,
        **extra: Any,
    ) -> bool:
        """Compare-and-set on `transcript_state`: moves `expected` -> `target`
        only if the stored state is still `expected`, setting `extra` in the same
        write. Returns False when nothing matched - the caller decides whether
        that's a conflict to report or a stale result to drop.
        """
        result = await TopicContent.find_one(
            TopicContent.id == topic_id,
            TopicContent.is_deleted == False,  # noqa: E712
            TopicContent.transcript_state == expected,
        ).update(Set({"transcript_state": target, **extra, "updated_at": utcnow()}))
        return result.matched_count == 1

    async def update_pending_review_segments(self, topic_id: str, segments: list[Any]) -> bool:
        """Persist teacher corrections only while a transcript is awaiting
        review. A concurrent approval wins instead of being overwritten."""
        result = await TopicContent.find_one(
            TopicContent.id == topic_id,
            TopicContent.is_deleted == False,  # noqa: E712
            TopicContent.transcript_state == TranscriptState.PENDING_REVIEW,
        ).update(Set({"raw_transcript": segments, "updated_at": utcnow()}))
        return result.matched_count == 1

    async def find_ids_by_title(self, text: str, limit: int = 500) -> list[str]:
        """Ids of non-deleted topics whose title contains `text` (case-insensitive, literal - never a user regex)."""
        topics = await TopicContent.find(
            {"title": {"$regex": re.escape(text), "$options": "i"}, "is_deleted": False}
        ).limit(limit).to_list()
        return [topic.id for topic in topics]
