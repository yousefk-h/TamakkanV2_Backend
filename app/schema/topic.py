"""Topic CRUD (Kanban card #9). `TopicCreate`/`TopicUpdate` deliberately
exclude `transcript_state`, `raw_transcript`, and `approved_transcript` -
those belong to the transcript pipeline (card #11), not to admin-authored
content, and exposing them on a plain content edit would let it corrupt that
state machine by hand.

Three response shapes, because "what a topic looks like" depends on who is
asking and whether it's a list:

* `TopicListItem` - list rows, no transcript for anyone (size).
* `TopicPublic` with `include_internal=False` - a Student's single topic: the
  lesson, but not the avatar prompt or any transcript (disclosure).
* `TopicPublic` with `include_internal=True` - everything, for the
  Administrator/Root who authors and reviews it.

An earlier version had one shape for all three, on the reasoning that "read
access was already open to any authenticated role" - which stopped being true
once entitlement landed in cards #14/#15, and was never true of the AI's own
prompt text.
"""

from datetime import datetime
from typing import Annotated, Optional
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.core.youtube import derive_thumbnail_url, extract_video_id
from app.models.topic_content import (
    ArabicInterventionLevel,
    GuidedQuestionPart,
    ExerciseKind,
    ExercisePair,
    Level,
    LessonExercise,
    LessonDraftExerciseSuggestion,
    LessonDraftJob,
    LessonFlow,
    LearningObjective,
    ObjectiveSource,
    PracticeScheduleBlock,
    SentenceStarterBlock,
    SourceDocument,
    TopicContent,
    TranscriptSegment,
    TranscriptSource,
    TranscriptState,
    UsefulPhraseGroup,
    VocabularyItem,
    VoicePromptMode,
)
from app.schema.base import BaseSchema
from app.schema.challenge import LessonDayState
from app.schema.limits import (
    MAX_LINE_LENGTH,
    MAX_LIST_ITEMS,
    MAX_PROMPT_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TRANSCRIPT_LENGTH,
    MAX_URL_LENGTH,
)


def _must_be_a_youtube_watch_url(url: str) -> str:
    """Validate at the boundary, not deep in the pipeline.

    The stored string is handed verbatim to `yt-dlp`, which happily fetches any
    scheme and host it supports. Today an SSRF is blocked only *accidentally*:
    the captions fetcher runs first and its `_extract_video_id` rejects
    non-YouTube hostnames, so audio extraction is never reached. That gate is in
    a function whose own docstring disclaims any security role, and it
    disappears the moment anything calls the extractor directly or the order in
    `_fetch_transcript` changes. So the allowlist lives here too, where it's the
    field's contract rather than a side effect.

    Scheme is checked as well as host: `urlparse` fills in `hostname` for any
    scheme, so `file://youtube.com/etc/passwd?v=<id>` passes a host-only check.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("youtube_urls must be http(s) URLs")
    if parsed.hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise ValueError(
            f"youtube_urls must point at YouTube (got host {parsed.hostname!r})"
        )
    try:
        extract_video_id(url)
    except ValueError:
        # The single most common way to hit this: pasting a playlist link
        # (`youtube.com/playlist?list=...`) instead of the URL of one video
        # in it - passes the host check above (still youtube.com) but has no
        # `v=` for any video at all, and would otherwise fail much later,
        # deep in the transcript pipeline, with a far less actionable error.
        raise ValueError(
            "youtube_urls must link to a single video, not a playlist "
            f"(could not find a video id in {url!r}) - open the video itself "
            "and copy its URL instead of the playlist's"
        ) from None
    return url


ALLOWED_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
)


Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]
Line = Annotated[str, Field(max_length=MAX_LINE_LENGTH)]
Prompt = Annotated[str, Field(max_length=MAX_PROMPT_LENGTH)]
Transcript = Annotated[str, Field(max_length=MAX_TRANSCRIPT_LENGTH)]
YouTubeUrl = Annotated[
    str, Field(max_length=MAX_URL_LENGTH), AfterValidator(_must_be_a_youtube_watch_url)
]

# Every authored list is capped: without this, one `POST /topics` body could
# carry unbounded elements, and the whole document (transcript included) is
# rewritten on every edit.
#
# Each is spelled out as its own `Annotated[...]` alias rather than built by a
# `_capped_list(item_type)` helper called inline in the field annotations
# below. A function call is not a valid static type expression - Pylance/Pyright
# flags `field: _capped_list(X)` as an error (and Pydantic would still accept it
# happily at runtime, since it only cares what the call returns), so every field
# using it lit up red. A plain module-level `Annotated[...]` assigned to a name,
# the same pattern as `Title`/`Line`/`Prompt`/`YouTubeUrl` above, is something
# both Pydantic and the type checker actually agree is a type alias.
YouTubeUrlList = Annotated[list[YouTubeUrl], Field(max_length=MAX_LIST_ITEMS)]
LineList = Annotated[list[Line], Field(max_length=MAX_LIST_ITEMS)]
GuidedQuestionList = Annotated[list[GuidedQuestionPart], Field(max_length=MAX_LIST_ITEMS)]
UsefulPhraseList = Annotated[list[UsefulPhraseGroup], Field(max_length=MAX_LIST_ITEMS)]
VocabularyList = Annotated[list[VocabularyItem], Field(max_length=MAX_LIST_ITEMS)]
SentenceStarterList = Annotated[list[SentenceStarterBlock], Field(max_length=MAX_LIST_ITEMS)]
PracticeScheduleList = Annotated[list[PracticeScheduleBlock], Field(max_length=MAX_LIST_ITEMS)]


class ExercisePairIn(ExercisePair):
    left: Line
    right: Line


class LessonExerciseIn(LessonExercise):
    prompt: Annotated[str, Field(min_length=1, max_length=MAX_LINE_LENGTH)]
    options: Annotated[list[Line], Field(max_length=6)] = Field(default_factory=list)
    explanation: Line = ""
    content: Annotated[str, Field(max_length=5000)] = ""
    arabic_note: Annotated[str, Field(max_length=1000)] = ""
    media_url: Annotated[str, Field(max_length=MAX_URL_LENGTH)] = ""
    audio_url: Annotated[str, Field(max_length=MAX_URL_LENGTH)] = ""
    pairs: Annotated[list[ExercisePairIn], Field(max_length=8)] = Field(default_factory=list)
    ordering_words: Annotated[list[Line], Field(max_length=20)] = Field(default_factory=list)
    min_words: Annotated[int, Field(ge=0, le=500)] = 0
    allow_resubmit: bool = True
    deadline_at: Optional[datetime] = None

    @model_validator(mode="after")
    def valid_answer(self) -> "LessonExerciseIn":
        if not self.prompt.strip():
            raise ValueError("Exercise prompt cannot be blank")
        answer_kinds = {ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.IMAGE_COMPLETION, ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING}
        if self.kind in answer_kinds:
            if len(self.options) < 2 or any(not option.strip() for option in self.options):
                raise ValueError("Multiple choice exercises need at least two non-empty options")
            if self.correct_option is None or not 0 <= self.correct_option < len(self.options):
                raise ValueError("Select a valid correct option")
        elif self.options or self.correct_option is not None:
            raise ValueError("This exercise type cannot have answer options")
        if self.kind in {ExerciseKind.LISTENING, ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.SERIES} and not self.content.strip():
            raise ValueError("This exercise needs source text or an instruction")
        if self.kind == ExerciseKind.IMAGE_COMPLETION and not self.media_url.startswith(("https://", "http://")):
            raise ValueError("Image completion needs an http(s) image URL")
        if self.audio_url and (self.kind != ExerciseKind.LISTENING or not self.audio_url.startswith(("https://", "http://"))):
            raise ValueError("Only listening exercises accept an http(s) audio URL")
        if self.kind == ExerciseKind.GAME and (len(self.pairs) < 2 or any(not pair.left.strip() or not pair.right.strip() for pair in self.pairs)):
            raise ValueError("A matching game needs at least two complete pairs")
        if self.kind == ExerciseKind.WORD_ORDER:
            if len(self.ordering_words) < 2 or any(not word.strip() for word in self.ordering_words):
                raise ValueError("A word-ordering exercise needs at least two non-empty words, in the correct order")
        elif self.ordering_words:
            raise ValueError("Only a word-ordering exercise can have ordering words")
        return self


LessonExerciseList = Annotated[list[LessonExerciseIn], Field(max_length=MAX_LIST_ITEMS)]


class TopicCreate(BaseModel):
    title: Title
    # Legacy/default library difficulty. A Program placement supplies the
    # authoritative delivery stage/difficulty, so creation no longer forces
    # an author to make a Program-specific decision here.
    level: Level = Level.BEGINNER
    level_profile_id: Optional[str] = None
    youtube_urls: YouTubeUrlList = Field(default_factory=list)
    # Topic organization by unit + order (card #17). `unit_id` is validated
    # against the authenticated admin's own (or legacy/shared) units by
    # `TopicContentService._resolve_unit` - never trusted as-is.
    unit_id: Optional[str] = None
    order_index: int = 0
    warm_up_questions: LineList = Field(default_factory=list)
    guided_questions: GuidedQuestionList = Field(default_factory=list)
    useful_phrases: UsefulPhraseList = Field(default_factory=list)
    vocabulary: VocabularyList = Field(default_factory=list)
    sentence_starters: SentenceStarterList = Field(default_factory=list)
    suggested_practice_schedule: PracticeScheduleList = Field(default_factory=list)
    lesson_exercises: LessonExerciseList = Field(default_factory=list)
    avatar_prompt: Prompt = ""
    opening_line: Line = ""
    voice_prompt_mode: VoicePromptMode = VoicePromptMode.SHARED
    voice_prompt: Prompt = ""
    # Explicit `False`, not the model's `None` default (see `TopicContent
    # .arabic_support_enabled`) - a topic saved through this schema was, by
    # definition, just authored/edited under the new feature, so it is never
    # "legacy/unset", only "off until the teacher turns it on".
    arabic_support_enabled: bool = False
    arabic_intervention_level: ArabicInterventionLevel = ArabicInterventionLevel.LOW
    lesson_flow: LessonFlow = LessonFlow.STANDARD


class TopicUpdate(BaseModel):
    """Every field optional - a partial update only touches what's provided,
    matching `UpdateMe`'s pattern for the same reason.
    """

    title: Optional[Title] = None
    level: Optional[Level] = None
    level_profile_id: Optional[str] = None
    youtube_urls: Optional[YouTubeUrlList] = None
    unit_id: Optional[str] = None
    order_index: Optional[int] = None
    warm_up_questions: Optional[LineList] = None
    guided_questions: Optional[GuidedQuestionList] = None
    useful_phrases: Optional[UsefulPhraseList] = None
    vocabulary: Optional[VocabularyList] = None
    sentence_starters: Optional[SentenceStarterList] = None
    suggested_practice_schedule: Optional[PracticeScheduleList] = None
    lesson_exercises: Optional[LessonExerciseList] = None
    avatar_prompt: Optional[Prompt] = None
    opening_line: Optional[Line] = None
    voice_prompt_mode: Optional[VoicePromptMode] = None
    voice_prompt: Optional[Prompt] = None
    arabic_support_enabled: Optional[bool] = None
    arabic_intervention_level: Optional[ArabicInterventionLevel] = None
    lesson_flow: Optional[LessonFlow] = None


class LearningObjectiveIn(BaseModel):
    """A teacher-reviewed outcome.  This is deliberately separate from the
    generic topic PATCH so changing objectives always goes through the
    explicit approval-invalidating workflow."""

    text: Annotated[str, Field(min_length=1, max_length=400)]
    source: ObjectiveSource = ObjectiveSource.MANUAL

    @model_validator(mode="after")
    def non_blank_text(self) -> "LearningObjectiveIn":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("Learning objective cannot be blank")
        return self


class LearningObjectivesUpdate(BaseModel):
    objectives: Annotated[list[LearningObjectiveIn], Field(max_length=8)]


class ObjectiveSuggestionPublic(BaseModel):
    """A transient AI suggestion.  Provider/model/usage metadata never
    crosses this boundary; accepting it is a separate teacher action."""

    text: str


class ApproveTranscriptRequest(BaseModel):
    """Optional body for `POST /topics/{id}/approve-transcript`. Omitted or
    null keeps the existing behavior (auto-join `raw_transcript`'s segments);
    a non-empty `approved_transcript` lets the reviewing admin ship their
    edited wording instead - `raw_transcript` itself (the pipeline's own
    output) is never mutated, only what gets frozen into `approved_transcript`.
    """

    approved_transcript: Optional[Transcript] = None


class TextSourceCreate(BaseModel):
    """Teacher-pasted lesson source.  It follows the exact same extraction
    review path as uploaded documents, but has no external asset."""

    label: Annotated[str, Field(max_length=200)] = "Pasted text"
    text: Annotated[str, Field(min_length=1, max_length=MAX_TRANSCRIPT_LENGTH)]

    @model_validator(mode="after")
    def non_blank_text(self) -> "TextSourceCreate":
        if not self.text.strip():
            raise ValueError("Pasted source text cannot be blank")
        return self


class ReviewTranscriptEnglishRequest(BaseModel):
    """Body for `POST /topics/{id}/review-transcript-english`. The admin's
    current edit-box text, sent explicitly rather than defaulted server-side
    to the pipeline's auto-join - the whole point is to review exactly what
    they're about to approve, including their own in-progress edits.
    """

    transcript: Transcript


def resolve_thumbnail_url(topic: TopicContent) -> Optional[str]:
    """A manually-uploaded thumbnail always wins; otherwise falls back to the
    one derived for free from the lesson's first YouTube URL - `None` only
    when there's neither (no custom upload and no `youtube_urls` at all, or a
    URL `derive_thumbnail_url` can't parse).
    """
    if topic.custom_thumbnail_url:
        return topic.custom_thumbnail_url
    if topic.youtube_urls:
        return derive_thumbnail_url(topic.youtube_urls[0])
    return None


class TopicListItem(BaseSchema):
    """The list-view shape. Deliberately carries no transcript.

    `raw_transcript` for an 18-minute video is ~400 segments, roughly 40 KB
    serialized, and `approved_transcript` is the same speech again as one
    string. Returning both for every row made `GET /topics` a multi-megabyte
    response that also built tens of thousands of `TranscriptSegment` objects
    per request. Nothing in a list view can display a transcript anyway - the
    review queue lists topics and then opens one, and `GET /topics/{id}` is
    where the transcript belongs.
    """

    title: str
    author_name: Optional[str] = None
    level: Level
    level_profile_id: Optional[str] = None
    level_name: str = ""
    youtube_urls: list[str]
    thumbnail_url: Optional[str] = None
    # Best-effort, from the YouTube Data API - `None` if there's no video,
    # no API key configured, or the lookup failed. Purely informational.
    video_duration_seconds: Optional[int] = None
    # The teacher review queue filters and displays on this, so it stays.
    transcript_state: TranscriptState
    # A Student's own lock/done/current/missed status for this lesson - see
    # `ProgressService.get_calendar_topics_and_statuses`. `None` for an
    # Administrator/Root, who aren't gated by any calendar.
    lesson_status: Optional[LessonDayState] = None
    # This student's allotted Mariam-session minutes for this lesson - see
    # `ProgressService.get_topic_session_minutes`. `None` for an
    # Administrator/Root, same as `lesson_status`.
    session_minutes: Optional[int] = None
    # Which Administrator authored this topic - `None` for legacy topics that
    # predate ownership. Lets the Teacher UI tell "mine" (editable) apart from
    # a read-only legacy row without a second round trip - see
    # `TopicContentService.get_own_topic`.
    created_by_id: Optional[str] = None
    # Topic organization by unit + order (card #17) - `unit_id` is `None`
    # for a legacy/unassigned topic.
    unit_id: Optional[str] = None
    order_index: int = 0
    # Whether this topic has an uploaded PDF source (video-optional, card
    # #16) - the list view doesn't need the extracted text itself (size),
    # same reasoning as leaving transcripts off this shape.
    has_pdf: bool = False
    has_video: bool = False
    has_audio: bool = False
    exercise_count: int = 0
    exercise_kinds: list[ExerciseKind] = Field(default_factory=list)


class TopicPublic(BaseSchema):
    """The single-topic shape.

    `avatar_prompt`/`transcript_state`/`raw_transcript` are Administrator/
    Root-only and come back null/empty for a Student - see `from_topic`'s
    `include_internal`. `approved_transcript` is on every response instead:
    it's meant for a Student to read once a teacher has approved it, and it's
    `None` for everyone until that happens, so there's nothing to gate. All
    four are on one schema rather than split across two so Swagger keeps a
    single, accurate response model for this route.
    """

    title: str
    content_revision: int
    level: Level
    level_profile_id: Optional[str]
    level_name: str
    level_guidance: str
    youtube_urls: list[str]
    thumbnail_url: Optional[str] = None
    # Whether the thumbnail above is a manual upload vs. the free YouTube-
    # derived one - the admin builder needs this to decide whether to offer
    # "remove custom image" or just show the auto-derived preview as read-only.
    has_custom_thumbnail: bool = False
    # Same as `TopicListItem.video_duration_seconds`.
    video_duration_seconds: Optional[int] = None
    uploaded_video_url: Optional[str] = None
    uploaded_video_filename: Optional[str] = None
    warm_up_questions: list[str]
    guided_questions: list[GuidedQuestionPart]
    useful_phrases: list[UsefulPhraseGroup]
    vocabulary: list[VocabularyItem]
    sentence_starters: list[SentenceStarterBlock]
    suggested_practice_schedule: list[PracticeScheduleBlock]
    lesson_exercises: list[LessonExercise]
    opening_line: str
    approved_transcript: Optional[str] = None
    # A Student's own lock/done/current/missed status for this lesson - see
    # `TopicListItem.lesson_status`. `None` for an Administrator/Root.
    lesson_status: Optional[LessonDayState] = None
    # Same as `TopicListItem.session_minutes`.
    session_minutes: Optional[int] = None
    # Same as `TopicListItem.created_by_id`.
    created_by_id: Optional[str] = None
    # Same as `TopicListItem.unit_id`/`order_index`.
    unit_id: Optional[str] = None
    order_index: int = 0

    # --- Administrator/Root only. ---
    # `avatar_prompt` is the instruction text that drives the Mariam avatar: a
    # student who can read it can craft input to steer the tutoring and
    # assessment it encodes. `raw_transcript` is machine output that no teacher
    # has reviewed yet - the pipeline's whole point is that nothing downstream
    # sees it before approval, and `list_all` does not filter by state, so
    # students were receiving `queued`/`extracting` transcripts too.
    avatar_prompt: Optional[str] = None
    # Same disclosure rule as `avatar_prompt` - it drives the Voice Practice
    # agent. `voice_prompt_mode` is the teacher's explicit shared/separate
    # choice (see `TopicContent.voice_practice_prompt`).
    voice_prompt_mode: Optional[VoicePromptMode] = None
    voice_prompt: Optional[str] = None
    # Same disclosure rule as `avatar_prompt`/`voice_prompt_mode` - teacher-
    # authored agent configuration, not student-facing. `arabic_support_enabled`
    # is `None` for a legacy topic that predates this field - see
    # `TopicContent.arabic_support_enabled`.
    arabic_support_enabled: Optional[bool] = None
    arabic_intervention_level: Optional[ArabicInterventionLevel] = None
    transcript_state: Optional[TranscriptState] = None
    raw_transcript: list[TranscriptSegment] = Field(default_factory=list)
    transcript_source_labels: list[str] = Field(default_factory=list)
    # Whether `raw_transcript` came from a real YouTube caption track or
    # Gemini transcribing the video's audio - `None` before the first run
    # completes. See `TranscriptSource`.
    transcript_source: Optional[TranscriptSource] = None
    # Video-optional lesson source (card #16) - `None`/empty when no PDF has
    # been uploaded. Same disclosure rule as `raw_transcript`: this is
    # authoring material, not something a Student reads directly.
    pdf_url: Optional[str] = None
    pdf_filename: Optional[str] = None
    pdf_extracted_text: Optional[str] = None
    # Additional uploaded/pasted authoring sources.  Never included in a
    # Student response because their extracted text is not reviewed content.
    source_documents: Optional[list[SourceDocument]] = None
    # Authoring-only. Students never receive objectives (approved or not),
    # avoiding disclosure of AI/teacher curriculum metadata.
    learning_objectives: list[LearningObjective] = Field(default_factory=list)
    objectives_approved: bool = False
    objective_drafts: list[LearningObjective] = Field(default_factory=list)
    # Authoring-only flow configuration. Interactive Video is publish-gated
    # until the real checkpoint player exists.
    lesson_flow: Optional[LessonFlow] = None
    # AI-suggested exercises awaiting teacher review (Slice C) - same
    # disclosure rule as avatar_prompt/raw_transcript: a draft is authoring
    # material, never shown to a student. See LessonDraftExerciseSuggestion.
    draft_exercises: list[LessonDraftExerciseSuggestion] = Field(default_factory=list)
    lesson_draft_job: Optional[LessonDraftJob] = None

    @classmethod
    def from_topic(
        cls,
        topic: TopicContent,
        include_internal: bool,
        lesson_status: Optional[LessonDayState] = None,
        session_minutes: Optional[int] = None,
    ) -> "TopicPublic":
        """`include_internal` is passed explicitly by the route (from the
        requester's role) rather than inferred here - the decision belongs
        somewhere a reader can see it next to the auth dependency.
        """
        internal = (
            {
                "avatar_prompt": topic.avatar_prompt,
                "voice_prompt_mode": topic.voice_prompt_mode,
                "voice_prompt": topic.voice_prompt,
                "arabic_support_enabled": topic.arabic_support_enabled,
                "arabic_intervention_level": topic.arabic_intervention_level,
                "transcript_state": topic.transcript_state,
                "raw_transcript": topic.raw_transcript,
                "transcript_source_labels": topic.transcript_source_labels,
                "transcript_source": topic.transcript_source,
                "pdf_url": topic.pdf_url,
                "pdf_filename": topic.pdf_filename,
                "pdf_extracted_text": topic.pdf_extracted_text,
                "source_documents": topic.source_documents,
                "learning_objectives": topic.learning_objectives,
                "objectives_approved": topic.objectives_approved,
                "objective_drafts": topic.objective_drafts,
                "lesson_flow": topic.lesson_flow,
                "draft_exercises": topic.draft_exercises,
                "lesson_draft_job": topic.lesson_draft_job,
            }
            if include_internal
            else {}
        )
        return cls(
            id=topic.id,
            created_at=topic.created_at,
            updated_at=topic.updated_at,
            title=topic.title,
            content_revision=topic.content_revision,
            level=topic.level,
            level_profile_id=topic.level_profile_id,
            level_name=topic.level_name,
            level_guidance=topic.level_guidance,
            youtube_urls=topic.youtube_urls,
            thumbnail_url=resolve_thumbnail_url(topic),
            has_custom_thumbnail=bool(topic.custom_thumbnail_url),
            video_duration_seconds=topic.video_duration_seconds,
            uploaded_video_url=topic.uploaded_video_url,
            uploaded_video_filename=topic.uploaded_video_filename,
            warm_up_questions=topic.warm_up_questions,
            guided_questions=topic.guided_questions,
            useful_phrases=topic.useful_phrases,
            vocabulary=topic.vocabulary,
            sentence_starters=topic.sentence_starters,
            suggested_practice_schedule=topic.suggested_practice_schedule,
            lesson_exercises=topic.lesson_exercises if include_internal else [
                exercise.model_copy(update={"correct_option": None}) for exercise in topic.lesson_exercises
            ],
            opening_line=topic.opening_line,
            # Unlike `raw_transcript`, this is meant for a Student to read once
            # a teacher has approved it - it's `None` until that happens
            # regardless of role, so this alone can't leak an unreviewed one.
            approved_transcript=topic.approved_transcript,
            lesson_status=lesson_status,
            session_minutes=session_minutes,
            created_by_id=topic.created_by_id,
            unit_id=topic.unit_id,
            order_index=topic.order_index,
            **internal,
        )


class EnrolledStudent(BaseModel):
    """One row of `TopicAdminDetail.enrolled_students` - just enough to show
    in the admin Topics page, not a full user profile."""

    id: str
    full_name: str
    email: str


class TopicAdminDetail(BaseModel):
    """Administrator/Root (and the authoring Teacher) only - who authored
    this topic and which students currently have it in their plan
    (`User.assigned_topic_ids`). Fetched lazily, on opening a topic's detail
    view, rather than joined onto every `GET /topics` row (payload size - see
    `TopicListItem`'s own docstring for the same reasoning applied there).
    """

    created_by_id: Optional[str] = None
    # `None` for a legacy/shared topic with no recorded author, or if the
    # recorded author's account has since been deleted.
    created_by_name: Optional[str] = None
    created_by_email: Optional[str] = None
    enrolled_students: list[EnrolledStudent] = Field(default_factory=list)
