"""Bounded reads: no transcript, user email, media URL or credential is loaded.

Deliberately doesn't extend `BaseRepository[Model]`: that base is shaped
around one Document's CRUD lifecycle (`get_by_id`/`save`/`soft_delete`), and
this repository does none of that - it's a read-only aggregator spanning two
unrelated collections (`LiveSession`, `RecordingSubmission`) for
`AnalyticsService`. Its projection types (`SessionMetric`/`SubmissionMetric`
etc.) live in `app.schema.analytics` alongside the rest of the analytics
response shapes, not here - a repository returns model data, never defines
response-shaping types itself.
"""
from datetime import datetime

from app.models.live_session import LiveSession
from app.models.recording_submission import RecordingSubmission
from app.schema.analytics import SessionMetric, SubmissionMetric


class AnalyticsRepository:
    async def sessions(self, student_ids: list[str], start: datetime, now: datetime) -> list[SessionMetric]:
        if not student_ids:
            return []
        return await LiveSession.find({
            "student_id": {"$in": student_ids}, "is_deleted": False, "state": "ended",
            "started_at": {"$gte": start, "$lte": now},
            "ended_at": {"$ne": None, "$lte": now},
        }).project(SessionMetric).to_list()

    async def submissions(self, student_ids: list[str], start: datetime, now: datetime) -> list[SubmissionMetric]:
        if not student_ids:
            return []
        return await RecordingSubmission.find({
            "student_id": {"$in": student_ids}, "is_deleted": False,
            "submitted_at": {"$gte": start, "$lte": now},
        }).project(SubmissionMetric).to_list()
