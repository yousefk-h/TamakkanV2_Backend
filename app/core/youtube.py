"""YouTube video-id parsing, shared by the transcript pipeline
(`app.core.transcription`/`app.schema.topic`, which need a hard failure when
a URL can't be parsed) and lesson-thumbnail derivation
(`app.services.topic_content_service`, which just wants `None` back for a
URL it can't use) - one parser, two error conventions layered on top, rather
than two independently-drifting copies of the same `urlparse` logic.
"""

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from loguru import logger

from app.core.config import get_settings

_ISO8601_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def extract_video_id(youtube_url: str) -> str:
    """Raises `ValueError` if no video id can be parsed out."""
    parsed = urlparse(youtube_url)
    if parsed.hostname in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/")
        if video_id:
            return video_id
    if parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        query = parse_qs(parsed.query)
        if "v" in query and query["v"][0]:
            return query["v"][0]
    raise ValueError(f"Could not extract a video id from {youtube_url!r}")


def canonical_watch_url(youtube_url: str) -> str:
    """Strips everything except the video id down to a bare
    `https://www.youtube.com/watch?v=<id>` - in particular `list=`/`index=`,
    which a playlist-context URL (`...watch?v=X&list=Y&index=N`) carries.

    Required before handing a URL to Gemini's video-understanding input
    (`app.core.transcription`): passing a URL with `list=`/`index=` on it was
    observed in production to make Gemini treat the request as referencing
    the surrounding playlist rather than the one video - tripping its "only
    1 video per request" limit (pre-2.5 models) even though the caller only
    ever intends a single video.

    Raises `ValueError` (via `extract_video_id`) if `youtube_url` has no
    resolvable single video id at all - e.g. a bare playlist URL
    (`.../playlist?list=...`, no `v=`).
    """
    return f"https://www.youtube.com/watch?v={extract_video_id(youtube_url)}"


def derive_thumbnail_url(youtube_url: str) -> Optional[str]:
    """YouTube serves this thumbnail for free, no API key, for any video id -
    `hqdefault.jpg` rather than `maxresdefault.jpg` because the latter 404s for
    a real fraction of videos (no HD thumbnail was ever generated), while
    `hqdefault` is guaranteed to exist for every public video.
    """
    try:
        video_id = extract_video_id(youtube_url)
    except ValueError:
        return None
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def _parse_iso8601_duration_seconds(duration: str) -> Optional[int]:
    match = _ISO8601_DURATION_RE.match(duration)
    if match is None:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


async def fetch_video_duration_seconds(youtube_url: str) -> Optional[int]:
    """Best-effort - `None` for a URL that can't be parsed, no configured
    `YOUTUBE_API_KEY`, a network/API error, or a video the API won't return
    contentDetails for (private/deleted). Purely informational (a "video
    length" badge - see `TopicContent.video_duration_seconds`); nothing
    depends on this succeeding, so every failure mode is swallowed here
    rather than raised.
    """
    settings = get_settings()
    if not settings.YOUTUBE_API_KEY:
        return None
    try:
        video_id = extract_video_id(youtube_url)
    except ValueError:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails", "id": video_id, "key": settings.YOUTUBE_API_KEY},
            )
            response.raise_for_status()
            items = response.json().get("items", [])
    except httpx.HTTPError:
        logger.warning("Failed to fetch YouTube video duration for {}", video_id)
        return None
    if not items:
        return None
    return _parse_iso8601_duration_seconds(items[0]["contentDetails"]["duration"])
