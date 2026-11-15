"""A lesson topic: the merged content model from the platform plan (§4.1) -
covers both the lesson catalogue and the richer authored content (warm-up
questions, phrase groups, themed vocabulary, etc.) that the real `.docx`
lesson files actually contain.

Lesson videos are embedded directly from YouTube at launch - no download, no
re-hosting - so there is no media/hosting state here, only the transcript
pipeline's state (captions or the Gemini fallback, see the platform plan §6).
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Optional

from beanie import Indexed
from pydantic import BaseModel, Field

from app.models.base import BaseDocument
from app.utils.time import utcnow


class Level(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class VoicePromptMode(str, Enum):
    SHARED = "shared"  # one prompt (`avatar_prompt`) for Avatar and Voice Practice
    SEPARATE = "separate"  # Voice Practice uses `voice_prompt`


class ArabicInterventionLevel(str, Enum):
    """How often/deeply the teacher wants the optional Jordanian-Arabic
    clarification support (see `TopicContent.arabic_support_enabled`) to kick
    in - only consulted by both agents when that flag is on. Jordanian
    (Ammani-dialect) Arabic specifically, never Egyptian dialect or heavy
    Modern Standard Arabic - see each agent's own prompt module."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TranscriptState(str, Enum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    PENDING_REVIEW = "pending_review"
    APPROVED_FOR_AI = "approved_for_ai"


class TranscriptSource(str, Enum):
    """Which path `TranscriptPipelineService._fetch_transcript` actually took
    - lets the reviewing admin see whether `raw_transcript` is a real YouTube
    caption track or Gemini transcribing the video's audio (the fallback
    when no captions exist). An AI-generated transcript can contain
    mishearings a real caption track wouldn't, so it's worth flagging before
    an admin approves it as-is.
    """

    CAPTIONS = "captions"
    AI_GENERATED = "ai_generated"
    GEMINI_VIDEO = "gemini_video"
    LOCAL_ASR = "local_asr"
    EXTRACTED_TEXT = "extracted_text"
    MIXED = "mixed"


TRANSCRIPT_TRANSITIONS: dict[TranscriptState, set[TranscriptState]] = {
    TranscriptState.QUEUED: {TranscriptState.EXTRACTING},
    TranscriptState.EXTRACTING: {TranscriptState.PENDING_REVIEW, TranscriptState.QUEUED},
    TranscriptState.PENDING_REVIEW: {TranscriptState.APPROVED_FOR_AI, TranscriptState.EXTRACTING},
    TranscriptState.APPROVED_FOR_AI: set(),  # terminal
}


class TranscriptSegment(BaseModel):
    text: str
    start: float
    duration: float


class UsefulPhraseGroup(BaseModel):
    function: str  # "Starting your speech", "Giving opinions", "Concluding", ...
    phrases: list[str]


class VocabularyItem(BaseModel):
    term: str
    theme: str  # "Travel", "Culture", "Describing Experiences", ...
    example_sentence: str
    # Optional: the platform's content is English-only otherwise (see the
    # localization work's own scope note), but a bare English vocabulary list
    # is far less useful to a student still building their English - this one
    # field is content translation, not full localization.
    arabic_meaning: str = ""


class GuidedQuestionPart(BaseModel):
    part_title: str  # "Part 1: Introduction", ...
    questions: list[str]


class SentenceStarterBlock(BaseModel):
    stage: str  # "Introduction", "Describe your favorite artist", ...
    starters: list[str]
    worked_example: str


class PracticeScheduleBlock(BaseModel):
    duration_minutes: int
    activity: str  # "Listening practice", "Shadowing", "Vocabulary notes", "Record yourself"


class ExerciseKind(str, Enum):
    VIDEO = "video"
    VOCAB = "vocab"
    IMAGE_COMPLETION = "image_completion"
    LISTENING = "listening"
    GRAMMAR = "grammar"
    READING = "reading"
    WRITING = "writing"
    SPEAKING = "speaking"
    GAME = "game"
    SERIES = "series"
    MULTIPLE_CHOICE = "multiple_choice"
    REFLECTION = "reflection"
    WORD_ORDER = "word_order"
    ERROR_CORRECTION = "error_correction"
    DIALOGUE_COMPLETION = "dialogue_completion"


class ExercisePair(BaseModel):
    left: str
    right: str


class DialogueTurn(BaseModel):
    """One line of a Dialogue Completion exercise. A non-blank turn is
    shown to the student verbatim (`text`); a blank turn (`is_blank`) shows
    only `speaker` and an input, and is graded against `accepted_answers` -
    which is never sent to a Student (see `app.schema.topic
    .mask_exercises_for_student`)."""

    speaker: str
    text: str = ""
    is_blank: bool = False
    accepted_answers: list[str] = Field(default_factory=list)


class LessonExercise(BaseModel):
    """Teacher-authored practice shown in order in the student lesson player."""

    kind: ExerciseKind
    prompt: str
    options: list[str] = Field(default_factory=list)
    correct_option: Optional[int] = None
    explanation: str = ""
    content: str = ""
    arabic_note: str = ""
    media_url: str = ""
    audio_url: str = ""
    pairs: list[ExercisePair] = Field(default_factory=list)
    # Sentence Building / Word Ordering: the target sentence's words, stored
    # in their CORRECT order. A Student never receives this order verbatim
    # (see `app.schema.topic.mask_exercises_for_student`) and submits the
    # chosen WORDS, in order, as the answer - never an index into any list.
    ordering_words: list[str] = Field(default_factory=list)
    # Error Correction: `content` holds the INCORRECT sentence shown to the
    # student; `accepted_answers` is every acceptable corrected version
    # (never sent to a Student - see `mask_exercises_for_student`). A
    # submission is graded by normalized exact match against any of them.
    accepted_answers: list[str] = Field(default_factory=list)
    # Dialogue Completion: a short multi-turn dialogue with one or more
    # blank turns the student fills in. Each blank's own `accepted_answers`
    # is never sent to a Student.
    dialogue_turns: list[DialogueTurn] = Field(default_factory=list)
    min_words: int = 0
    # Submission policy is part of the authored lesson, so it travels with a
    # pinned Topic revision and cannot be bypassed by a client-side checkbox.
    allow_resubmit: bool = True
    deadline_at: Optional[datetime] = None


class TopicRevisionSnapshot(BaseModel):
    revision: int
    created_at: datetime
    content: dict[str, Any]


class SourceDocumentKind(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    TEXT = "text"


class SourceDocument(BaseModel):
    """One teacher-supplied non-video lesson source (Phase 1 of the Lesson
    Builder transformation): an uploaded PDF/Word/PowerPoint document whose
    text was extracted at upload time, or text the teacher pasted directly.
    Each becomes its own labelled `raw_transcript` segment in
    `TranscriptPipelineService.run_generation`, so the existing review /
    approve / draft pipeline needs no new state. `url`/`public_id` are `None`
    for pasted text (nothing is stored in Cloudinary for it)."""

    kind: SourceDocumentKind
    label: str
    filename: Optional[str] = None
    url: Optional[str] = None
    public_id: Optional[str] = None
    extracted_text: str
    added_at: datetime


class ObjectiveSource(str, Enum):
    MANUAL = "manual"
    AI = "ai"


class LearningObjective(BaseModel):
    """What the STUDENT should be able to say or do by the end - written by
    the teacher, or AI-suggested and then explicitly accepted. Delivered to
    the Student themselves (Course Content) once `objectives_approved` is
    true - see `app.schema.topic.TopicPublic.from_topic`; a draft is
    authoring-only until then."""

    text: str
    source: ObjectiveSource = ObjectiveSource.MANUAL


class LessonFlow(str, Enum):
    """Owner-approved lesson flows. `interactive_video` uses a revisioned
    teacher-owned checkpoint timeline with the native uploaded-video player;
    see `app.core.interactive_video` for publish/delivery validation."""

    STANDARD = "standard"
    INTERACTIVE_VIDEO = "interactive_video"


class InteractiveVideoCompletionRule(str, Enum):
    """What unlocks an Interactive Video checkpoint."""

    SUBMITTED = "submitted"
    CORRECT = "correct"


class InteractiveVideoCheckpoint(BaseModel):
    """A pause point that references the exercise in this Topic revision."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp_seconds: float = Field(ge=0)
    exercise_index: int = Field(ge=0)
    completion_rule: InteractiveVideoCompletionRule = InteractiveVideoCompletionRule.SUBMITTED


class InteractiveVideoTimelineSuggestion(BaseModel):
    """Private AI proposal, never active until a teacher accepts it.

    Older proposals point at an already-authored exercise.  New proposals may
    carry a complete editable exercise draft instead, so the AI can recommend
    both a meaningful video moment and the suitable activity type without
    publishing either one automatically.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp_seconds: float = Field(ge=0)
    exercise_index: Optional[int] = Field(default=None, ge=0)
    completion_rule: InteractiveVideoCompletionRule = InteractiveVideoCompletionRule.SUBMITTED
    rationale: str = Field(default="", max_length=500)
    # Teacher-facing pedagogical context. This stays with the private draft
    # and lets the teacher judge the learning value, not just the wording.
    learning_action: str = Field(default="apply", max_length=40)
    learning_goal: str = Field(default="", max_length=280)
    source_evidence: str = Field(default="", max_length=500)
    exercise: Optional[LessonExercise] = None


class LessonDraftStepState(str, Enum):
    """One step's status within a `LessonDraftJob` - distinct from
    `AIGenerationStatus` (app.models.ai_generation), which tracks one
    provider call; a step here can resolve to `skipped` without ever
    calling a provider at all (see `LessonDraftService._plan`)."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class LessonDraftExerciseSuggestion(BaseModel):
    """One AI-suggested exercise, pending teacher review - never merged into
    `lesson_exercises` (the published/saved list) until the teacher explicitly
    accepts it (see `LessonDraftService.accept_suggestion`). Teacher-rejected
    or never-accepted suggestions are simply removed/replaced; nothing here
    is ever shown to a student."""

    kind: ExerciseKind
    exercise: LessonExercise
    state: LessonDraftStepState = LessonDraftStepState.DONE
    error: Optional[str] = None


class LessonDraftJob(BaseModel):
    """Progress/status for one "Generate Lesson Draft" orchestration run
    (Slice C) - polled via the ordinary `GET /topics/{id}` an admin already
    uses, not a separate endpoint. `content_step` covers avatar_prompt/
    opening_line/warm_up_questions/vocabulary (one combined Gemini call, see
    `TranscriptPipelineService.run_content_drafting`); `exercise_steps` maps
    each planned `ExerciseKind.value` this run attempted to its own status,
    so a teacher sees exactly which exercise kinds succeeded, were skipped
    (already present), or failed - and a failed kind can be retried without
    repeating the ones that already succeeded.
    """

    state: LessonDraftStepState = LessonDraftStepState.PENDING
    content_step: LessonDraftStepState = LessonDraftStepState.PENDING
    exercise_steps: dict[str, LessonDraftStepState] = Field(default_factory=dict)
    # Populated only for a kind whose `exercise_steps` entry is FAILED - the
    # actual reason (a quality-gate rejection's specific issue codes, a
    # provider error, or a budget/quota message), so a Teacher can see WHY a
    # specific kind failed instead of just a bare "Failed" badge, and judge
    # whether retrying that one kind alone is worth another AI call.
    exercise_step_errors: dict[str, str] = Field(default_factory=dict)
    planned_calls: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class TopicContent(BaseDocument):
    title: str
    # Legacy/default difficulty for library filtering and unplaced topics.
    # Program delivery resolves the mapped ProgramLevel difficulty instead.
    level: Level
    content_revision: int = 1
    revision_snapshots: list[TopicRevisionSnapshot] = Field(default_factory=list)
    level_profile_id: Optional[str] = None
    level_name: str = ""
    level_guidance: str = ""
    youtube_urls: list[str] = Field(default_factory=list)
    # Set only via `POST/DELETE /topics/{id}/thumbnail` (an uploaded Cloudinary
    # asset), never accepted as a raw URL on `TopicCreate`/`TopicUpdate` - that
    # would let an admin point a lesson's thumbnail at an arbitrary origin
    # rather than something this server actually hosts. `None` means "use the
    # thumbnail derived from `youtube_urls[0]`" - see `app.core.youtube
    # .derive_thumbnail_url` and `TopicPublic.from_topic`.
    custom_thumbnail_url: Optional[str] = None
    custom_thumbnail_public_id: Optional[str] = None
    # Best-effort, fetched from the YouTube Data API when `youtube_urls`
    # changes (see `app.core.youtube.fetch_video_duration_seconds`) - `None`
    # if there's no video, no API key configured, or the lookup failed.
    # Purely informational (a "video length" badge); nothing enforces it.
    video_duration_seconds: Optional[int] = None
    # Teacher-uploaded source video. Public delivery is intentional: this is
    # lesson material students must be able to watch, never student media.
    uploaded_video_url: Optional[str] = None
    uploaded_video_public_id: Optional[str] = None
    uploaded_video_filename: Optional[str] = None

    # Video-optional lesson source (card #16): an uploaded PDF document a
    # teacher can ground a lesson in instead of, or alongside, a video. Same
    # upload shape as the thumbnail above (url + Cloudinary public id), plus
    # the plain-text extracted from it at upload time - see
    # `app.core.pdf_extraction`. `pdf_extracted_text` (not `youtube_urls`) is
    # what `TranscriptPipelineService` actually reads as source material.
    pdf_url: Optional[str] = None
    pdf_public_id: Optional[str] = None
    pdf_filename: Optional[str] = None
    pdf_extracted_text: Optional[str] = None

    # Additional document/text sources (Phase 1). The legacy `pdf_*` slot
    # above is left untouched for every existing topic; `all_source_documents`
    # presents both uniformly to the pipeline and the API.
    source_documents: list[SourceDocument] = Field(default_factory=list)
    learning_objectives: list[LearningObjective] = Field(default_factory=list)
    # Transient teacher-only AI proposals. Keeping them on the Topic lets a
    # refresh return the same reviewed drafts instead of paying for the same
    # request again. They never become objectives without an explicit save.
    objective_drafts: list[LearningObjective] = Field(default_factory=list)
    objective_draft_generation_key: Optional[str] = None
    # Explicit teacher approval gate: AI drafting that depends on objectives
    # refuses to run while objectives exist but are not yet approved.
    objectives_approved: bool = False
    lesson_flow: LessonFlow = LessonFlow.STANDARD
    # These stay on the reusable/versioned Topic. ProgramTopic pins a whole
    # revision, so a draft timeline can never alter a live student's lesson.
    interactive_video_checkpoints: list[InteractiveVideoCheckpoint] = Field(default_factory=list)
    interactive_video_drafts: list[InteractiveVideoTimelineSuggestion] = Field(default_factory=list)
    interactive_video_draft_generation_key: Optional[str] = None

    # Topic organization by unit + order (card #17). `unit_id` is `None` for
    # a legacy/unassigned topic - the UI groups those under an "Unassigned"
    # bucket per level rather than hiding them. `order_index` breaks ties
    # within that grouping; equal values (every pre-existing topic defaults
    # to 0) fall back to `created_at` for a still-deterministic order - see
    # `TopicContentRepository._paged`.
    unit_id: Annotated[Optional[str], Indexed()] = None
    order_index: int = 0

    warm_up_questions: list[str] = Field(default_factory=list)
    guided_questions: list[GuidedQuestionPart] = Field(default_factory=list)
    useful_phrases: list[UsefulPhraseGroup] = Field(default_factory=list)
    vocabulary: list[VocabularyItem] = Field(default_factory=list)
    sentence_starters: list[SentenceStarterBlock] = Field(default_factory=list)
    suggested_practice_schedule: list[PracticeScheduleBlock] = Field(default_factory=list)
    lesson_exercises: list[LessonExercise] = Field(default_factory=list)
    # AI-suggested exercises awaiting teacher review (Slice C) - never
    # shown to a student and never counted as part of the published
    # lesson; see `LessonDraftExerciseSuggestion`'s own docstring.
    draft_exercises: list[LessonDraftExerciseSuggestion] = Field(default_factory=list)
    lesson_draft_job: Optional[LessonDraftJob] = None

    transcript_state: TranscriptState = TranscriptState.QUEUED
    # When the current `extracting` run claimed this topic. `extracting` is the
    # only state entered by a background task, so it's the only one that can be
    # left behind by a process that died mid-run - and the transition table has
    # no `extracting -> extracting` edge, which would otherwise make a stranded
    # topic permanently un-regenerable. This timestamp is what lets
    # `TranscriptPipelineService` recognise a run as abandoned and reclaim it.
    # `None` in every other state.
    transcript_state_started_at: Optional[datetime] = None
    # A short, teacher-safe recovery message. Provider diagnostics and source
    # content stay in server logs and never leave the backend.
    transcript_error: Optional[str] = None
    raw_transcript: list[TranscriptSegment] = Field(default_factory=list)
    # Parallel to `raw_transcript`: a short human-readable origin label for
    # each segment. Keeping labels separate preserves caption text verbatim
    # while allowing the review UI and downstream AI input to show clear
    # source boundaries when a lesson combines video and documents.
    transcript_source_labels: list[str] = Field(default_factory=list)
    # Set alongside `raw_transcript` when a run completes - `None` until the
    # first one does. See `TranscriptSource`.
    transcript_source: Optional[TranscriptSource] = None
    approved_transcript: Optional[str] = None

    avatar_prompt: str = ""
    opening_line: str = ""
    # Voice Practice (the hosted voice-only agent) reuses `avatar_prompt`
    # unless the teacher explicitly chose separate prompts - see
    # `voice_practice_prompt`. The choice is stored on its own so a leftover
    # `voice_prompt` from an earlier "separate" edit never silently wins after
    # the teacher switches back to "shared". Old lessons default to shared.
    voice_prompt_mode: VoicePromptMode = VoicePromptMode.SHARED
    voice_prompt: str = ""

    # Optional, teacher-controlled Jordanian-Arabic clarification support
    # (English stays the primary practice language either way) - threaded
    # into both agents' job-dispatch metadata by `LiveSessionService
    # .start_session` and turned into an extra, additive instruction block by
    # each agent's own prompt module when it's on.
    #
    # Deliberately `Optional[bool]`, not a plain `bool = False`, to keep
    # three distinct states apart:
    # - `None`: every topic that predates this field (legacy content) -
    #   absent from the stored document, so Beanie fills in this default on
    #   load. The Voice Practice agent treats this the same as its old,
    #   unconditional "you may briefly explain in Arabic if requested" line,
    #   so a pre-existing lesson's behavior there is unchanged byte-for-byte.
    #   The Avatar agent never had any Arabic behavior at all, so for it this
    #   is identical to `False` (no Arabic block added).
    # - `False`: a teacher explicitly turned this off - no Arabic, full stop,
    #   on either agent (overrides the Voice agent's old default-on line).
    # - `True`: the teacher explicitly turned it on, at `arabic_intervention_
    #   level` below.
    arabic_support_enabled: Optional[bool] = None
    arabic_intervention_level: ArabicInterventionLevel = ArabicInterventionLevel.LOW

    # Which Administrator authored this topic - teacher isolation. `None` on
    # every topic that predates this field (legacy content): visible to every
    # Administrator but editable by none until Root explicitly re-owns it -
    # see `TopicContentService.get_own_topic`.
    #
    # A plain indexed id, not a `Link[User]` - same reasoning as
    # `User.created_by_id`: a Link can't be queried by equality, and its
    # `fetch()` returns the Link itself rather than `None` for a dangling
    # target.
    created_by_id: Annotated[Optional[str], Indexed()] = None

    @property
    def all_source_documents(self) -> list[SourceDocument]:
        """Every non-video source in display order: the legacy single PDF
        slot first (when present), then `source_documents`."""
        items: list[SourceDocument] = []
        if self.pdf_extracted_text:
            items.append(SourceDocument(
                kind=SourceDocumentKind.PDF,
                label=f"PDF — {self.pdf_filename or 'PDF document'}",
                filename=self.pdf_filename,
                url=self.pdf_url,
                public_id=self.pdf_public_id,
                extracted_text=self.pdf_extracted_text,
                added_at=self.updated_at,
            ))
        return items + list(self.source_documents)

    @property
    def has_any_source(self) -> bool:
        return bool(self.youtube_urls or self.uploaded_video_url or self.all_source_documents)

    def make_revision_snapshot(self) -> TopicRevisionSnapshot:
        return TopicRevisionSnapshot(
            revision=self.content_revision,
            created_at=utcnow(),
            content=self.model_dump(exclude={"revision_snapshots"}),
        )

    def at_revision(self, revision: int | None) -> "TopicContent":
        if revision is None or revision == self.content_revision:
            return self
        snapshot = next((item for item in self.revision_snapshots if item.revision == revision), None)
        if snapshot is None:
            return self
        # Re-validate nested exercises/transcript models; `model_copy(update=)`
        # deliberately skips validation and would leave nested model dumps as
        # plain dictionaries in student/API and agent paths.
        return self.__class__.model_validate({
            **snapshot.content,
            "revision_snapshots": self.revision_snapshots,
        })

    @property
    def voice_practice_prompt(self) -> str:
        """The prompt the Voice Practice agent receives. Falls back to
        `avatar_prompt` whenever a separate voice prompt isn't selected (or
        was selected but left blank)."""
        if self.voice_prompt_mode == VoicePromptMode.SEPARATE and self.voice_prompt.strip():
            return self.voice_prompt
        return self.avatar_prompt

    class Settings:
        name = "topic_contents"
        # Every query in `TopicContentRepository` filters `is_deleted` and sorts
        # newest-first, then narrows by either `transcript_state` (the teacher
        # review queue, which clients poll) or `level` (a Standard student's
        # entitlement). Without these both were collection scans - and a scan
        # here is unusually expensive because each document carries its whole
        # transcript, so it reads tens of MB to return a handful of rows.
        # Pro entitlement queries by `_id: {$in: [...]}`, already indexed.
        indexes = [
            [("is_deleted", 1), ("transcript_state", 1), ("created_at", -1)],
            [("is_deleted", 1), ("level", 1), ("created_at", -1)],
            # An Administrator's own "My Topics" list (teacher isolation).
            [("is_deleted", 1), ("created_by_id", 1), ("created_at", -1)],
            # Deterministic unit/order display (card #17) - see
            # `TopicContentRepository._paged`.
            [("is_deleted", 1), ("unit_id", 1), ("order_index", 1), ("created_at", 1)],
        ]
