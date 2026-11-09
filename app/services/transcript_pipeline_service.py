"""Builds a teacher-reviewed source document for each lesson.

YouTube URLs keep the established Gemini path. Teacher-uploaded videos use
embedded captions first and local faster-whisper ASR second; uploaded PDFs
use deterministic text extraction. A lesson can combine all three. Source
text stays verbatim in `raw_transcript`, while `transcript_source_labels`
adds explicit boundaries for teacher review and downstream AI generation.

Previously tried fetching real YouTube captions first via the private
InnerTube API (`app.core.innertube_client`, removed), falling back to Gemini
only when captions were unavailable. Dropped entirely once InnerTube proved
consistently blocked in production - every client identity tried
(`WEB`, `ANDROID`, an embedded-player client) got rejected with YouTube's
literal "Sign in to confirm you're not a bot" challenge from the deployed
server's IP, which no request-shape/client-identity change can pass (it
needs either real sign-in cookies or solving a JS challenge). Gemini therefore
remains the YouTube fallback, but is never used for uploaded video files.

Only `topic.youtube_urls[0]` is processed; an uploaded video and a PDF are
separate additional sources.

Video-optional (card #16): a topic may instead (or also) carry an uploaded
PDF, whose text was already extracted at upload time (see
`app.core.pdf_extraction`, `TopicContentService.upload_pdf`) rather than
here - a bad/scanned PDF fails fast as a 422 on upload, not silently inside
this background job. `start_generation` requires at least one of
`youtube_urls`/`pdf_extracted_text`; `run_generation` appends whichever
sources are present as separate `raw_transcript` segments, so a topic with
both is reviewed/approved as one combined text, same as a multi-segment
video always was.

Generation runs in two halves, deliberately: `start_generation` is the
synchronous part (validates state, claims the topic) so a caller in the wrong
state gets a real 409 in the HTTP response; `run_generation` is the slow part
(network calls, retries, minutes of work) meant to run via FastAPI
`BackgroundTasks`, with no caller left to report a failure to.

Three things make that safe, all of them learned from breaking it:

* **Every write is a compare-and-set**, never a whole-document save. The
  background run holds its topic across minutes of network calls; saving that
  snapshot would revert any edit made in the meantime, and - because the
  transition was validated against the *snapshot's* state - could move an
  `approved_for_ai` topic back to `pending_review` and null the
  `approved_transcript`, defeating a state the transition table calls terminal.
  With a CAS, a run whose topic has moved on matches nothing and its result is
  dropped with a warning, which is the correct outcome.
* **`run_generation` catches `Exception`, not just `TranscriptGenerationFailed`.**
  Anything narrower leaves the topic in `extracting` forever: a missing
  `GEMINI_API_KEY` raises `ValueError` from the SDK, a cleared `youtube_urls`
  raises `IndexError`, and neither is a pipeline error type.
* **`extracting` is reclaimable.** It is the only state a background task
  enters, so it's the only one a dead process can strand, and
  `TRANSCRIPT_TRANSITIONS` has no `extracting -> extracting` edge - so without
  this, a topic stranded by a redeploy could never be regenerated through the
  API at all. `transcript_state_started_at` is what makes an abandoned run
  recognisable; see `_extraction_is_abandoned`.
"""

from datetime import timedelta
from typing import Optional

from loguru import logger

from app.core.ai_cost_control import (
    claim_generation,
    compute_generation_key,
    complete_generation,
    fail_generation,
    record_completed_without_tokens,
)
from app.core.config import get_settings
from app.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from app.core.state_machine import InvalidTransition, transition
from app.core.transcription import MODEL as TRANSCRIPTION_MODEL, Transcriber, TranscriptionError, get_transcriber
from app.core.local_video_transcription import UploadedVideoTranscriber, get_uploaded_video_transcriber
from app.core.youtube import canonical_watch_url, fetch_video_duration_seconds
from app.core.content_drafting import (
    GENERATOR_VERSION,
    MODEL as CONTENT_DRAFTING_MODEL,
    ContentDrafter,
    ContentDraftingError,
    get_content_drafter,
)
from app.models.ai_generation import AIGenerationType
from app.models.topic_content import (
    TRANSCRIPT_TRANSITIONS,
    TopicContent,
    TranscriptSegment,
    TranscriptSource,
    TranscriptState,
)
from app.repositories.topic_content_repository import TopicContentRepository
from app.schema.limits import MAX_PROMPT_LENGTH
from app.utils.time import utcnow


class TranscriptGenerationFailed(Exception):
    """Internal only - never escapes `run_generation`. Raised to short-
    circuit the pipeline and get caught in exactly one place, where the
    topic's state is reverted and the failure is logged.
    """


# Card #18: an approved transcript that technically isn't empty but is only
# a handful of characters (a near-blank PDF, a one-word "transcript") gives
# the content drafter nothing real to ground a draft in - better to refuse
# clearly than let the model pad the gap with generic language-learning
# filler unrelated to the lesson. Deliberately low (not a quality bar, just
# "is there anything here at all") - a real short lesson description is
# normal and should still draft fine.
MIN_SOURCE_CHARS_FOR_DRAFTING = 30

NOT_ENOUGH_SOURCE_MESSAGE = (
    "Not enough lesson content is available to generate an Avatar Prompt. "
    "Add a PDF, transcript, or supported lesson material first."
)


class TranscriptPipelineService:
    def __init__(
        self,
        topic_repository: TopicContentRepository,
        transcriber: Transcriber,
        content_drafter: ContentDrafter,
        extraction_timeout: timedelta,
        uploaded_video_transcriber: UploadedVideoTranscriber | None = None,
    ) -> None:
        self.topic_repository = topic_repository
        self.transcriber = transcriber
        self.content_drafter = content_drafter
        self.extraction_timeout = extraction_timeout
        self.uploaded_video_transcriber = uploaded_video_transcriber or get_uploaded_video_transcriber()

    async def start_generation(self, topic_id: str) -> TopicContent:
        topic = await self._get_or_404(topic_id)
        if not topic.youtube_urls and not topic.uploaded_video_url and not topic.pdf_extracted_text:
            # Video-optional (card #16): a topic needs *some* source
            # material - a video, a PDF, or both - never neither.
            raise UnprocessableException("Topic has no video or PDF source to generate from")

        if self._extraction_is_abandoned(topic):
            logger.warning(
                "Reclaiming topic {} stranded in extracting since {}",
                topic_id,
                topic.transcript_state_started_at,
            )
        else:
            # Validated against the transition table first, purely so an invalid
            # request gets a 409 that says which transition it asked for.
            self._advance(topic, TranscriptState.EXTRACTING)

        # The actual guard. The check above reads a snapshot, so on its own two
        # concurrent POSTs would both pass it, both schedule a background run,
        # and duplicate the (billed) transcription. Only one CAS can win.
        claimed = await self.topic_repository.transition_state(
            topic_id,
            expected=topic.transcript_state,
            target=TranscriptState.EXTRACTING,
            transcript_state_started_at=utcnow(),
        )
        if not claimed:
            raise ConflictException("Transcript generation is already running for this topic")
        return await self._get_or_404(topic_id)

    async def run_generation(self, topic_id: str) -> None:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            return  # deleted between the request and this background task running

        try:
            # Video and PDF are independent segments of the same
            # `raw_transcript` list - `approve_transcript`'s join already
            # concatenates every segment's text generically, so a topic
            # with both sources needs no special-casing there.
            segments: list[TranscriptSegment] = []
            sources: list[TranscriptSource] = []
            source_labels: list[str] = []
            if topic.youtube_urls:
                youtube_segments = await self._fetch_transcript(topic.youtube_urls[0])
                segments.extend(youtube_segments)
                sources.append(TranscriptSource.AI_GENERATED)
                source_labels.extend(["YouTube video"] * len(youtube_segments))
                # Usage-log only (not a dedup claim) - duplicate/concurrent
                # transcript generation is already prevented by the
                # transcript_state CAS above (`start_generation`'s
                # `transition_state` call), so this call site's only job here
                # is recording that a paid generation happened. Token counts
                # aren't captured yet for this provider call (see Slice B's
                # own HANDOFF note on why) - honestly `None`, not guessed.
                await record_completed_without_tokens(
                    generation_type=AIGenerationType.TRANSCRIPT_EXTRACTION,
                    model=TRANSCRIPTION_MODEL,
                    topic_id=topic_id,
                )
            if topic.uploaded_video_url:
                result = await self.uploaded_video_transcriber.transcribe(topic.uploaded_video_url)
                filename = self._safe_source_name(topic.uploaded_video_filename or "Uploaded video")
                segments.append(TranscriptSegment(text=result.text, start=0.0, duration=0.0))
                sources.append(result.source)
                source_labels.append(f"Uploaded video — {filename}")
            if topic.pdf_extracted_text:
                filename = self._safe_source_name(topic.pdf_filename or "PDF document")
                segments.append(TranscriptSegment(text=topic.pdf_extracted_text, start=0.0, duration=0.0))
                sources.append(TranscriptSource.EXTRACTED_TEXT)
                source_labels.append(f"PDF — {filename}")
        except Exception as exc:
            # Broad on purpose - see the module docstring. There is no caller
            # left to raise to, and anything that escapes here leaves the topic
            # in `extracting` with a stranded background run behind it.
            logger.opt(exception=True).error("Transcript generation failed for topic {}: {}", topic_id, exc)
            await self._release(topic_id, TranscriptState.QUEUED)
            return

        stored = await self.topic_repository.transition_state(
            topic_id,
            expected=TranscriptState.EXTRACTING,
            target=TranscriptState.PENDING_REVIEW,
            raw_transcript=segments,
            transcript_source_labels=source_labels,
            transcript_source=sources[0] if len(sources) == 1 else TranscriptSource.MIXED,
            transcript_state_started_at=None,
        )
        if not stored:
            # The topic was deleted, reclaimed by a newer run, or already
            # approved while this run was working. Dropping the result is
            # correct; writing it would undo whichever of those happened.
            logger.warning(
                "Discarding a finished transcript for topic {} - it is no longer extracting", topic_id
            )
            return
        logger.info("Transcript ready for review: topic={} segments={}", topic_id, len(segments))

    @staticmethod
    def _safe_source_name(value: str) -> str:
        return value.replace("\r", " ").replace("\n", " ").replace("]", ")").strip()[:160]

    async def approve_transcript(
        self, topic_id: str, edited_transcript: Optional[str] = None
    ) -> TopicContent:
        topic = await self._get_or_404(topic_id)
        self._advance(topic, TranscriptState.APPROVED_FOR_AI)
        if not topic.raw_transcript:
            # `APPROVED_FOR_AI` is terminal, so approving an empty transcript
            # would permanently ship an empty one to the AI with no way back.
            # Gated on the pipeline having produced *something* even when the
            # caller supplies edited text - an admin can correct wording, not
            # fabricate a transcript for a topic that never went through
            # generation at all.
            raise UnprocessableException("There is no transcript to approve for this topic")

        if edited_transcript is not None:
            approved = edited_transcript.strip()
        elif len(set(topic.transcript_source_labels)) > 1:
            groups: list[tuple[str, list[str]]] = []
            for index, segment in enumerate(topic.raw_transcript):
                label = topic.transcript_source_labels[index] if index < len(topic.transcript_source_labels) else f"Source {index + 1}"
                if groups and groups[-1][0] == label:
                    groups[-1][1].append(segment.text.strip())
                else:
                    groups.append((label, [segment.text.strip()]))
            approved = "\n\n".join(f"[SOURCE: {label}]\n{' '.join(texts)}" for label, texts in groups).strip()
        else:
            approved = " ".join(segment.text.strip() for segment in topic.raw_transcript).strip()
        if not approved:
            raise UnprocessableException("There is no transcript to approve for this topic")
        if not await self.topic_repository.transition_state(
            topic_id,
            expected=TranscriptState.PENDING_REVIEW,
            target=TranscriptState.APPROVED_FOR_AI,
            approved_transcript=approved,
            transcript_state_started_at=None,
        ):
            raise ConflictException("This topic's transcript state changed - reload it and try again")
        return await self._get_or_404(topic_id)

    def _require_draftable_source(self, topic: TopicContent) -> None:
        if not topic.approved_transcript:
            raise UnprocessableException("Approve this topic's transcript before drafting content")
        if len(topic.approved_transcript.strip()) < MIN_SOURCE_CHARS_FOR_DRAFTING:
            raise UnprocessableException(NOT_ENOUGH_SOURCE_MESSAGE)

    async def start_content_drafting(self, topic_id: str) -> TopicContent:
        """The synchronous half of a manual re-run of content drafting
        (`POST /topics/{id}/draft-content`) - same split as
        `start_generation`/`run_generation` above, for the same reason: a
        caller with no approved transcript yet gets a real 422 in the HTTP
        response, not a background task that silently no-ops. Same for one
        that exists but is too thin to ground a real draft in - see
        `MIN_SOURCE_CHARS_FOR_DRAFTING`.

        Unlike approval, this can be called any number of times - each run
        is the same "fill only what's still empty" pass as the automatic
        one, so re-running it after an admin has since filled in some fields
        by hand only drafts whatever's left.
        """
        topic = await self._get_or_404(topic_id)
        self._require_draftable_source(topic)
        return topic

    async def start_regenerate_content(self, topic_id: str) -> TopicContent:
        """The synchronous half of a full regenerate (`POST
        /topics/{id}/regenerate-content`) - unlike `start_content_drafting`,
        this is for replacing an existing `avatar_prompt`/`opening_line`/
        `warm_up_questions`/`vocabulary` outright, not just filling gaps.
        Added alongside card #18's grounding fix: without it, a topic
        drafted under an earlier, weaker prompt had no way to ever be
        regenerated - "fill only what's empty" means a bad-but-non-empty
        `avatar_prompt` is permanent. Same source-quality guard applies.
        """
        topic = await self._get_or_404(topic_id)
        self._require_draftable_source(topic)
        return topic

    async def run_content_drafting(self, topic_id: str, requested_by_id: Optional[str] = None) -> None:
        """Runs after `approve_transcript` returns, via `BackgroundTasks` - a
        second real Gemini call per approval, same reasoning as
        `run_generation` for not blocking the approve request on it.

        Drafts `avatar_prompt`/`opening_line`/`warm_up_questions`/`vocabulary`
        together in one call (see `content_drafting.py`), but only ever
        *fills a gap* - each field is written only if it is still at its
        empty default on the stored document, so an admin who already typed
        an avatar prompt (Step3AvatarPrompt is reachable before approval too,
        via the builder's nav rail) never has it overwritten. This re-reads
        the topic right before writing specifically to narrow that window,
        not to close it - an admin saving Step3 in the few seconds between
        approval and this call landing is a real, accepted race, same
        trade-off this class already makes elsewhere for a single-admin tool.

        `approved_transcript` is the only thing this reads, and
        `APPROVED_FOR_AI` is terminal (see `TRANSCRIPT_TRANSITIONS`), so
        approval - and therefore this - fires at most once per topic; there is
        no CAS on the write itself because there is no concurrent *writer* of
        this same background job to race against, only a possible delete.
        """
        await self._run_draft(topic_id, overwrite=False, requested_by_id=requested_by_id)

    async def run_regenerate_content(self, topic_id: str, requested_by_id: Optional[str] = None) -> None:
        """The background half of a full regenerate (`start_regenerate_content`
        above) - same Gemini call as `run_content_drafting`, but every field
        the draft actually returned replaces whatever was there, including a
        non-empty one. The teacher explicitly asked to replace this topic's
        existing avatar prompt/opening line/warm-up questions/vocabulary, so
        there is no gap-filling judgment call to make here.
        """
        await self._run_draft(topic_id, overwrite=True, requested_by_id=requested_by_id)

    async def _run_draft(self, topic_id: str, overwrite: bool, requested_by_id: Optional[str] = None) -> None:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None or not topic.approved_transcript:
            return  # deleted, or approved with empty text (shouldn't happen - guarded in approve_transcript)
        if len(topic.approved_transcript.strip()) < MIN_SOURCE_CHARS_FOR_DRAFTING:
            # Reachable only if the topic's transcript was edited down after
            # `start_content_drafting`/`start_regenerate_content` already
            # passed the same check - `_require_draftable_source` is the
            # real gate; this is a second, cheap check right before the
            # Gemini call itself, not a new error surface.
            logger.info(
                "Skipping content draft for topic {} - source too thin ({} chars)",
                topic_id, len(topic.approved_transcript.strip()),
            )
            return

        if not overwrite and topic.vocabulary and topic.avatar_prompt and topic.opening_line and topic.warm_up_questions:
            # Real, content-hash-independent waste this slice eliminates
            # directly: a "fill only what's empty" pass has nothing left to
            # fill when every target field already has a value - calling
            # Gemini anyway (the pre-Slice-B behavior) would always discard
            # every field it returned. Still runs the Arabic vocabulary
            # backfill below, which has its own independent "anything
            # missing?" gate and is not skipped by this one.
            logger.info("Content draft skipped for topic {} - every target field is already filled", topic_id)
            values: dict = {}
            await self._fill_missing_arabic_meanings(topic_id, values, topic, requested_by_id)
            if values:
                await self.topic_repository.update_fields(topic_id, values)
                logger.info("Content auto-drafted for topic {}: {}", topic_id, sorted(values))
            return

        # Bounds what actually reaches the drafting prompt, independent of
        # whatever cap already applied when the transcript was approved (see
        # Settings.MAX_DRAFTING_SOURCE_CHARS's own comment) - keeps a single
        # drafting call's input tokens, and therefore its cost, bounded.
        source_for_drafting = topic.approved_transcript[: get_settings().MAX_DRAFTING_SOURCE_CHARS]

        # Safe diagnostics only (card #18's investigation asked for exactly
        # this): lesson title and source character count, never the source
        # text itself - this is what should be checked first the next time a
        # generated prompt looks ungrounded, before suspecting the prompt
        # wording again. `GENERATOR_VERSION` (card #20) answers a different
        # question: whether the server process actually has this module's
        # current code loaded at all - see that constant's own comment.
        logger.info(
            "GENERATOR_VERSION={} Drafting content for topic {} title={!r} level={} overwrite={} source_chars={}",
            GENERATOR_VERSION, topic_id, topic.title, topic.level.value, overwrite, len(source_for_drafting),
        )

        generation_type = AIGenerationType.CONTENT_REGENERATE if overwrite else AIGenerationType.CONTENT_DRAFT
        generation_key = compute_generation_key(
            generation_type,
            topic_id=topic_id,
            source=source_for_drafting,
            title=topic.title,
            level=topic.level.value,
            model=CONTENT_DRAFTING_MODEL,
        )
        claim = await claim_generation(
            generation_type=generation_type,
            generation_key=generation_key,
            model=CONTENT_DRAFTING_MODEL,
            topic_id=topic_id,
            requested_by_id=requested_by_id,
        )

        try:
            draft = await self.content_drafter.draft(source_for_drafting, topic.level, topic.title)
        except ContentDraftingError as exc:
            # Best-effort: the teacher can still author all of this by hand in
            # Step3AvatarPrompt, so a failed draft is a missed convenience,
            # not a broken pipeline. The record is marked `failed` and the
            # lock released - so a later request can retry immediately
            # instead of being stuck as "busy".
            await fail_generation(claim, exc)
            logger.opt(exception=True).warning(
                "Content drafting failed for topic {}: {}", topic_id, exc
            )
            return
        usage = getattr(self.content_drafter, "last_usage", None)
        await complete_generation(
            claim,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

        if len(draft.avatar_prompt) > MAX_PROMPT_LENGTH:
            # Defense in depth (card #19): the system prompt instructs a hard
            # character ceiling well under this, but an LLM's own length
            # instructions are a strong preference, not a guarantee - this is
            # the one place that actually can't be violated, since this write
            # bypasses `TopicUpdate`'s own `Prompt` field validation (a raw
            # `$set`, not a schema-checked `PATCH`). Without this, a topic
            # could end up with an `avatar_prompt` the admin's own next save
            # in Step3AvatarPrompt would then 422 on, for a value they never
            # typed themselves.
            logger.warning(
                "Drafted avatar_prompt for topic {} was {} chars - truncating to {}",
                topic_id, len(draft.avatar_prompt), MAX_PROMPT_LENGTH,
            )
            draft.avatar_prompt = draft.avatar_prompt[:MAX_PROMPT_LENGTH]

        current = await self.topic_repository.get_by_id(topic_id)
        if current is None:
            return  # deleted while drafting ran

        values: dict = {}
        if (overwrite or not current.vocabulary) and draft.vocabulary:
            values["vocabulary"] = [item.model_dump() for item in draft.vocabulary]
        if (overwrite or not current.avatar_prompt) and draft.avatar_prompt:
            values["avatar_prompt"] = draft.avatar_prompt
        if (overwrite or not current.opening_line) and draft.opening_line:
            values["opening_line"] = draft.opening_line
        if (overwrite or not current.warm_up_questions) and draft.warm_up_questions:
            values["warm_up_questions"] = draft.warm_up_questions

        await self._fill_missing_arabic_meanings(topic_id, values, current, requested_by_id)

        if not values:
            logger.info("Content drafting had nothing new to fill for topic {}", topic_id)
            return
        await self.topic_repository.update_fields(topic_id, values)
        logger.info(
            "Content {} for topic {}: {}",
            "regenerated" if overwrite else "auto-drafted", topic_id, sorted(values),
        )

    async def _fill_missing_arabic_meanings(
        self, topic_id: str, values: dict, current: TopicContent, requested_by_id: Optional[str] = None
    ) -> None:
        """Backfills `arabic_meaning` on individual vocabulary items that are
        missing just that one field - unlike every other field above, this is
        gap-filling *within* a list the admin may have already curated by
        hand, not a whole-field fill, so it runs whether or not `vocabulary`
        itself was empty: a fresh `draft.vocabulary` can still land with a
        term or two missing a translation, and an admin's own hand-typed
        terms never have one to begin with.

        Mutates `values["vocabulary"]` in place (adding it if this is the
        only thing there was to fill) so the caller's single `update_fields`
        call stays the one write for this whole method.
        """
        vocabulary: list[dict] = (
            values["vocabulary"] if "vocabulary" in values else [item.model_dump() for item in current.vocabulary]
        )
        missing_terms = sorted({item["term"] for item in vocabulary if not item.get("arabic_meaning")})
        if not missing_terms:
            return

        generation_key = compute_generation_key(
            AIGenerationType.VOCABULARY_ARABIC_BACKFILL,
            topic_id=topic_id,
            terms=missing_terms,
            model=CONTENT_DRAFTING_MODEL,
        )
        claim = await claim_generation(
            generation_type=AIGenerationType.VOCABULARY_ARABIC_BACKFILL,
            generation_key=generation_key,
            model=CONTENT_DRAFTING_MODEL,
            topic_id=topic_id,
            requested_by_id=requested_by_id,
        )

        try:
            translations = await self.content_drafter.translate_to_arabic(missing_terms)
        except ContentDraftingError as exc:
            # Same best-effort reasoning as the main draft call: a failed
            # translation pass leaves these terms without an Arabic meaning,
            # exactly as they were before this call - not a broken topic.
            await fail_generation(claim, exc)
            logger.opt(exception=True).warning(
                "Arabic-meaning backfill failed for topic {}: {}", topic_id, exc
            )
            return
        usage = getattr(self.content_drafter, "last_usage", None)
        await complete_generation(
            claim,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

        by_term = {t.term: t.arabic_meaning for t in translations if t.arabic_meaning}
        filled_any = False
        for item in vocabulary:
            if not item.get("arabic_meaning") and item["term"] in by_term:
                item["arabic_meaning"] = by_term[item["term"]]
                filled_any = True

        if filled_any:
            values["vocabulary"] = vocabulary

    async def _get_or_404(self, topic_id: str) -> TopicContent:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        return topic

    def _extraction_is_abandoned(self, topic: TopicContent) -> bool:
        """True when a topic is in `extracting` but its run can no longer be
        alive - the process died, was redeployed, or the task raised something
        that escaped before it could revert the state. A missing timestamp
        counts as abandoned: it means the topic predates this field, so there is
        no evidence of a live run either.
        """
        if topic.transcript_state != TranscriptState.EXTRACTING:
            return False
        started_at = topic.transcript_state_started_at
        return started_at is None or utcnow() - started_at > self.extraction_timeout

    async def _release(self, topic_id: str, target: TranscriptState) -> None:
        if not await self.topic_repository.transition_state(
            topic_id,
            expected=TranscriptState.EXTRACTING,
            target=target,
            transcript_state_started_at=None,
        ):
            logger.warning("Not reverting topic {} to {} - it is no longer extracting", topic_id, target)

    def _advance(self, topic: TopicContent, target: TranscriptState) -> TranscriptState:
        try:
            return transition(topic.transcript_state, target, TRANSCRIPT_TRANSITIONS)
        except InvalidTransition as exc:
            raise ConflictException(str(exc)) from exc

    async def _fetch_transcript(self, youtube_url: str) -> list[TranscriptSegment]:
        # Gemini transcribes the YouTube video directly (no download, no
        # chunking; see `app.core.transcription`'s docstring).
        # `canonical_watch_url` strips `list=`/`index=` first - a URL that
        # still carries those (e.g. one copied from inside a playlist) was
        # observed in production making Gemini treat the request as the
        # whole playlist rather than the one video, tripping its "1 video
        # per request" limit. The result has no real per-word timing, so
        # it's wrapped as a single segment spanning the whole video rather
        # than inventing fake boundaries - `approved_transcript` (what
        # everything downstream actually reads) is just the joined text
        # either way.
        try:
            video_url = canonical_watch_url(youtube_url)
        except ValueError as exc:
            raise TranscriptGenerationFailed(f"Could not resolve a single video from {youtube_url!r}: {exc}") from exc
        try:
            text = await self.transcriber.transcribe(video_url)
        except TranscriptionError as exc:
            raise TranscriptGenerationFailed(f"Video transcription failed: {exc}") from exc
        duration = await fetch_video_duration_seconds(youtube_url) or 0
        return [TranscriptSegment(text=text, start=0.0, duration=float(duration))]


def get_transcript_pipeline_service() -> TranscriptPipelineService:
    settings = get_settings()
    return TranscriptPipelineService(
        TopicContentRepository(),
        get_transcriber(),
        get_content_drafter(),
        extraction_timeout=timedelta(minutes=settings.TRANSCRIPT_EXTRACTION_TIMEOUT_MINUTES),
        uploaded_video_transcriber=get_uploaded_video_transcriber(),
    )
