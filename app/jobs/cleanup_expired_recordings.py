"""One-shot script that deletes Cloudinary media for recordings past their
7-day retention window (`RecordingSubmission.expires_at`), keeping the
document itself - score, reviewed status, and label all stay queryable after
the media is gone, per card #6's done-when.

No in-process scheduler is wired up (this project defers Celery/APScheduler
until actually needed - see requirements.txt). Run this on a schedule some
other way (cron, a platform's scheduled-task feature, ...):

    python -m app.jobs.cleanup_expired_recordings

Safe to run more than once - already-deleted submissions are skipped
(`media_deleted=True` is the filter), and a delete for a public_id Cloudinary
no longer has is not an error worth failing the whole run over.
"""

import asyncio

from loguru import logger

from app.core.database import close_db, init_db
from app.core.storage import UploadFailedError, get_audio_storage
from app.repositories.recording_submission_repository import RecordingSubmissionRepository


async def _cleanup() -> None:
    await init_db()
    try:
        repository = RecordingSubmissionRepository()
        storage = get_audio_storage()
        expired = await repository.get_expired_and_not_yet_deleted()
        logger.info("{} expired submission(s) to clean up", len(expired))

        for submission in expired:
            try:
                await storage.delete(submission.cloudinary_public_id)
            except UploadFailedError as exc:
                logger.warning("Skipping {}: delete failed ({}) - will retry next run", submission.id, exc)
                continue
            submission.media_deleted = True
            await repository.save(submission)
            logger.info("Cleaned up {}", submission.id)
    finally:
        await close_db()


def main() -> None:
    asyncio.run(_cleanup())


if __name__ == "__main__":
    main()
