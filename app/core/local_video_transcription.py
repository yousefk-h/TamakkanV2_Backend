"""Transcript extraction for teacher-uploaded lesson videos.

Text subtitle streams are extracted first with FFmpeg: this is deterministic
media extraction and does not call an AI provider. When the file has no text
subtitles, faster-whisper performs local ASR on CPU. No video/source content is
sent to Gemini or another transcription API.
"""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from app.core.config import get_settings
from app.models.topic_content import TranscriptSource

MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024


class LocalVideoTranscriptionError(Exception):
    pass


@dataclass(frozen=True)
class LocalVideoTranscript:
    text: str
    source: TranscriptSource


class UploadedVideoTranscriber(Protocol):
    async def transcribe(self, url: str) -> LocalVideoTranscript: ...


def _plain_srt(value: str) -> str:
    lines = []
    for line in value.replace("\r", "").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        if not lines or stripped != lines[-1]:
            lines.append(stripped)
    return " ".join(lines).strip()


def _extract_embedded_subtitles(path: Path) -> str:
    process = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    return _plain_srt(process.stdout) if process.returncode == 0 else ""


_model = None


def _local_asr(path: Path) -> str:
    global _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise LocalVideoTranscriptionError("Local video transcription is not installed on this server") from exc
    if _model is None:
        _model = WhisperModel(get_settings().LOCAL_ASR_MODEL, device="cpu", compute_type="int8")
    segments, _info = _model.transcribe(str(path), language="en", beam_size=5, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    if not text:
        raise LocalVideoTranscriptionError("No spoken English could be extracted from the uploaded video")
    return text


class LocalUploadedVideoTranscriber:
    async def transcribe(self, url: str) -> LocalVideoTranscript:
        suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
        with tempfile.TemporaryDirectory(prefix="tamakkan-video-") as directory:
            path = Path(directory) / f"source{suffix}"
            size = 0
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with path.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > MAX_DOWNLOAD_BYTES:
                                    raise LocalVideoTranscriptionError("Uploaded video is larger than the transcription limit")
                                output.write(chunk)
            except LocalVideoTranscriptionError:
                raise
            except Exception as exc:
                raise LocalVideoTranscriptionError(f"Could not read the uploaded video: {exc}") from exc
            subtitles = await run_in_threadpool(_extract_embedded_subtitles, path)
            if subtitles:
                logger.info("Uploaded video transcript used embedded subtitles")
                return LocalVideoTranscript(subtitles, TranscriptSource.CAPTIONS)
            logger.info("Uploaded video has no text subtitles; using local faster-whisper ASR")
            text = await run_in_threadpool(_local_asr, path)
            return LocalVideoTranscript(text, TranscriptSource.LOCAL_ASR)


def get_uploaded_video_transcriber() -> UploadedVideoTranscriber:
    return LocalUploadedVideoTranscriber()
