"""Gemini-based transcription - the only way `TranscriptPipelineService`
gets a transcript for a topic's video. A previous version tried fetching
real YouTube captions first via a private API, falling back to this only
when none existed; that path was removed entirely once it proved
consistently blocked in production (see `transcript_pipeline_service.py`'s
module docstring for what was tried and why it doesn't work), so Gemini is
now the only path, not a fallback.

Passes the YouTube URL directly to Gemini rather than downloading audio
(the old `yt-dlp` + chunking approach, removed entirely): `google-genai`'s
Interactions API accepts a YouTube URL as a `video` content item natively
(https://ai.google.dev/gemini-api/docs/video-understanding#youtube), so
there is no download, no temp file, and no chunking to manage - Gemini
fetches and processes the video itself, well within its per-request length
limits for anything this app's lesson videos would realistically be.

`resolution=MEDIA_RESOLUTION_LOW` (confirmed a real field on the installed
SDK's `VideoContentParam`): this call only ever wants the spoken audio
transcribed, never anything from the video frames, so there is no reason to
spend tokens on higher-resolution frame sampling - low resolution is
~100 tokens/second of video vs ~300 at the default, per Gemini's own
documented token-usage figures.

A tier-1 Gemini project's video-understanding quota is low enough that a
single burst of transcript requests can trip a transient `429`
(`RESOURCE_EXHAUSTED`) even well under this app's own YouTube-side rate
limiting (that limiter only paces InnerTube calls - it has no reason to
also pace Gemini calls, a separate service with its own independent quota).
`_create_with_retry` retries a `429` specifically, with exponential
backoff, on the assumption a fixed per-minute/per-day quota usually frees
up within a few seconds; any other error (a real 4xx like a bad request, or
a persistent `429` that outlasts the retries) still surfaces as
`TranscriptionError` rather than being retried indefinitely.
"""

import asyncio
from typing import Optional, Protocol

from google import genai
from google.genai import errors, types
from loguru import logger

from app.core.config import get_settings

MODEL = "gemini-3.5-flash"
MAX_RATE_LIMIT_RETRIES = 4
INITIAL_RETRY_BACKOFF_SECONDS = 5.0

TRANSCRIBE_PROMPT = (
    "Transcribe the spoken audio of this video verbatim, in English. Return "
    "only the transcript text - no commentary, timestamps, or formatting. "
    "Ignore the visual content entirely; only the audio matters."
)


class TranscriptionError(Exception):
    """Raised when a single transcription attempt fails."""


class Transcriber(Protocol):
    async def transcribe(self, youtube_url: str) -> str: ...


_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    """Lazy singleton, same reasoning as `CloudinaryAudioStorage`'s
    `_ensure_configured`: constructing this at import time would run before
    `.env` is necessarily loaded in every context (e.g. a one-shot job script).
    """
    global _client
    if _client is None:
        api_key = get_settings().GEMINI_API_KEY.strip()
        if not api_key:
            # Validate before constructing the SDK client: failed construction
            # can also leave its asynchronous cleanup task raising an error.
            raise TranscriptionError(
                "GEMINI_API_KEY is missing from the active backend environment. "
                "Set it in that profile (the backend's .env) "
                "and restart the backend."
            )
        _client = genai.Client(api_key=api_key)
    return _client


class YouTubeVideoTranscriber:
    async def transcribe(self, youtube_url: str) -> str:
        try:
            # Keep client setup and provider failures inside the pipeline's
            # TranscriptionError boundary.
            client = _get_client()
            interaction = await self._create_with_retry(client, youtube_url)
            text = interaction.output_text
        except Exception as exc:
            raise TranscriptionError(str(exc)) from exc

        usage = getattr(interaction, "usage", None)
        if usage is not None:
            logger.info(
                "Gemini video transcription token usage for {}: input={} output={} total={}",
                youtube_url,
                usage.total_input_tokens,
                usage.total_output_tokens,
                usage.total_tokens,
            )

        if not text:
            raise TranscriptionError("Gemini returned no transcript text")
        return text.strip()

    async def _create_with_retry(self, client: genai.Client, youtube_url: str):
        backoff = INITIAL_RETRY_BACKOFF_SECONDS
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return await client.aio.interactions.create(
                    model=MODEL,
                    input=[
                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                        {"type": "video", "uri": youtube_url, "resolution": "low"},
                    ],
                )
            except errors.APIError as exc:
                if exc.code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                logger.warning(
                    "Gemini video transcription rate-limited for {} (attempt {}/{}) - retrying in {:.0f}s",
                    youtube_url,
                    attempt + 1,
                    MAX_RATE_LIMIT_RETRIES,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= 2


def get_transcriber() -> Transcriber:
    return YouTubeVideoTranscriber()
