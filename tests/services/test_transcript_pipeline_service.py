"""Kanban card #11: the transcript pipeline, behind a fake for its one
external boundary (Gemini video transcription) - nothing here makes a real
network call. `FakeTranscriber`'s real counterpart's request/response shapes
were independently verified during development (see
app/core/transcription.py's docstring).
"""

from datetime import timedelta

import pytest

from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.transcription import TranscriptionError
from app.core.local_video_transcription import LocalVideoTranscript
from app.core.content_drafting import ArabicMeaning, ContentDraftingError, DraftedContent
from app.models.topic_content import (
    Level,
    TopicContent,
    TranscriptSegment,
    TranscriptSource,
    TranscriptState,
    VocabularyItem,
)
from app.repositories.topic_content_repository import TopicContentRepository
from app.services.transcript_pipeline_service import TranscriptPipelineService
from app.utils.time import utcnow


class FakeTranscriber:
    def __init__(self, text="from gemini", error=None):
        self.text = text
        self.error = error
        self.calls: list[str] = []

    async def transcribe(self, youtube_url):
        self.calls.append(youtube_url)
        if self.error is not None:
            raise self.error
        return self.text


class FakeUploadedVideoTranscriber:
    def __init__(self, text="local video words", source=TranscriptSource.LOCAL_ASR):
        self.text = text
        self.source = source
        self.calls: list[str] = []

    async def transcribe(self, video_url):
        self.calls.append(video_url)
        return LocalVideoTranscript(self.text, self.source)


class FakeContentDrafter:
    def __init__(self, draft=None, error=None, translations=None, translate_error=None):
        self.draft_result = draft if draft is not None else DraftedContent(
            avatar_prompt="Discuss the video with the student.",
            opening_line="So, what did you think of that video?",
            warm_up_questions=["What was the video about?"],
            vocabulary=[VocabularyItem(term="melody", theme="Music", example_sentence="The melody stuck in my head all day.")],
        )
        self.error = error
        self.calls: list[tuple] = []
        # Empty by default (not e.g. one entry per missing term) - most tests
        # never exercise Arabic-meaning backfill at all and expect
        # `arabic_meaning` to stay "" exactly as `draft_result` left it.
        self.translations = translations if translations is not None else []
        self.translate_error = translate_error
        self.translate_calls: list[list[str]] = []

    async def draft(self, approved_transcript, level, title):
        self.calls.append((approved_transcript, level, title))
        if self.error is not None:
            raise self.error
        return self.draft_result

    async def translate_to_arabic(self, terms):
        self.translate_calls.append(terms)
        if self.translate_error is not None:
            raise self.translate_error
        return self.translations


def _make_service(
    transcriber=None,
    content_drafter=None,
    extraction_timeout=timedelta(minutes=30),
    uploaded_video_transcriber=None,
):
    return TranscriptPipelineService(
        TopicContentRepository(),
        transcriber or FakeTranscriber(),
        content_drafter or FakeContentDrafter(),
        extraction_timeout=extraction_timeout,
        uploaded_video_transcriber=uploaded_video_transcriber or FakeUploadedVideoTranscriber(),
    )


async def _make_topic(youtube_urls=("https://www.youtube.com/watch?v=abc123",)) -> TopicContent:
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE, youtube_urls=list(youtube_urls))
    await topic.insert()
    return topic


# --- start_generation / state machine ---------------------------------------


async def test_start_generation_moves_queued_to_extracting():
    topic = await _make_topic()
    updated = await _make_service().start_generation(topic.id)
    assert updated.transcript_state == TranscriptState.EXTRACTING


async def test_start_generation_rejects_a_topic_with_no_video():
    topic = await _make_topic(youtube_urls=())
    with pytest.raises(UnprocessableException):
        await _make_service().start_generation(topic.id)


async def test_start_generation_conflicts_if_already_extracting():
    topic = await _make_topic()
    service = _make_service()
    await service.start_generation(topic.id)
    with pytest.raises(ConflictException):
        await service.start_generation(topic.id)


async def test_start_generation_unknown_topic_is_404():
    with pytest.raises(NotFoundException):
        await _make_service().start_generation("not-a-real-id")


# --- run_generation -----------------------------------------------------


async def test_run_generation_transcribes_via_gemini():
    topic = await _make_topic()
    transcriber = FakeTranscriber(text="hello world")
    service = _make_service(transcriber=transcriber)
    await service.start_generation(topic.id)

    await service.run_generation(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.transcript_state == TranscriptState.PENDING_REVIEW
    # A single, untimed segment - there's no per-word timing from a whole-
    # video Gemini transcription.
    assert len(refreshed.raw_transcript) == 1
    assert refreshed.raw_transcript[0].text == "hello world"
    assert refreshed.raw_transcript[0].start == 0.0
    assert refreshed.transcript_source == TranscriptSource.AI_GENERATED
    assert transcriber.calls == [topic.youtube_urls[0]]


async def test_uploaded_video_and_pdf_are_labeled_and_never_sent_to_gemini():
    topic = await _make_topic(youtube_urls=())
    topic.uploaded_video_url = "https://cdn.example/lesson.mp4"
    topic.uploaded_video_filename = "lesson.mp4"
    topic.pdf_filename = "notes.pdf"
    topic.pdf_extracted_text = "Support notes for the lesson."
    await topic.save()
    gemini = FakeTranscriber(error=AssertionError("uploaded videos must not use Gemini"))
    local = FakeUploadedVideoTranscriber(text="Words spoken in the video.")
    service = _make_service(transcriber=gemini, uploaded_video_transcriber=local)

    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    pending = await TopicContent.get(topic.id)

    assert [segment.text for segment in pending.raw_transcript] == [
        "Words spoken in the video.",
        "Support notes for the lesson.",
    ]
    assert pending.transcript_source_labels == ["Uploaded video — lesson.mp4", "PDF — notes.pdf"]
    assert pending.transcript_source == TranscriptSource.MIXED
    assert local.calls == [topic.uploaded_video_url]
    assert gemini.calls == []

    approved = await service.approve_transcript(topic.id)
    assert "[SOURCE: Uploaded video — lesson.mp4]" in approved.approved_transcript
    assert "[SOURCE: PDF — notes.pdf]" in approved.approved_transcript


async def test_run_generation_passes_gemini_a_canonical_url_stripped_of_playlist_params():
    """Reproduces a real production bug: a video URL still carrying
    `list=`/`index=` (e.g. one copied from inside a playlist) made Gemini
    treat the request as the whole playlist rather than the one video,
    tripping its "1 video per request" limit - `_fetch_transcript` must
    strip those before calling the transcriber.
    """
    topic = await _make_topic(
        youtube_urls=(
            "https://www.youtube.com/watch?v=n72v-tPu8FI&list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c&index=22",
        )
    )
    transcriber = FakeTranscriber(text="from gemini")
    service = _make_service(transcriber=transcriber)
    await service.start_generation(topic.id)

    await service.run_generation(topic.id)

    assert transcriber.calls == ["https://www.youtube.com/watch?v=n72v-tPu8FI"]
    refreshed = await TopicContent.get(topic.id)
    assert refreshed.transcript_state == TranscriptState.PENDING_REVIEW


async def test_run_generation_reverts_to_queued_when_the_video_url_has_no_resolvable_id():
    """Belt-and-suspenders: `TopicCreate`'s schema validator already rejects
    a bare playlist URL before it can ever be stored, but this service is
    also usable directly (as these tests do) - a topic with an unresolvable
    URL must still fail cleanly through the normal QUEUED-revert path rather
    than raising past `run_generation`'s own exception boundary.
    """
    topic = await _make_topic(youtube_urls=("https://www.youtube.com/playlist?list=PLsomething",))
    transcriber = FakeTranscriber()
    service = _make_service(transcriber=transcriber)
    await service.start_generation(topic.id)

    await service.run_generation(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.transcript_state == TranscriptState.QUEUED
    assert transcriber.calls == []


async def test_run_generation_reverts_to_queued_when_gemini_transcription_fails():
    topic = await _make_topic()
    transcriber = FakeTranscriber(error=TranscriptionError("gemini is down"))
    service = _make_service(transcriber=transcriber)
    await service.start_generation(topic.id)

    await service.run_generation(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.transcript_state == TranscriptState.QUEUED
    assert refreshed.raw_transcript == []


async def test_run_generation_on_a_deleted_topic_is_a_silent_no_op():
    await _make_service().run_generation("not-a-real-id")  # must not raise


async def test_only_the_first_video_url_is_processed():
    topic = await _make_topic(youtube_urls=("https://youtu.be/first", "https://youtu.be/second"))
    transcriber = FakeTranscriber(text="x")
    service = _make_service(transcriber=transcriber)
    await service.start_generation(topic.id)

    await service.run_generation(topic.id)

    assert transcriber.calls == ["https://www.youtube.com/watch?v=first"]


# --- approve_transcript ------------------------------------------------------


async def test_approve_transcript_from_pending_review_joins_segment_text():
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="Hello world."))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)

    approved = await service.approve_transcript(topic.id)

    assert approved.transcript_state == TranscriptState.APPROVED_FOR_AI
    assert approved.approved_transcript == "Hello world."


async def test_approve_transcript_uses_edited_text_over_auto_join():
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="Hello"))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)

    approved = await service.approve_transcript(topic.id, edited_transcript="  Corrected wording.  ")

    assert approved.approved_transcript == "Corrected wording."


async def test_approve_transcript_rejects_blank_edited_text():
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="Hello"))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)

    with pytest.raises(UnprocessableException):
        await service.approve_transcript(topic.id, edited_transcript="   ")


async def test_approve_transcript_rejects_from_queued():
    topic = await _make_topic()
    with pytest.raises(ConflictException):
        await _make_service().approve_transcript(topic.id)


async def test_approve_transcript_is_terminal_cannot_reapprove():
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="x"))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    with pytest.raises(ConflictException):
        await service.approve_transcript(topic.id)


async def test_approve_transcript_unknown_topic_is_404():
    with pytest.raises(NotFoundException):
        await _make_service().approve_transcript("not-a-real-id")


# --- run_content_drafting ----------------------------------------------------


async def test_run_content_drafting_fills_all_empty_fields_after_approval():
    topic = await _make_topic()
    draft = DraftedContent(
        avatar_prompt="Ask about greetings.",
        opening_line="Hi there, ready to practice?",
        warm_up_questions=["What was the video about?"],
        vocabulary=[VocabularyItem(term="hello", theme="Greetings", example_sentence="Hello, how are you?")],
    )
    drafter = FakeContentDrafter(draft=draft)
    # Long enough to clear `MIN_SOURCE_CHARS_FOR_DRAFTING` - a real approved
    # transcript is never this short, and the guard exists precisely to
    # refuse drafting from source this thin (see test_transcript_pipeline
    # _service.py's `test_run_content_drafting_refuses_source_below_the_
    # minimum_length` below).
    source_text = "Hello world, this is today's lesson transcript for testing."
    service = _make_service(transcriber=FakeTranscriber(text=source_text), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.avatar_prompt == "Ask about greetings."
    assert refreshed.opening_line == "Hi there, ready to practice?"
    assert refreshed.warm_up_questions == ["What was the video about?"]
    assert [v.term for v in refreshed.vocabulary] == ["hello"]
    # The topic's own level and title were passed through to the drafter -
    # grounding needs both, not just the raw transcript text.
    assert drafter.calls == [(source_text, Level.INTERMEDIATE, "Talking about Music")]


async def test_run_content_drafting_never_overwrites_fields_the_admin_already_wrote():
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)
    # The admin wrote their own opening line before the draft landed - the
    # nav rail makes Step3 reachable at any point once a topic exists.
    await TopicContentRepository().update_fields(topic.id, {"opening_line": "Hand-written by the admin."})

    await service.run_content_drafting(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.opening_line == "Hand-written by the admin."
    # Everything else was still empty, so it's still filled.
    assert refreshed.avatar_prompt != ""
    assert refreshed.warm_up_questions != []
    assert refreshed.vocabulary != []


async def test_run_content_drafting_backfills_arabic_meaning_on_freshly_drafted_vocabulary():
    """`draft.vocabulary` is what the main Gemini call returns - this covers
    it occasionally omitting `arabic_meaning` for a term even though the
    vocabulary list itself was empty and so got fully written.
    """
    topic = await _make_topic()
    draft = DraftedContent(
        vocabulary=[
            VocabularyItem(term="hello", theme="Greetings", example_sentence="Hello, how are you?"),
            VocabularyItem(term="goodbye", theme="Greetings", example_sentence="Goodbye!", arabic_meaning="وداعاً"),
        ],
    )
    drafter = FakeContentDrafter(draft=draft, translations=[ArabicMeaning(term="hello", arabic_meaning="مرحباً")])
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)

    refreshed = await TopicContent.get(topic.id)
    by_term = {v.term: v.arabic_meaning for v in refreshed.vocabulary}
    assert by_term == {"hello": "مرحباً", "goodbye": "وداعاً"}
    # Only the term actually missing a meaning was sent for translation.
    assert drafter.translate_calls == [["hello"]]


async def test_run_content_drafting_backfills_missing_arabic_meanings_on_existing_vocabulary():
    """The vocabulary list itself isn't empty (so the main fill above never
    touches it - matching the guard `if not current.vocabulary`), but one
    item the admin typed by hand has no Arabic meaning yet.
    """
    topic = await _make_topic()
    await TopicContentRepository().update_fields(
        topic.id,
        {
            "vocabulary": [
                VocabularyItem(term="hello", theme="Greetings", example_sentence="Hi.").model_dump(),
                VocabularyItem(term="goodbye", theme="Greetings", example_sentence="Bye.", arabic_meaning="وداعاً").model_dump(),
            ],
            "avatar_prompt": "Already written by the admin.",
        },
    )
    drafter = FakeContentDrafter(translations=[ArabicMeaning(term="hello", arabic_meaning="مرحباً")])
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)

    refreshed = await TopicContent.get(topic.id)
    by_term = {v.term: v.arabic_meaning for v in refreshed.vocabulary}
    assert by_term == {"hello": "مرحباً", "goodbye": "وداعاً"}
    # The admin's own avatar_prompt (and the rest of each vocab item) is untouched.
    assert refreshed.avatar_prompt == "Already written by the admin."
    assert [v.example_sentence for v in refreshed.vocabulary] == ["Hi.", "Bye."]


async def test_run_content_drafting_arabic_backfill_has_nothing_to_do_when_all_terms_have_one():
    topic = await _make_topic()
    await TopicContentRepository().update_fields(
        topic.id,
        {"vocabulary": [VocabularyItem(term="hello", theme="Greetings", example_sentence="Hi.", arabic_meaning="مرحباً").model_dump()]},
    )
    drafter = FakeContentDrafter()
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)

    assert drafter.translate_calls == []  # nothing missing - no call made at all


async def test_run_content_drafting_arabic_backfill_failure_does_not_raise():
    topic = await _make_topic()
    draft = DraftedContent(vocabulary=[VocabularyItem(term="hello", theme="Greetings", example_sentence="Hi.")])
    drafter = FakeContentDrafter(draft=draft, translate_error=ContentDraftingError("gemini is down"))
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)  # must not raise

    refreshed = await TopicContent.get(topic.id)
    # The vocabulary itself still landed - only the translation pass failed.
    assert [v.term for v in refreshed.vocabulary] == ["hello"]
    assert refreshed.vocabulary[0].arabic_meaning == ""


async def test_run_content_drafting_on_a_deleted_topic_is_a_silent_no_op():
    await _make_service().run_content_drafting("not-a-real-id")  # must not raise


async def test_run_content_drafting_on_an_unapproved_topic_is_a_no_op():
    topic = await _make_topic()  # never approved - no approved_transcript
    drafter = FakeContentDrafter()
    service = _make_service(content_drafter=drafter)

    await service.run_content_drafting(topic.id)

    assert drafter.calls == []
    refreshed = await TopicContent.get(topic.id)
    assert refreshed.vocabulary == []


async def test_run_content_drafting_failure_leaves_everything_empty_without_raising():
    topic = await _make_topic()
    drafter = FakeContentDrafter(error=ContentDraftingError("gemini is down"))
    service = _make_service(transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."), content_drafter=drafter)
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)
    await service.approve_transcript(topic.id)

    await service.run_content_drafting(topic.id)  # must not raise

    refreshed = await TopicContent.get(topic.id)
    assert refreshed.vocabulary == []
    assert refreshed.avatar_prompt == ""


# --- Regression tests for the concurrency/durability review ---------------
#
# Each of these reproduces a bug that was found and fixed after card #11
# shipped. They are written against the service rather than the HTTP layer
# because the failure modes are all about *when* a write lands, which
# `TestClient` can't express (it runs `BackgroundTasks` synchronously inside
# the request, so a "slow" background run is not observable through a route).


class ExplodingTranscriber:
    """Fails with something that is deliberately *not* one of the pipeline's
    own error types - the shape of a missing `GEMINI_API_KEY` (`ValueError`
    from the SDK) or a cleared `youtube_urls` (`IndexError`).
    """

    async def transcribe(self, youtube_url):
        raise RuntimeError("an SDK error nobody wrapped")


async def test_an_unexpected_error_reverts_to_queued_instead_of_stranding_the_topic():
    """`run_generation` used to catch only `TranscriptGenerationFailed`, so
    anything else escaped and left the topic in `extracting` - which has no
    `extracting -> extracting` edge, so it could never be regenerated.
    """
    service = _make_service(transcriber=ExplodingTranscriber())
    topic = await _make_topic()

    await service.start_generation(topic.id)
    await service.run_generation(topic.id)  # must not raise

    fresh = await TopicContentRepository().get_by_id(topic.id)
    assert fresh.transcript_state == TranscriptState.QUEUED
    assert fresh.transcript_state_started_at is None
    # ...and the admin can simply try again.
    await service.start_generation(topic.id)


async def test_a_topic_stranded_in_extracting_can_be_reclaimed_once_its_run_is_abandoned():
    """The process-death case: nothing reverts the state, so recovery has to
    come from recognising the run as abandoned rather than from the task.
    """
    repository = TopicContentRepository()
    topic = await _make_topic()
    service = _make_service(
        transcriber=FakeTranscriber(text="ok"),
        extraction_timeout=timedelta(minutes=30),
    )

    await service.start_generation(topic.id)
    # Still inside the timeout: a second caller must not stomp on a live run.
    with pytest.raises(ConflictException):
        await service.start_generation(topic.id)

    # Backdate the claim to simulate a run that died 31 minutes ago.
    await repository.update_fields(
        topic.id, {"transcript_state_started_at": utcnow() - timedelta(minutes=31)}
    )
    reclaimed = await service.start_generation(topic.id)
    assert reclaimed.transcript_state == TranscriptState.EXTRACTING


class TranscriberWithSideEffect:
    """Runs `side_effect` *while the pipeline is working* - after
    `run_generation` has read the topic, before it writes its result.

    That interleaving is the entire point. These bugs were about a write landing
    on top of a change made mid-run, so a test that makes the change before the
    run (where the run simply reads the newer state) or after it proves nothing
    and passes against the buggy code too.
    """

    def __init__(self, text, side_effect):
        self.text = text
        self.side_effect = side_effect

    async def transcribe(self, youtube_url):
        await self.side_effect()
        return self.text


async def test_a_late_run_cannot_undo_an_approved_transcript():
    """The worst of the set: `save()` wrote a whole stale snapshot, so a run
    finishing after an approval moved `approved_for_ai` (documented terminal)
    back to `pending_review` and nulled `approved_transcript`.
    """
    repository = TopicContentRepository()
    topic = await _make_topic()

    async def a_faster_run_finishes_and_is_approved():
        await repository.transition_state(
            topic.id,
            expected=TranscriptState.EXTRACTING,
            target=TranscriptState.PENDING_REVIEW,
            raw_transcript=[TranscriptSegment(text="run A", start=0.0, duration=1.0)],
        )
        await service.approve_transcript(topic.id)

    service = _make_service(
        transcriber=TranscriberWithSideEffect("run B", a_faster_run_finishes_and_is_approved)
    )

    await service.start_generation(topic.id)
    await service.run_generation(topic.id)  # finishes last, must be discarded

    fresh = await repository.get_by_id(topic.id)
    assert fresh.transcript_state == TranscriptState.APPROVED_FOR_AI
    assert fresh.approved_transcript == "run A"
    assert [segment.text for segment in fresh.raw_transcript] == ["run A"]


async def test_a_finished_run_does_not_resurrect_a_deleted_topic():
    repository = TopicContentRepository()
    topic = await _make_topic()
    service = _make_service(transcriber=FakeTranscriber(text="x"))

    await service.start_generation(topic.id)
    await repository.soft_delete(await repository.get_by_id(topic.id))
    await service.run_generation(topic.id)  # no caller, must not raise

    assert await repository.get_by_id(topic.id) is None


async def test_a_running_generation_does_not_revert_a_concurrent_edit():
    """The same whole-document-save bug seen from the other side: an admin
    `PATCH` landing mid-run used to be silently reverted when the run saved its
    minutes-old snapshot.
    """
    repository = TopicContentRepository()
    topic = await _make_topic()

    async def an_admin_edits_the_topic():
        await repository.update_fields(
            topic.id, {"title": "Renamed mid-run", "opening_line": "hello"}
        )

    service = _make_service(transcriber=TranscriberWithSideEffect("t", an_admin_edits_the_topic))
    await service.start_generation(topic.id)
    await service.run_generation(topic.id)

    fresh = await repository.get_by_id(topic.id)
    assert fresh.title == "Renamed mid-run"
    assert fresh.opening_line == "hello"
    assert fresh.transcript_state == TranscriptState.PENDING_REVIEW  # the run still landed
    assert [segment.text for segment in fresh.raw_transcript] == ["t"]


# --- Card #18: Avatar Prompt grounding -----------------------------------
#
# Root cause (see content_drafting.py's module docstring for the full
# writeup): the drafter never received the lesson's title, nothing enforced
# a minimum amount of real source text before calling Gemini, and
# "fill only what's empty" meant a bad draft could never be regenerated.
# These tests check the actual request reaching the drafter, not just that
# some string exists in the output - a generic prompt is syntactically
# identical to a grounded one.


async def _make_pdf_only_topic(pdf_text: str, title: str = "Talking about Music") -> TopicContent:
    topic = TopicContent(title=title, level=Level.INTERMEDIATE, youtube_urls=[], pdf_extracted_text=pdf_text)
    await topic.insert()
    return topic


DAILY_ROUTINE_SOURCE = (
    "This lesson is about describing a typical daily routine. Maria wakes up "
    "at six o'clock every morning. She studies English for twenty minutes "
    "before eating a quick breakfast of fruit and toast. At seven thirty she "
    "starts work. In the evening she compares her morning routine to her "
    "evening routine and talks about which one she prefers."
)

WORKPLACE_MEETING_SOURCE = (
    "This lesson teaches useful language for a team meeting at work. It "
    "covers how to give an opinion, how to agree with a colleague, how to "
    "politely disagree, and how to ask for clarification when you don't "
    "understand something someone said in the meeting."
)


async def test_daily_routine_source_and_level_and_title_reach_the_drafter():
    """TEST 1 (daily routine): the drafter actually receives the real
    source text, the topic's level, and its title - not just a thin or
    empty string - and the application itself never injects any of the
    hardcoded generic topics the production bug produced.
    """
    topic = await _make_topic()
    drafter = FakeContentDrafter()
    service = _make_service(content_drafter=drafter)
    await TopicContentRepository().update_fields(
        topic.id,
        {
            "transcript_state": TranscriptState.APPROVED_FOR_AI.value,
            "approved_transcript": DAILY_ROUTINE_SOURCE,
        },
    )

    await service.run_content_drafting(topic.id)

    assert len(drafter.calls) == 1
    sent_source, sent_level, sent_title = drafter.calls[0]
    assert sent_source == DAILY_ROUTINE_SOURCE
    assert sent_level == Level.INTERMEDIATE
    assert sent_title == "Talking about Music"
    # The real daily-routine vocabulary is in what was sent...
    for keyword in ("wakes up", "breakfast", "studies English", "starts work"):
        assert keyword in sent_source
    # ...and the generic failure-mode topics are nowhere in the *request* -
    # confirming nothing on the application side introduces them.
    for banned in ("sports", "animals", "fashion", "cars"):
        assert banned not in sent_source.lower()


async def test_workplace_meeting_source_reaches_the_drafter():
    """TEST 2 (workplace meeting): same check, different lesson - proves
    this isn't special-cased to one fixture's wording."""
    topic = await _make_topic()
    drafter = FakeContentDrafter()
    service = _make_service(content_drafter=drafter)
    await TopicContentRepository().update_fields(
        topic.id,
        {
            "transcript_state": TranscriptState.APPROVED_FOR_AI.value,
            "approved_transcript": WORKPLACE_MEETING_SOURCE,
        },
    )

    await service.run_content_drafting(topic.id)

    assert len(drafter.calls) == 1
    sent_source = drafter.calls[0][0]
    for keyword in ("meeting", "opinion", "agree", "disagree", "clarification"):
        assert keyword in sent_source.lower()


async def test_pdf_only_topic_drafts_from_the_pdf_text_with_no_video():
    """TEST 3 (PDF only): no video at all - the PDF's extracted text alone
    must reach the drafter and generation must succeed.
    """
    topic = await _make_pdf_only_topic(DAILY_ROUTINE_SOURCE)
    drafter = FakeContentDrafter()
    service = _make_service(content_drafter=drafter)
    await TopicContentRepository().update_fields(
        topic.id,
        {
            "transcript_state": TranscriptState.APPROVED_FOR_AI.value,
            "approved_transcript": DAILY_ROUTINE_SOURCE,
        },
    )

    await service.run_content_drafting(topic.id)

    assert len(drafter.calls) == 1
    assert drafter.calls[0][0] == DAILY_ROUTINE_SOURCE
    refreshed = await TopicContent.get(topic.id)
    assert refreshed.avatar_prompt != ""


async def test_no_usable_source_fails_clearly_instead_of_drafting_generically():
    """TEST 4 (no content): an approved transcript that is empty or only a
    few characters must never reach Gemini at all - a clear refusal beats a
    generic, unrelated prompt. Checked at both call sites: the synchronous
    manual endpoint (a real 422 the admin sees) and the automatic
    post-approval background path (a silent skip, logged, not a draft).
    """
    topic = await _make_topic()
    drafter = FakeContentDrafter()
    service = _make_service(content_drafter=drafter)
    await TopicContentRepository().update_fields(
        topic.id, {"transcript_state": TranscriptState.APPROVED_FOR_AI.value, "approved_transcript": "Hi."}
    )

    with pytest.raises(UnprocessableException) as exc_info:
        await service.start_content_drafting(topic.id)
    assert "Not enough lesson content" in str(exc_info.value)

    await service.run_content_drafting(topic.id)
    assert drafter.calls == []  # never called the model at all
    refreshed = await TopicContent.get(topic.id)
    assert refreshed.avatar_prompt == ""


async def test_an_oversized_draft_is_truncated_to_the_schema_limit():
    """card #19: the richer structured prompt occasionally overshoots its
    own length instruction (observed in real testing: an instructed ~9,500
    character ceiling produced a 10,668-character draft). `MAX_PROMPT_LENGTH`
    (app/schema/limits.py) is enforced on every manual `PATCH /topics/{id}`
    via `TopicUpdate`'s `Prompt` type, but this write is a raw `$set` that
    bypasses that validation - so the service itself must guarantee a stored
    `avatar_prompt` can always be re-saved unchanged later without a surprise
    422 the admin never caused.
    """
    from app.schema.limits import MAX_PROMPT_LENGTH

    topic = await _make_topic()
    drafter = FakeContentDrafter(draft=DraftedContent(avatar_prompt="x" * (MAX_PROMPT_LENGTH + 500)))
    service = _make_service(content_drafter=drafter)
    await TopicContentRepository().update_fields(
        topic.id,
        {"transcript_state": TranscriptState.APPROVED_FOR_AI.value, "approved_transcript": DAILY_ROUTINE_SOURCE},
    )

    await service.run_content_drafting(topic.id)

    refreshed = await TopicContent.get(topic.id)
    assert len(refreshed.avatar_prompt) == MAX_PROMPT_LENGTH


async def test_regenerate_content_overwrites_an_existing_bad_prompt():
    """The structural half of card #18's fix: `run_content_drafting`
    ("fill only what's empty") can never replace a topic's existing
    `avatar_prompt`, even a bad one from an earlier, weaker version of this
    pipeline - `run_regenerate_content` is the only path that can.
    """
    topic = await _make_topic()
    await TopicContentRepository().update_fields(
        topic.id,
        {
            "transcript_state": TranscriptState.APPROVED_FOR_AI.value,
            "approved_transcript": DAILY_ROUTINE_SOURCE,
            "avatar_prompt": "You are Mariam, a friendly speaking partner. Ask about sports, animals, fashion, or cars.",
            "opening_line": "So, do you like sports?",
        },
    )
    fresh_draft = DraftedContent(
        avatar_prompt="You are Mariam, helping an intermediate learner practice describing a daily routine.",
        opening_line="What time do you usually wake up?",
        warm_up_questions=["What do you usually eat for breakfast?"],
        vocabulary=[VocabularyItem(term="wake up", theme="Daily Routine", example_sentence="I wake up at six.")],
    )
    drafter = FakeContentDrafter(draft=fresh_draft)
    service = _make_service(content_drafter=drafter)

    # Fill-only must leave the bad prompt untouched...
    await service.run_content_drafting(topic.id)
    untouched = await TopicContent.get(topic.id)
    assert "sports" in untouched.avatar_prompt.lower()

    # ...but an explicit regenerate replaces it.
    await service.start_regenerate_content(topic.id)
    await service.run_regenerate_content(topic.id)
    regenerated = await TopicContent.get(topic.id)
    assert regenerated.avatar_prompt == fresh_draft.avatar_prompt
    assert regenerated.opening_line == fresh_draft.opening_line
    assert "sports" not in regenerated.avatar_prompt.lower()


async def test_regenerate_content_also_refuses_on_too_thin_a_source():
    topic = await _make_topic()
    await TopicContentRepository().update_fields(
        topic.id, {"transcript_state": TranscriptState.APPROVED_FOR_AI.value, "approved_transcript": "Hi."}
    )
    service = _make_service()

    with pytest.raises(UnprocessableException) as exc_info:
        await service.start_regenerate_content(topic.id)
    assert "Not enough lesson content" in str(exc_info.value)


async def test_approving_a_topic_with_no_transcript_is_rejected():
    """`approved_for_ai` is terminal, so an empty approval would permanently
    hand the AI an empty transcript with no route back.
    """
    repository = TopicContentRepository()
    topic = await _make_topic()
    await repository.update_fields(topic.id, {"transcript_state": TranscriptState.PENDING_REVIEW})

    with pytest.raises(UnprocessableException):
        await _make_service().approve_transcript(topic.id)

    fresh = await repository.get_by_id(topic.id)
    assert fresh.transcript_state == TranscriptState.PENDING_REVIEW
    assert fresh.approved_transcript is None


async def test_teacher_can_save_timestamped_segment_corrections_before_approval():
    topic = await _make_topic()
    repository = TopicContentRepository()
    await repository.update_fields(
        topic.id,
        {
            "transcript_state": TranscriptState.PENDING_REVIEW,
            "raw_transcript": [TranscriptSegment(text="هسه question one", start=0.0, duration=2.0)],
        },
    )

    saved = await _make_service().save_review_segments(
        topic.id,
        [TranscriptSegment(text="هسه السؤال الأول", start=0.0, duration=2.0)],
    )

    assert saved.transcript_state == TranscriptState.PENDING_REVIEW
    assert saved.raw_transcript[0].text == "هسه السؤال الأول"
