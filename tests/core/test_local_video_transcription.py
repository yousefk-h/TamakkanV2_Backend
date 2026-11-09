import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.local_video_transcription import _extract_embedded_subtitles


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_embedded_subtitles_are_extracted_without_speech_recognition(tmp_path: Path):
    subtitle = tmp_path / "lesson.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nWelcome to today's lesson.\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\nWe will practice introductions.\n",
        encoding="utf-8",
    )
    video = tmp_path / "lesson.mkv"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3",
            "-i", str(subtitle), "-c:v", "libx264", "-c:s", "srt", "-shortest", str(video),
        ],
        check=True,
    )

    assert _extract_embedded_subtitles(video) == (
        "Welcome to today's lesson. We will practice introductions."
    )
