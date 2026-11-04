from datetime import datetime
from typing import Optional

from app.models.live_session import LiveSession, PracticeMode
from app.repositories.base_repository import BaseRepository


class LiveSessionRepository(BaseRepository[LiveSession]):
    def __init__(self) -> None:
        super().__init__(LiveSession)

    async def get_all_for_student(self, student_id: str) -> list[LiveSession]:
        return (
            await LiveSession.find(
                LiveSession.student_id == student_id,
                LiveSession.is_deleted == False,  # noqa: E712
            )
            .sort(-LiveSession.started_at)
            .to_list()
        )

    async def get_all_for_admin(self, student_ids: list[str]) -> list[LiveSession]:
        """`student_ids` is always the caller's own roster - same pattern as
        `RecordingSubmissionRepository.search`.
        """
        return (
            await LiveSession.find({"student_id": {"$in": student_ids}, "is_deleted": False})
            .sort(-LiveSession.started_at)
            .to_list()
        )

    async def search(
        self,
        student_ids: list[str],
        *,
        student_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        mode: Optional[PracticeMode] = None,
        report_status: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_before: Optional[datetime] = None,
        search_student_ids: Optional[list[str]] = None,
        search_topic_ids: Optional[list[str]] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> tuple[list[LiveSession], int]:
        """Filtered, newest-first page of sessions plus the TOTAL number matching, all in the database.

        `student_ids` is always the caller's own roster, so no filter can widen access: asking for a `student_id` outside it
        simply matches nothing. `search_*_ids` are the already-resolved ids a free-text search matched (student name/email,
        topic title); a session matches the search if EITHER its student or its topic is in them.
        """
        conditions: list[dict] = [{"student_id": {"$in": student_ids}, "is_deleted": False}]
        if student_id is not None:
            conditions.append({"student_id": student_id})
        if topic_id is not None:
            conditions.append({"topic_id": topic_id})
        if mode is not None:
            conditions.append({"mode": mode.value})
        if search_student_ids is not None or search_topic_ids is not None:
            conditions.append({"$or": [{"student_id": {"$in": search_student_ids or []}}, {"topic_id": {"$in": search_topic_ids or []}}]})
        if started_from is not None or started_before is not None:
            window: dict = {}
            if started_from is not None:
                window["$gte"] = started_from
            if started_before is not None:
                window["$lt"] = started_before
            conditions.append({"started_at": window})
        status_conditions = {
            "any": {"report": {"$ne": None}},
            "scored": {"report.overall_score": {"$gte": 0}},  # a numeric comparison: null / missing scores never match (same on real Mongo and the test DB)
            "not_scored": {"report": {"$ne": None}, "report.overall_score": None},
            "needs_review": {"report": {"$ne": None}, "report.reviewed": False},
            "reviewed": {"report.reviewed": True},
        }
        if report_status is not None:
            conditions.append(status_conditions[report_status])

        query = {"$and": conditions}
        total = await LiveSession.find(query).count()
        cursor = LiveSession.find(query).sort(-LiveSession.started_at).skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        return await cursor.to_list(), total
