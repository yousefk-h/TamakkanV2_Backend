"""Student-submitted recordings: an end-of-week video check-in (the one real
student recording flow - per-lesson audio was removed from the player) and
the original per-topic audio task endpoint, kept working but unused by
anything today (Kanban card #6, extended for the end-of-week video check-in).

Card #8 (submissions views) is the read side: a student's own history, an
ownership-checked single fetch, and an admin's filtered view over their own
students - "New screen" and "filter chips" are frontend work in
the frontend repository; these are the endpoints behind them.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.core.security import get_current_user, require_teacher_or_administrator, require_role
from app.models.user import Role, User
from app.schema.submission import MarkReviewedRequest, SubmissionPublic
from app.services.recording_submission_service import (
    MAX_END_OF_WEEK_VIDEO_UPLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    RecordingSubmissionService,
    get_recording_submission_service,
)

router = APIRouter()


@router.post("/submissions", response_model=SubmissionPublic)
async def submit_audio(
    topic_id: str = Form(...),
    file: UploadFile = File(..., description="An audio file, max 2 minutes long."),
    student: User = Depends(require_role(Role.STUDENT)),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> SubmissionPublic:
    """Student only. Submit an audio recording for a topic. Resubmitting for
    the same topic replaces the previous submission without losing its
    history - see `replaces` on the stored record.
    """
    # Bounded read: one byte past the limit is enough for the service to reject
    # it, and it means an oversized file never becomes an oversized `bytes`.
    # `BodySizeLimitMiddleware` is the outer guard - this is the inner one, and
    # the only one that knows the *audio* limit specifically.
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    submission = await service.submit_audio(
        student, topic_id, file_bytes, file.content_type or "application/octet-stream"
    )
    return SubmissionPublic.from_submission(submission, service.media_url_for(submission))


@router.post("/submissions/end-of-week-video", response_model=SubmissionPublic)
async def submit_end_of_week_video(
    day: int = Form(..., description="Which day (1-30) of the challenge this check-in answers."),
    file: UploadFile = File(..., description="A video file, max 2 minutes long."),
    student: User = Depends(require_role(Role.STUDENT)),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> SubmissionPublic:
    """Student only. Submit a weekly video check-in - not topic-tied, not
    entitlement-gated (every student does the challenge regardless of plan).
    Resubmitting for the same day replaces the previous submission, same as
    audio's per-topic replace.
    """
    file_bytes = await file.read(MAX_END_OF_WEEK_VIDEO_UPLOAD_BYTES + 1)
    submission = await service.submit_end_of_week_video(
        student, day, file_bytes, file.content_type or "application/octet-stream"
    )
    return SubmissionPublic.from_submission(submission, service.media_url_for(submission))


@router.get("/submissions/me", response_model=list[SubmissionPublic])
async def list_my_submissions(
    student: User = Depends(require_role(Role.STUDENT)),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> list[SubmissionPublic]:
    """Student only. List your own full submission history, across every
    topic - group by `topic_id` client-side to display by topic.
    """
    submissions = await service.list_for_student(student)
    return [SubmissionPublic.from_submission(s, service.media_url_for(s)) for s in submissions]


@router.get("/submissions", response_model=list[SubmissionPublic])
async def list_submissions_for_admin(
    student_id: Optional[str] = Query(None, description="Restrict to one of your own students."),
    topic_id: Optional[str] = Query(None, description="Restrict to submissions for this topic."),
    reviewed: Optional[bool] = Query(None, description="Restrict to reviewed or unreviewed submissions."),
    admin: User = Depends(require_teacher_or_administrator),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> list[SubmissionPublic]:
    """Administrator only. List submissions from your own students - never
    another Administrator's. `student_id` for a student that isn't yours is
    rejected, not silently emptied.
    """
    submissions = await service.list_for_admin(admin, student_id=student_id, topic_id=topic_id, reviewed=reviewed)
    return [SubmissionPublic.from_submission(s, service.media_url_for(s)) for s in submissions]


@router.get("/submissions/{submission_id}", response_model=SubmissionPublic)
async def get_submission(
    submission_id: str,
    requester: User = Depends(get_current_user),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> SubmissionPublic:
    """Fetch one submission. Only the owning Student, or that Student's own
    owning Administrator, may access it - not any Administrator.
    """
    submission = await service.get_one(submission_id, requester)
    return SubmissionPublic.from_submission(submission, service.media_url_for(submission))


@router.patch("/submissions/{submission_id}/reviewed", response_model=SubmissionPublic)
async def mark_submission_reviewed(
    submission_id: str,
    body: MarkReviewedRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: RecordingSubmissionService = Depends(get_recording_submission_service),
) -> SubmissionPublic:
    """Administrator only. Marks (or unmarks) one of your own students'
    submissions as reviewed - the same ownership rule as `GET /submissions/{id}`.
    """
    submission = await service.mark_reviewed(submission_id, admin, body.reviewed)
    return SubmissionPublic.from_submission(submission, service.media_url_for(submission))
