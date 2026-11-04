from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.schema.base import BaseSchema


class SubmissionPublic(BaseSchema):
    student_id: str
    kind: SubmissionKind
    topic_id: Optional[str]
    day: Optional[int]
    label: str
    media_url: str
    duration_seconds: float
    reviewed: bool
    media_deleted: bool
    submitted_at: datetime
    expires_at: datetime

    @classmethod
    def from_submission(cls, submission: RecordingSubmission, media_url: str) -> "SubmissionPublic":
        """`media_url` is passed in rather than read off the document: the asset
        is stored with Cloudinary's `authenticated` delivery type, so a playable
        URL has to be signed per response. `RecordingSubmissionService
        .media_url_for` is what mints it (and returns `""` once the media has
        been cleaned up).
        """
        return cls(
            id=submission.id,
            created_at=submission.created_at,
            updated_at=submission.updated_at,
            student_id=submission.student_id,
            kind=submission.kind,
            topic_id=submission.topic_id,
            day=submission.day,
            label=submission.label,
            media_url=media_url,
            duration_seconds=submission.duration_seconds,
            reviewed=submission.reviewed,
            media_deleted=submission.media_deleted,
            submitted_at=submission.submitted_at,
            expires_at=submission.expires_at,
        )


class MarkReviewedRequest(BaseModel):
    reviewed: bool
