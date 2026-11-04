"""Audio storage - uploads a completed recording to Cloudinary and can delete
it again once its retention window passes (see
`app/jobs/cleanup_expired_recordings.py`).

A `Protocol` + concrete implementation, the same shape as
`app.core.email.EmailSender` and for the same reason: there are no test
credentials for a real Cloudinary account, so tests inject a fake instead of
making a real network call.

Audio uploads under Cloudinary's `video` resource type - Cloudinary has no
separate `audio` type. `video` is what accepts MP3/WAV/etc. and is what
returns a `duration` in the response, which is how the 2-minute limit is
enforced (see `RecordingSubmissionService`) - there's no reliable way to read
duration from an arbitrary browser-recorded container (e.g. `audio/webm`)
without either an external binary (`ffprobe`) or a codec-specific library, and
Cloudinary already has to inspect the file to store it.
"""

from dataclasses import dataclass
from typing import Protocol

import cloudinary
import cloudinary.uploader
import cloudinary.utils
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from app.core.config import get_settings

# Cloudinary has no `audio` resource type; `video` is what accepts MP3/WAV/etc.
# and what reports a `duration`.
RESOURCE_TYPE = "video"

# NOT Cloudinary's default (`upload`), which serves the asset to anyone who has
# the URL, with no credential. These are minors' voice recordings, and the
# careful ownership checks on `GET /submissions/{id}` protect the *pointer* to
# the asset, not the asset - so a URL that leaked once (browser history, a
# copied link, a proxy log) would be permanently, publicly playable.
# `authenticated` means delivery requires a signature only this server can
# produce; see `signed_url`.
DELIVERY_TYPE = "authenticated"


@dataclass
class UploadResult:
    public_id: str
    secure_url: str
    duration_seconds: float


class AudioStorage(Protocol):
    async def upload(self, file_bytes: bytes, public_id: str) -> UploadResult: ...
    async def delete(self, public_id: str) -> None: ...

    def signed_url(self, public_id: str) -> str:
        """A delivery URL for an `authenticated` asset. Not `async` - signing is
        an HMAC over the public_id, pure CPU with no network call.
        """
        ...


class UploadFailedError(Exception):
    """Raised when a single upload/delete attempt fails. The service layer
    decides whether and how many times to retry - this module never retries
    on its own.
    """


_configured = False


def _ensure_configured() -> None:
    """Cloudinary's SDK holds its credentials in module-global state
    (`cloudinary.config(...)`), not an instance you pass around - so this
    configures it once, lazily, the first time it's actually needed, reading
    from `Settings` rather than at import time (import time runs before
    `.env` is necessarily loaded in every context, e.g. a one-shot job script).
    """
    global _configured
    if _configured:
        return
    settings = get_settings()
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    _configured = True


class CloudinaryAudioStorage:
    async def upload(self, file_bytes: bytes, public_id: str) -> UploadResult:
        _ensure_configured()
        try:
            # The Cloudinary SDK is synchronous (blocking network I/O) - run it
            # off the event loop so one slow upload doesn't stall every other
            # request this process is handling.
            result = await run_in_threadpool(
                cloudinary.uploader.upload,
                file_bytes,
                public_id=public_id,
                resource_type=RESOURCE_TYPE,
                type=DELIVERY_TYPE,
            )
            # Parsing the response stays *inside* the try: a payload missing
            # `duration` would otherwise raise `KeyError` (or `ValueError` from
            # `float()`), which is not an `UploadFailedError`, so it would skip
            # the service's retry handling and surface as a bare 500 - having
            # already stored an asset that no database row points at, which the
            # cleanup job (it iterates rows) could then never delete.
            return UploadResult(
                public_id=result["public_id"],
                secure_url=result["secure_url"],
                duration_seconds=float(result["duration"]),
            )
        except Exception as exc:
            logger.warning("Cloudinary upload failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc

    async def delete(self, public_id: str) -> None:
        _ensure_configured()
        try:
            await run_in_threadpool(
                cloudinary.uploader.destroy,
                public_id,
                resource_type=RESOURCE_TYPE,
                type=DELIVERY_TYPE,
            )
        except Exception as exc:
            logger.warning("Cloudinary delete failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc

    def signed_url(self, public_id: str) -> str:
        _ensure_configured()
        url, _options = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=RESOURCE_TYPE,
            type=DELIVERY_TYPE,
            sign_url=True,
            secure=True,
        )
        return url


def get_audio_storage() -> AudioStorage:
    return CloudinaryAudioStorage()


# Lesson thumbnails (Kanban follow-up to card #9). Unlike audio, these are not
# sensitive - a lesson's cover image is meant to be publicly visible wherever
# the lesson is listed - so, unlike `AudioStorage` above, delivery is
# Cloudinary's default `upload` type (a plain public URL, no signing) and the
# resource type is `image`.
IMAGE_RESOURCE_TYPE = "image"
IMAGE_DELIVERY_TYPE = "upload"


@dataclass
class ImageUploadResult:
    public_id: str
    secure_url: str


class ImageStorage(Protocol):
    async def upload(self, file_bytes: bytes, public_id: str) -> ImageUploadResult: ...
    async def delete(self, public_id: str) -> None: ...


class CloudinaryImageStorage:
    async def upload(self, file_bytes: bytes, public_id: str) -> ImageUploadResult:
        _ensure_configured()
        try:
            result = await run_in_threadpool(
                cloudinary.uploader.upload,
                file_bytes,
                public_id=public_id,
                resource_type=IMAGE_RESOURCE_TYPE,
                type=IMAGE_DELIVERY_TYPE,
            )
            return ImageUploadResult(public_id=result["public_id"], secure_url=result["secure_url"])
        except Exception as exc:
            logger.warning("Cloudinary image upload failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc

    async def delete(self, public_id: str) -> None:
        _ensure_configured()
        try:
            await run_in_threadpool(
                cloudinary.uploader.destroy,
                public_id,
                resource_type=IMAGE_RESOURCE_TYPE,
                type=IMAGE_DELIVERY_TYPE,
            )
        except Exception as exc:
            logger.warning("Cloudinary image delete failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc


def get_image_storage() -> ImageStorage:
    return CloudinaryImageStorage()


class CloudinaryLessonAudioStorage:
    """Public teacher-authored listening audio, separate from private student recordings."""

    async def upload(self, file_bytes: bytes, public_id: str) -> ImageUploadResult:
        _ensure_configured()
        try:
            result = await run_in_threadpool(cloudinary.uploader.upload, file_bytes,
                                             public_id=public_id, resource_type="video", type="upload")
            return ImageUploadResult(public_id=result["public_id"], secure_url=result["secure_url"])
        except Exception as exc:
            raise UploadFailedError(str(exc)) from exc


def get_lesson_audio_storage() -> CloudinaryLessonAudioStorage:
    return CloudinaryLessonAudioStorage()


# Lesson source PDFs (video-optional content, card #16). Same visibility
# reasoning as the thumbnail above - teacher-authored source material, not
# sensitive student data - so delivery is the same plain public `upload`
# type. Resource type is `raw`: Cloudinary's `image`/`video` types each
# assume they can transform/transcode the asset, which does not apply to an
# arbitrary document.
DOCUMENT_RESOURCE_TYPE = "raw"
DOCUMENT_DELIVERY_TYPE = "upload"


@dataclass
class DocumentUploadResult:
    public_id: str
    secure_url: str


class DocumentStorage(Protocol):
    async def upload(self, file_bytes: bytes, public_id: str) -> DocumentUploadResult: ...
    async def delete(self, public_id: str) -> None: ...


class CloudinaryDocumentStorage:
    async def upload(self, file_bytes: bytes, public_id: str) -> DocumentUploadResult:
        _ensure_configured()
        try:
            result = await run_in_threadpool(
                cloudinary.uploader.upload,
                file_bytes,
                public_id=public_id,
                resource_type=DOCUMENT_RESOURCE_TYPE,
                type=DOCUMENT_DELIVERY_TYPE,
            )
            return DocumentUploadResult(public_id=result["public_id"], secure_url=result["secure_url"])
        except Exception as exc:
            logger.warning("Cloudinary document upload failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc

    async def delete(self, public_id: str) -> None:
        _ensure_configured()
        try:
            await run_in_threadpool(
                cloudinary.uploader.destroy,
                public_id,
                resource_type=DOCUMENT_RESOURCE_TYPE,
                type=DOCUMENT_DELIVERY_TYPE,
            )
        except Exception as exc:
            logger.warning("Cloudinary document delete failed for public_id={}: {}", public_id, exc)
            raise UploadFailedError(str(exc)) from exc


def get_document_storage() -> DocumentStorage:
    return CloudinaryDocumentStorage()
