"""`app.core.youtube`'s URL helpers - `extract_video_id` and
`canonical_watch_url` in particular, since a bug in either one either breaks
topic-URL validation entirely (`extract_video_id`) or, more subtly, makes
Gemini misinterpret a single-video request as a playlist one
(`canonical_watch_url` - see its docstring for the production bug this
exists to fix).
"""

import pytest

from app.core.youtube import canonical_watch_url, derive_thumbnail_url, extract_video_id


def test_extract_video_id_from_a_plain_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"


def test_extract_video_id_from_a_short_url():
    assert extract_video_id("https://youtu.be/abc123") == "abc123"


def test_extract_video_id_from_a_watch_url_with_playlist_params():
    url = "https://www.youtube.com/watch?v=n72v-tPu8FI&list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c&index=22"
    assert extract_video_id(url) == "n72v-tPu8FI"


def test_extract_video_id_rejects_a_bare_playlist_url():
    with pytest.raises(ValueError):
        extract_video_id("https://www.youtube.com/playlist?list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c")


def test_extract_video_id_rejects_a_non_youtube_host():
    with pytest.raises(ValueError):
        extract_video_id("https://example.com/watch?v=abc123")


def test_canonical_watch_url_passes_through_a_plain_watch_url():
    assert canonical_watch_url("https://www.youtube.com/watch?v=abc123") == "https://www.youtube.com/watch?v=abc123"


def test_canonical_watch_url_strips_playlist_and_index_params():
    """Reproduces a real production bug: Gemini treated a URL still carrying
    `list=`/`index=` as a reference to the whole playlist rather than the
    one video, tripping its "1 video per request" limit.
    """
    url = "https://www.youtube.com/watch?v=n72v-tPu8FI&list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c&index=22"
    assert canonical_watch_url(url) == "https://www.youtube.com/watch?v=n72v-tPu8FI"


def test_canonical_watch_url_normalizes_a_short_url_too():
    assert canonical_watch_url("https://youtu.be/abc123") == "https://www.youtube.com/watch?v=abc123"


def test_canonical_watch_url_raises_for_a_bare_playlist_url():
    with pytest.raises(ValueError):
        canonical_watch_url("https://www.youtube.com/playlist?list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c")


def test_derive_thumbnail_url_still_works_for_a_url_with_playlist_params():
    url = "https://www.youtube.com/watch?v=n72v-tPu8FI&list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c&index=22"
    assert derive_thumbnail_url(url) == "https://img.youtube.com/vi/n72v-tPu8FI/hqdefault.jpg"
