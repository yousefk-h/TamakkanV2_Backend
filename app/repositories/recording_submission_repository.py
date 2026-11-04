from typing import Optional

from app.models.recording_submission import RecordingSubmission
from app.repositories.base_repository import BaseRepository
from app.utils.time import utcnow


class RecordingSubmissionRepository(BaseRepository[RecordingSubmission]):
    def __init__(self) -> None:
        super().__init__(RecordingSubmission)

    async def get_latest_for_student_and_topic(
        self, student_id: str, topic_id: str
    ) -> Optional[RecordingSubmission]:
        """The current submission for one (student, topic) pair. Resubmitting
        creates a new document linked via `replaces` rather than overwriting
        the old one (so review history isn't lost), and since each
        resubmission is strictly later, "latest by `submitted_at`" is exactly
        "the one not superseded by any other" - no need to walk the
        `replaces` chain to find it.
        """
        return (
            await RecordingSubmission.find(
                RecordingSubmission.student_id == student_id,
                RecordingSubmission.topic_id == topic_id,
                RecordingSubmission.is_deleted == False,  # noqa: E712
            )
            .sort(-RecordingSubmission.submitted_at)
            .first_or_none()
        )

    async def get_latest_for_student_and_day(self, student_id: str, day: int) -> Optional[RecordingSubmission]:
        """The end-of-week-video equivalent of `get_latest_for_student_and_topic` -
        same reasoning, keyed by `day` instead of `topic_id`.
        """
        return (
            await RecordingSubmission.find(
                RecordingSubmission.student_id == student_id,
                RecordingSubmission.day == day,
                RecordingSubmission.is_deleted == False,  # noqa: E712
            )
            .sort(-RecordingSubmission.submitted_at)
            .first_or_none()
        )

    async def get_expired_and_not_yet_deleted(self) -> list[RecordingSubmission]:
        return await RecordingSubmission.find(
            RecordingSubmission.expires_at < utcnow(),
            RecordingSubmission.media_deleted == False,  # noqa: E712
        ).to_list()

    async def get_all_for_student(self, student_id: str) -> list[RecordingSubmission]:
        return await (
            RecordingSubmission.find(
                RecordingSubmission.student_id == student_id,
                RecordingSubmission.is_deleted == False,  # noqa: E712
            )
            .sort(-RecordingSubmission.submitted_at)
            .to_list()
        )

    async def search(
        self,
        student_ids: list[str],
        topic_id: Optional[str] = None,
        reviewed: Optional[bool] = None,
    ) -> list[RecordingSubmission]:
        """The query behind the admin's all-submissions / per-student / filter-
        chip views - `student_ids` is always the caller's own roster (or a
        single id already verified to belong to them), never "everyone."
        """
        query: dict = {"student_id": {"$in": student_ids}, "is_deleted": False}
        if topic_id is not None:
            query["topic_id"] = topic_id
        if reviewed is not None:
            query["reviewed"] = reviewed
        return await RecordingSubmission.find(query).sort(-RecordingSubmission.submitted_at).to_list()
