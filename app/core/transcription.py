"""Gemini Video transcription for teacher-owned lesson sources.

Both public YouTube URLs and uploaded lesson videos use Gemini's official
video input. An uploaded video is first placed in the Gemini Files API; a
YouTube URL is passed to Gemini directly. We deliberately never decode audio
for local speech recognition.
"""

import asyncio
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import httpx
from google import genai
from google.genai import errors, types
from loguru import logger

from app.core.config import get_settings

MODEL = "gemini-3.5-flash"
MAX_RATE_LIMIT_RETRIES = 4
INITIAL_RETRY_BACKOFF_SECONDS = 5.0
MAX_VIDEO_BYTES = 80 * 1024 * 1024
FILE_ACTIVE_TIMEOUT_SECONDS = 180

TRANSCRIBE_PROMPT = """Transcribe only the spoken audio in this educational video.
Keep the ORIGINAL spoken language exactly: Arabic must stay Arabic script,
English must stay English, and Arabic-English code switching must be preserved.
Do not translate, summarize, normalize dialect, or transliterate Arabic.

Return ONLY valid JSON in this exact shape:
{"segments":[{"start_seconds":0.0,"end_seconds":2.4,"text":"..."}]}
Each segment must have directly observed video-timeline timestamps. Do not
guess timestamps. Segments must be chronological, non-overlapping, and each
must contain spoken words. Omit silence.
"""


class TranscriptionError(Exception):
    """A recoverable provider/input failure safe to show to a teacher."""


@dataclass(frozen=True)
class TranscriptSegmentResult:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class VideoTranscriptionResult:
    segments: list[TranscriptSegmentResult]
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class Transcriber(Protocol):
    async def transcribe_youtube(self, youtube_url: str) -> VideoTranscriptionResult: ...
    async def transcribe_uploaded(self, video_url: str, filename: str) -> VideoTranscriptionResult: ...


_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = get_settings().GEMINI_API_KEY.strip()
        if not api_key:
            raise TranscriptionError("Gemini video transcription is not configured for this environment. Ask an administrator to configure it, then retry.")
        _client = genai.Client(api_key=api_key)
    return _client


def _usage(interaction: object) -> tuple[Optional[int], Optional[int]]:
    usage = getattr(interaction, "usage", None)
    return (
        getattr(usage, "total_input_tokens", None) if usage is not None else None,
        getattr(usage, "total_output_tokens", None) if usage is not None else None,
    )


def _segments_from_response(text: Optional[str]) -> list[TranscriptSegmentResult]:
    if not text:
        raise TranscriptionError("Gemini returned no transcript. Please retry this video.")
    payload_text = text.strip()
    if payload_text.startswith("```"):
        payload_text = payload_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        items = json.loads(payload_text)["segments"]
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise TranscriptionError("Gemini did not return reviewable timestamped segments. Please retry this video.") from exc
    if not isinstance(items, list) or not items:
        raise TranscriptionError("Gemini returned no spoken segments to review. Please retry this video.")
    segments: list[TranscriptSegmentResult] = []
    previous_end = 0.0
    for item in items:
        try:
            start, end, spoken = float(item["start_seconds"]), float(item["end_seconds"]), str(item["text"]).strip()
        except (TypeError, KeyError, ValueError) as exc:
            raise TranscriptionError("Gemini returned invalid transcript timestamps. Please retry this video.") from exc
        if not spoken or start < 0 or end <= start or start + 0.01 < previous_end:
            raise TranscriptionError("Gemini returned timestamps that cannot be verified. Please retry this video.")
        segments.append(TranscriptSegmentResult(text=spoken, start=start, end=end))
        previous_end = end
    return segments


class GeminiVideoTranscriber:
    async def transcribe_youtube(self, youtube_url: str) -> VideoTranscriptionResult:
        interaction = await self._create_with_retry(_get_client(), {"type": "video", "uri": youtube_url, "resolution": "low"})
        input_tokens, output_tokens = _usage(interaction)
        return VideoTranscriptionResult(_segments_from_response(getattr(interaction, "output_text", None)), input_tokens, output_tokens)

    async def transcribe_uploaded(self, video_url: str, filename: str) -> VideoTranscriptionResult:
        path = await self._download_video(video_url, filename)
        uploaded = None
        try:
            client = _get_client()
            mime_type = mimetypes.guess_type(filename)[0] or "video/mp4"
            uploaded = await client.aio.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime_type))
            uploaded = await self._wait_until_active(client, uploaded)
            uri = getattr(uploaded, "uri", None)
            if not uri:
                raise TranscriptionError("Gemini could not prepare this uploaded video. Please retry it.")
            interaction = await self._create_with_retry(client, {"type": "video", "uri": uri, "resolution": "low"})
            input_tokens, output_tokens = _usage(interaction)
            return VideoTranscriptionResult(_segments_from_response(getattr(interaction, "output_text", None)), input_tokens, output_tokens)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            if uploaded is not None:
                try:
                    await _get_client().aio.files.delete(name=getattr(uploaded, "name", ""))
                except Exception:
                    logger.warning("Could not remove temporary Gemini video file")

    async def _download_video(self, video_url: str, filename: str) -> str:
        suffix = Path(filename).suffix or ".mp4"
        fd, path = tempfile.mkstemp(prefix="tamakkan-gemini-video-", suffix=suffix)
        os.close(fd)
        size = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=180.0), follow_redirects=False) as client:
                async with client.stream("GET", video_url) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_VIDEO_BYTES:
                        raise TranscriptionError("This video is larger than the 80 MB transcription limit.")
                    with open(path, "wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_VIDEO_BYTES:
                                raise TranscriptionError("This video is larger than the 80 MB transcription limit.")
                            output.write(chunk)
            if not size:
                raise TranscriptionError("The uploaded video is empty. Upload another file and try again.")
            return path
        except TranscriptionError:
            try: os.unlink(path)
            except OSError: pass
            raise
        except Exception as exc:
            try: os.unlink(path)
            except OSError: pass
            raise TranscriptionError("We could not read the uploaded video for Gemini. Check the file and retry.") from exc

    async def _wait_until_active(self, client: genai.Client, uploaded: object) -> object:
        name = getattr(uploaded, "name", None)
        if not name:
            raise TranscriptionError("Gemini could not prepare this uploaded video. Please retry it.")
        for _ in range(FILE_ACTIVE_TIMEOUT_SECONDS // 3):
            state = str(getattr(uploaded, "state", "")).upper()
            if "ACTIVE" in state:
                return uploaded
            if "FAILED" in state:
                raise TranscriptionError("Gemini could not process this video format. Convert it to MP4 or MOV and retry.")
            await asyncio.sleep(3)
            uploaded = await client.aio.files.get(name=name)
        raise TranscriptionError("Gemini is still preparing this video. Please retry in a few minutes.")

    async def _create_with_retry(self, client: genai.Client, video: dict):
        backoff = INITIAL_RETRY_BACKOFF_SECONDS
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return await client.aio.interactions.create(model=MODEL, input=[video, {"type": "text", "text": TRANSCRIBE_PROMPT}])
            except errors.APIError as exc:
                if exc.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise TranscriptionError("Gemini could not process this video right now. Please retry later.") from exc
                await asyncio.sleep(backoff)
                backoff *= 2
            except Exception as exc:
                raise TranscriptionError("Gemini could not process this video. Check that it is accessible and retry.") from exc
        raise TranscriptionError("Gemini could not process this video. Please retry later.")


def get_transcriber() -> Transcriber:
    return GeminiVideoTranscriber()
