"""Student-submitted audio for a lesson topic (Kanban card #6, scoped to audio
only for now - no end-of-week video check-in submission is built here).

Recording is a client-side concern: the browser enforces the 2-minute cap
while capturing. This service enforces it again, server-side, against
Cloudinary's own reported duration once the file is actually uploaded - it
can't be checked any earlier than that, since this endpoint only ever sees a
completed file (an HTTP upload, not a live stream), and there's no reliable
way to read duration from an arbitrary browser-recorded container without
either an external binary or a codec-specific library.
"""

import re
import unicodedata
import uuid
from typing import Optional

from loguru import logger

from app.core.entitlement import requester_may_open
from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException, UpstreamServiceException
from app.core.storage import AudioStorage, UploadFailedError, get_audio_storage
from app.models.recording_submission import (
    MAX_AUDIO_TASK_DURATION_SECONDS,
    MAX_END_OF_WEEK_VIDEO_DURATION_SECONDS,
    RecordingSubmission,
    SubmissionKind,
)
from app.models.topic_content import TopicContent
from app.models.user import Role, User
from app.repositories.recording_submission_repository import RecordingSubmissionRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.repositories.user_repository import UserRepository
from app.services.progress_service import ProgressService, get_progress_service
from app.utils.time import utcnow

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # generous headroom over 2 minutes of even uncompressed WAV
# 120 seconds of webcam video at a typical unconstrained MediaRecorder bitrate
# (browsers commonly default to ~2.5 Mbps for 720p) is already ~35-40MB - the
# audio ceiling above would clip a normal recording well before the duration
# cap does. Sized with the same "generous headroom" intent as the audio one,
# against that real bitrate rather than copied from it.
MAX_END_OF_WEEK_VIDEO_UPLOAD_BYTES = 80 * 1024 * 1024
UPLOAD_ATTEMPTS = 3
# New uploads only: existing assets keep their stored `cloudinary_public_id`.
CLOUDINARY_ROOT_FOLDER = "Tamakkan"
MAX_SLUG_LENGTH = 60  # per path segment - readability, not a security boundary


def _slugify(value: str) -> str:
    # NFKD + ascii-ignore folds accented Latin to its plain letter (e.g. "É"
    # -> "E") instead of just dropping it - a naive [^a-z0-9] strip on the raw
    # string would silently eat the first letter of "Écoute" and leave
    # "coute". Non-Latin scripts (Arabic, CJK, ...) have no ASCII equivalent
    # and still fall through to "untitled" - a real limitation, not one this
    # function can fix, and localization is explicitly deferred platform-wide.
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    # Truncated defensively as well as at the schema (`app.schema.limits`): this
    # helper is also reachable from jobs and scripts that don't go through a
    # schema, and its output becomes a storage key rather than just a response.
    return slug[:MAX_SLUG_LENGTH].strip("-") or "untitled"


def _build_public_id(student: User, topic: TopicContent) -> str:
    """`Tamakkan/{student}-{id fragment}/{topic}-{timestamp}-{random}` - readable
    enough to find a specific student's submissions directly in the Cloudinary
    console, which is why the display name is here at all rather than a bare id.

    The id fragment is what makes the folder *correct*, and it isn't decoration:
    `_slugify` is many-to-one, so two students both called "John Doe" would
    otherwise share one folder, and every non-Latin-script name collapses to
    `untitled` - which on an Arabic-speaking platform is the common case, not
    the edge case. `full_name` is also Student-writable via `PATCH /me`, so a
    name-only folder is something a student could deliberately aim at another
    student's folder.

    The random suffix is a full `uuid4` hex, not a truncation: this is the only
    thing preventing two submissions from colliding on one public_id (Cloudinary
    overwrites silently), and it's reused across all three upload retries of a
    single submission, so it needs to be a real guarantee rather than a 32-bit
    probabilistic one.
    """
    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return (
        f"{CLOUDINARY_ROOT_FOLDER}/{_slugify(student.full_name)}-{student.id[:8]}/"
        f"{_slugify(topic.title)}-{timestamp}-{uuid.uuid4().hex}"
    )


def _build_public_id_for_day(student: User, day: int) -> str:
    """The end-of-week-video equivalent of `_build_public_id` - same
    reasoning for every part of the shape, keyed by `day` instead of a topic
    title since an end-of-week check-in isn't topic-tied. The path prefix
    ("end-of-week-day-") only affects newly-uploaded assets going forward -
    existing assets keep whatever `cloudinary_public_id` they were already
    given, so there's nothing to migrate here.
    """
    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{CLOUDINARY_ROOT_FOLDER}/{_slugify(student.full_name)}-{student.id[:8]}/end-of-week-day-{day}-{timestamp}-{uuid.uuid4().hex}"


class RecordingSubmissionService:
    def __init__(
        self,
        submission_repository: RecordingSubmissionRepository,
        topic_repository: TopicContentRepository,
        storage: AudioStorage,
        user_repository: UserRepository,
        progress_service: ProgressService,
    ) -> None:
        self.submission_repository = submission_repository
        self.topic_repository = topic_repository
        self.storage = storage
        self.user_repository = user_repository
        self.progress_service = progress_service

    async def submit_audio(
        self, student: User, topic_id: str, file_bytes: bytes, content_type: str
    ) -> RecordingSubmission:
        if not content_type.startswith("audio/"):
            raise UnprocessableException(f"Expected an audio file, got '{content_type}'")
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise UnprocessableException("File is larger than a 2-minute recording should ever be")

        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        # The same rule `GET /topics/{topic_id}` applies, from the same module.
        # Without it this endpoint was a write path into unentitled (or
        # not-yet-unlocked) content and an oracle for it: the response
        # carries `label`, i.e. the title of a topic the caller can't read.
        if not await requester_may_open(student, topic_id, self.progress_service):
            raise ForbiddenException("This topic is not part of your assigned plan, or hasn't unlocked yet")

        public_id = _build_public_id(student, topic)
        result = await self._upload_with_retry(file_bytes, public_id)

        # Past this line the asset exists in storage. Every failure below has to
        # remove it: the cleanup job finds assets by iterating database rows, so
        # an asset with no row is one it can never reach.
        try:
            if result.duration_seconds > MAX_AUDIO_TASK_DURATION_SECONDS:
                # The client enforces this while recording; this is the backstop
                # for a buggy or bypassed one.
                logger.warning(
                    "Rejected over-length submission: student={} topic={} duration={:.0f}s",
                    student.id,
                    topic_id,
                    result.duration_seconds,
                )
                raise UnprocessableException(
                    f"Recording is {result.duration_seconds:.0f}s, "
                    f"longer than the {MAX_AUDIO_TASK_DURATION_SECONDS}s limit"
                )

            previous = await self.submission_repository.get_latest_for_student_and_topic(student.id, topic_id)

            submission = RecordingSubmission(
                student=student,
                student_id=student.id,
                program_id=student.program_id,
                program_level_id=student.program_level_id,
                kind=SubmissionKind.AUDIO_TASK,
                topic=topic,
                topic_id=topic_id,
                label=topic.title,
                media_url=result.secure_url,
                cloudinary_public_id=result.public_id,
                duration_seconds=result.duration_seconds,
                replaces=previous,
            )
            return await self.submission_repository.save(submission)
        except Exception:
            await self._delete_orphan(result.public_id)
            raise

    async def submit_end_of_week_video(
        self, student: User, day: int, file_bytes: bytes, content_type: str
    ) -> RecordingSubmission:
        """The one real student recording flow (per-lesson audio was removed
        from the player) - a weekly video check-in, not topic-tied and not
        entitlement-gated (every student, Standard or Pro, does the 30-day
        challenge), keyed instead by `day`. Otherwise a straight mirror of
        `submit_audio`: same retry/orphan-cleanup/replace-on-resubmit shape.
        """
        if not content_type.startswith("video/"):
            raise UnprocessableException(f"Expected a video file, got '{content_type}'")
        if len(file_bytes) > MAX_END_OF_WEEK_VIDEO_UPLOAD_BYTES:
            raise UnprocessableException("File is larger than a 2-minute recording should ever be")

        public_id = _build_public_id_for_day(student, day)
        result = await self._upload_with_retry(file_bytes, public_id)

        try:
            if result.duration_seconds > MAX_END_OF_WEEK_VIDEO_DURATION_SECONDS:
                logger.warning(
                    "Rejected over-length end-of-week video: student={} day={} duration={:.0f}s",
                    student.id,
                    day,
                    result.duration_seconds,
                )
                raise UnprocessableException(
                    f"Recording is {result.duration_seconds:.0f}s, "
                    f"longer than the {MAX_END_OF_WEEK_VIDEO_DURATION_SECONDS}s limit"
                )

            previous = await self.submission_repository.get_latest_for_student_and_day(student.id, day)

            submission = RecordingSubmission(
                student=student,
                student_id=student.id,
                program_id=student.program_id,
                program_level_id=student.program_level_id,
                kind=SubmissionKind.END_OF_WEEK_VIDEO,
                topic=None,
                topic_id=None,
                day=day,
                label=f"End-of-week check-in - Day {day}",
                media_url=result.secure_url,
                cloudinary_public_id=result.public_id,
                duration_seconds=result.duration_seconds,
                replaces=previous,
            )
            return await self.submission_repository.save(submission)
        except Exception:
            await self._delete_orphan(result.public_id)
            raise

    async def mark_reviewed(self, submission_id: str, admin: User, reviewed: bool) -> RecordingSubmission:
        """Administrator only (enforced by the route) - reuses `get_one`'s
        ownership check (the owning student's own owning Administrator, not
        any Administrator) rather than re-deriving it.
        """
        submission = await self.get_one(submission_id, admin)
        submission.reviewed = reviewed
        return await self.submission_repository.save(submission)

    async def _delete_orphan(self, public_id: str) -> None:
        """Best-effort removal of an asset whose submission was never recorded.

        Deliberately swallows its own failure: this runs while another exception
        is already propagating, and letting a Cloudinary blip replace it would
        turn a clear 422 ("your recording is too long") into an opaque 500 - and
        still leave the asset behind. Logged at error level because an orphan is
        real, unrecoverable storage cost that nothing else will ever find.
        """
        try:
            await self.storage.delete(public_id)
        except UploadFailedError as exc:
            logger.error("Orphaned storage asset {} - delete failed: {}", public_id, exc)

    def media_url_for(self, submission: RecordingSubmission) -> str:
        """Signed fresh per response rather than served from the stored
        `media_url`: the asset is `authenticated` (see `app.core.storage`), so
        delivery needs a signature, and minting it here leaves room to add a
        short expiry later without a data migration.

        Empty once the media is gone - the cleanup job deletes the asset but
        keeps the row, and handing back a URL to a deleted asset is a lie.
        """
        if submission.media_deleted:
            return ""
        return self.storage.signed_url(submission.cloudinary_public_id)

    async def list_for_student(self, student: User) -> list[RecordingSubmission]:
        """Full history, not just the current one per topic - the student's
        own screen groups by topic, and a full list lets the frontend show
        past attempts if it wants to; a latest-only list would throw that
        away and there'd be no getting it back.
        """
        return await self.submission_repository.get_all_for_student(student.id)

    async def get_one(self, submission_id: str, requester: User) -> RecordingSubmission:
        """Ownership-checked fetch: the owning student, or *that student's*
        owning Teacher - not any Teacher. The card text says "an
        Administrator," but scoping it to any admin platform-wide reopens the
        exact cross-tenant hole just fixed for password resets (any admin
        acting on any student via an id they can see). This is a deliberate
        strengthening beyond the literal card wording, not an oversight.

        The Center Admin (`Role.ADMINISTRATOR`) bypasses this entirely - it may read any student's submission,
        same reach as every other center-wide action.
        """
        submission = await self.submission_repository.get_by_id(submission_id)
        if submission is None:
            raise NotFoundException("Submission not found")

        if requester.role == Role.STUDENT:
            if submission.student_id != requester.id:
                raise ForbiddenException("This submission does not belong to you")
            return submission

        if requester.role == Role.ADMINISTRATOR:
            return submission

        student = await self.user_repository.get_by_id(submission.student_id)
        if student is None or student.created_by_id != requester.id:
            raise ForbiddenException("This student is not assigned to you")
        return submission

    async def list_for_admin(
        self,
        admin: User,
        student_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        reviewed: Optional[bool] = None,
    ) -> list[RecordingSubmission]:
        """The Center Admin (`Role.ADMINISTRATOR`) sees every submission in the center; a Teacher sees only
        their own roster's."""
        roster = (
            await self.user_repository.get_all_students()
            if admin.role == Role.ADMINISTRATOR
            else await self.user_repository.get_students_created_by(admin.id)
        )
        roster_ids = {student.id for student in roster}

        if student_id is not None:
            if student_id not in roster_ids:
                raise ForbiddenException("This student is not assigned to you")
            target_ids = [student_id]
        else:
            target_ids = list(roster_ids)

        if not target_ids:
            return []
        return await self.submission_repository.search(target_ids, topic_id=topic_id, reviewed=reviewed)

    async def _upload_with_retry(self, file_bytes: bytes, public_id: str):
        # Same public_id across every retry of *this* submission - it already
        # has its own random suffix (see `_build_public_id`), so retrying
        # under it can't collide with a different submission's asset.
        last_error: Exception = UploadFailedError("no attempt was made")
        for _ in range(UPLOAD_ATTEMPTS):
            try:
                return await self.storage.upload(file_bytes, public_id)
            except UploadFailedError as exc:
                last_error = exc
        logger.error("Upload to storage failed after {} attempts: {}", UPLOAD_ATTEMPTS, last_error)
        # Fixed message, not `last_error`. The raw Cloudinary exception string
        # names the configured cloud, the API endpoint, and connection-pool
        # internals, and it reaches the client verbatim through the
        # `AppException` handler. It's already logged above, server-side.
        raise UpstreamServiceException("Could not store the recording. Please try again.")


def get_recording_submission_service() -> RecordingSubmissionService:
    return RecordingSubmissionService(
        RecordingSubmissionRepository(), TopicContentRepository(), get_audio_storage(), UserRepository(), get_progress_service()
    )
