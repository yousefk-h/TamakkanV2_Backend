import pytest

from app.core.transcription import TranscriptionError, _segments_from_response


def test_gemini_timestamped_segments_preserve_arabic_and_code_switching():
    segments = _segments_from_response(
        '{"segments":[{"start_seconds":0,"end_seconds":3.25,"text":"هسه خلينا نحل السؤال الأول عن الـ Present Simple."},'
        '{"start_seconds":3.25,"end_seconds":5,"text":"Then choose the correct answer."}]}'
    )
    assert segments[0].text == "هسه خلينا نحل السؤال الأول عن الـ Present Simple."
    assert segments[1].text == "Then choose the correct answer."
    assert segments[0].start == 0 and segments[0].end == 3.25


@pytest.mark.parametrize("payload", [
    '{"segments":[{"start_seconds":2,"end_seconds":1,"text":"bad"}]}',
    '{"segments":[{"start_seconds":4,"end_seconds":5,"text":"first"},{"start_seconds":3,"end_seconds":6,"text":"overlap"}]}',
    'plain text without timestamps',
])
def test_gemini_rejects_unverifiable_timestamps(payload):
    with pytest.raises(TranscriptionError):
        _segments_from_response(payload)
