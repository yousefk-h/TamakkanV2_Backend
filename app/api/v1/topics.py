"""Topic/content CRUD (Kanban card #9) - the Administrator authors topics;
students need at least one to exist before `POST /submissions` is usable at
all (see `app/api/v1/submissions.py`).

Also the transcript pipeline (card #11): generating and approving a topic's
transcript, and the `transcript_state` filter that is the teacher review
queue - see `app/services/transcript_pipeline_service.py` for the pipeline
itself. `review-transcript-english` is a separate, stateless proofreading aid
for the same review queue - see `app/services/transcript_review_service.py`.

And entitlement enforcement (cards #14/#15): `GET /topics/{topic_id}` (card
#14) rejects a Student outside their assigned plan, and `GET /topics` (card
#15's backend slice) now returns only what a Student is actually entitled to
- see `TopicContentService.get_one_for_requester` /
`.list_for_requester`. Card #15's UI half (removing the level-picker so a
Student has no path to even attempt viewing other content) is frontend work,
not this.
"""

from typing import Optional
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile

from app.core.security import get_current_user, require_teacher_or_administrator
from app.core.storage import UploadFailedError, get_image_storage, get_lesson_audio_storage
from app.core.exceptions import UnprocessableException
from app.core.transcript_english_review import TranscriptReviewResult
from app.models.topic_content import Level, TranscriptState
from app.models.user import Role, User
from app.schema.topic import (
    ApproveTranscriptRequest,
    ReviewTranscriptEnglishRequest,
    TopicAdminDetail,
    TopicCreate,
    TopicListItem,
    TopicPublic,
    TopicUpdate,
    TextSourceCreate,
    resolve_thumbnail_url,
)
from app.repositories.topic_content_repository import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.services.topic_content_service import (
    MAX_PDF_UPLOAD_BYTES,
    MAX_SOURCE_DOCUMENT_UPLOAD_BYTES,
    MAX_SOURCE_VIDEO_UPLOAD_BYTES,
    MAX_THUMBNAIL_UPLOAD_BYTES,
    TopicContentService,
    get_topic_content_service,
)
from app.services.transcript_pipeline_service import (
    TranscriptPipelineService,
    get_transcript_pipeline_service,
)
from app.services.transcript_review_service import (
    TranscriptReviewService,
    get_transcript_review_service,
)
from app.services.enrollment_service import EnrollmentService, get_enrollment_service
from app.services.exercise_draft_service import DraftExercisesRequest, DraftExercisesResponse, ExerciseDraftService, get_exercise_draft_service
from app.services.lesson_draft_service import LessonDraftService, get_lesson_draft_service

router = APIRouter()


@router.post("/topics/{topic_id}/exercise-media")
async def upload_exercise_media(topic_id: str, file: UploadFile = File(...),
                                teacher: User = Depends(require_teacher_or_administrator),
                                service: TopicContentService = Depends(get_topic_content_service)) -> dict[str, str]:
    """Upload teacher-owned lesson image or listening audio before saving the block."""
    await service.get_own_topic(topic_id, teacher)
    mime = file.content_type or ""
    image = mime in {"image/jpeg", "image/png", "image/webp"}
    audio = mime in {"audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "audio/ogg"}
    if not image and not audio:
        raise UnprocessableException("Use a JPEG, PNG, WebP, MP3, M4A, WAV, WebM or OGG file")
    max_bytes = (5 if image else 15) * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise UnprocessableException("File is empty or larger than the lesson media limit")
    public_id = f"Tamakkan/lesson-{('images' if image else 'audio')}/{topic_id}-{uuid.uuid4().hex}"
    try:
        result = await (get_image_storage() if image else get_lesson_audio_storage()).upload(data, public_id)
    except UploadFailedError as exc:
        raise UnprocessableException(f"Could not upload lesson media: {exc}") from exc
    return {"url": result.secure_url, "kind": "image" if image else "audio"}


@router.post("/topics", response_model=TopicPublic)
async def create_topic(
    body: TopicCreate,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only. Author a new lesson topic - owned by the
    authenticated Administrator (teacher isolation), never a client-supplied
    owner.
    """
    topic = await service.create(body, created_by=admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.get("/topics", response_model=list[TopicListItem])
async def list_topics(
    transcript_state: Optional[TranscriptState] = Query(
        None, description="Filter to topics in this transcript pipeline state - e.g. pending_review for the teacher review queue."
    ),
    limit: int = Query(DEFAULT_PAGE_SIZE, gt=0, le=MAX_PAGE_SIZE, description="Page size."),
    skip: int = Query(0, ge=0, description="Number of topics to skip, for paging."),
    search: Optional[str] = Query(None, min_length=1, max_length=100, description="Literal title, vocabulary, or exercise-prompt search."),
    level: Optional[Level] = Query(None, description="Suggested/default difficulty filter."),
    requester: User = Depends(get_current_user),
    service: TopicContentService = Depends(get_topic_content_service),
) -> list[TopicListItem]:
    """List topics, newest first, paged. Root sees everything; an
    Administrator sees only topics they authored plus legacy ones with no
    recorded author (teacher isolation - see `TopicContentService
    .get_own_topic`); a Student sees only topics within their assigned plan
    (Kanban card #15) - empty if they have no plan assigned yet.

    Returns `TopicListItem`, which carries no transcript - fetch a single topic
    for that. See the schema for why.
    """
    triples = await service.list_for_requester(
        requester, transcript_state=transcript_state, limit=limit, skip=skip, search=search, level=level
    )
    creator_ids = list({topic.created_by_id for topic, _, _ in triples if topic.created_by_id})
    creators = await User.find({"_id": {"$in": creator_ids}, "is_deleted": False}).to_list() if creator_ids else []
    creator_names = {creator.id: creator.full_name for creator in creators}
    return [
        TopicListItem(
            id=t.id,
            created_at=t.created_at,
            updated_at=t.updated_at,
            title=t.title,
            author_name=creator_names.get(t.created_by_id) if t.created_by_id else None,
            level=t.level,
            level_profile_id=t.level_profile_id,
            level_name=t.level_name,
            youtube_urls=t.youtube_urls,
            thumbnail_url=resolve_thumbnail_url(t),
            video_duration_seconds=t.video_duration_seconds,
            transcript_state=t.transcript_state,
            lesson_status=status,
            session_minutes=session_minutes,
            created_by_id=t.created_by_id,
            unit_id=t.unit_id,
            order_index=t.order_index,
            has_pdf=t.pdf_extracted_text is not None,
            has_video=bool(t.youtube_urls or t.uploaded_video_url),
            has_audio=any(exercise.audio_url for exercise in t.lesson_exercises),
            exercise_count=len(t.lesson_exercises),
            exercise_kinds=list(dict.fromkeys(exercise.kind for exercise in t.lesson_exercises)),
        )
        for t, status, session_minutes in triples
    ]


@router.get("/topics/{topic_id}", response_model=TopicPublic)
async def get_topic(
    topic_id: str,
    enrollment_id: Optional[str] = Query(None, description="Required only if you have more than one active enrollment."),
    requester: User = Depends(get_current_user),
    service: TopicContentService = Depends(get_topic_content_service),
    enrollments: EnrollmentService = Depends(get_enrollment_service),
) -> TopicPublic:
    """Fetch a single topic by id. For a Student, rejected if the topic is
    outside their assigned plan/enrollment (Kanban card #14) - resolved
    against `enrollment_id` when given, the student's one unambiguous active
    enrollment otherwise, or the legacy `program_id`/`program_level_id` for an
    account with no Enrollment at all (see `EnrollmentService.resolve_context`).
    For an Administrator, rejected if authored by a different Administrator
    (teacher isolation) - Root is unaffected.
    """
    context = await enrollments.resolve_context(requester, enrollment_id) if requester.role == Role.STUDENT else None
    topic, lesson_status, session_minutes = await service.get_one_for_requester(topic_id, requester, context)
    return TopicPublic.from_topic(
        topic, include_internal=requester.role != Role.STUDENT, lesson_status=lesson_status, session_minutes=session_minutes
    )


@router.get("/topics/{topic_id}/admin-detail", response_model=TopicAdminDetail)
async def get_topic_admin_detail(
    topic_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicAdminDetail:
    """Administrator/Root (and the authoring Teacher) only. Who created this
    topic and which students currently have it in their plan - shown in the
    admin Topics page's detail view, fetched on demand rather than on every
    list row (see `TopicAdminDetail`'s docstring).
    """
    return await service.get_admin_detail(topic_id, admin)


@router.patch("/topics/{topic_id}", response_model=TopicPublic)
async def update_topic(
    topic_id: str,
    body: TopicUpdate,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may edit any). Partially update a topic - only the fields you
    provide are changed; everything else is left as-is.
    """
    topic = await service.update(topic_id, body, admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/draft-exercises", response_model=DraftExercisesResponse)
async def draft_lesson_exercises(
    topic_id: str,
    body: DraftExercisesRequest,
    teacher: User = Depends(require_teacher_or_administrator),
    service: ExerciseDraftService = Depends(get_exercise_draft_service),
) -> DraftExercisesResponse:
    """Generate editable suggestions from approved source text; never saves them."""
    return await service.draft(topic_id, teacher, body)


@router.post("/topics/{topic_id}/generate-lesson-draft", response_model=TopicPublic, status_code=202)
async def generate_lesson_draft(
    topic_id: str,
    background_tasks: BackgroundTasks,
    teacher: User = Depends(require_teacher_or_administrator),
    service: LessonDraftService = Depends(get_lesson_draft_service),
) -> TopicPublic:
    """The unified "Generate Lesson Draft" action (Slice C): one request
    orchestrates content drafting (whatever of avatar_prompt/opening_line/
    warm_up_questions/vocabulary is still empty) and a small, bounded
    default set of exercise-kind suggestions, reusing the exact same
    cost-controlled call sites as the existing per-field/per-card actions.
    422 before any provider call if the topic's remaining daily generation
    budget can't cover the planned number of requests, or if there is
    nothing left to draft. 409 if a draft is already running for this
    topic. Exercise suggestions land in `draft_exercises`, never directly
    in the published `lesson_exercises` - see accept/reject/regenerate
    below. Poll `GET /topics/{topic_id}` for `lesson_draft_job` progress.
    """
    topic = await service.start(topic_id, teacher)
    background_tasks.add_task(service.run, topic_id, teacher.id)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/draft-exercises/{index}/accept", response_model=TopicPublic)
async def accept_lesson_draft_exercise(
    topic_id: str,
    index: int,
    teacher: User = Depends(require_teacher_or_administrator),
    service: LessonDraftService = Depends(get_lesson_draft_service),
) -> TopicPublic:
    """Moves one pending AI-suggested exercise into the real, published
    `lesson_exercises` list. Never touches any other suggestion or any
    manually-authored exercise."""
    topic = await service.accept_suggestion(topic_id, index, teacher)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/draft-exercises/{index}/reject", response_model=TopicPublic)
async def reject_lesson_draft_exercise(
    topic_id: str,
    index: int,
    teacher: User = Depends(require_teacher_or_administrator),
    service: LessonDraftService = Depends(get_lesson_draft_service),
) -> TopicPublic:
    """Discards one pending AI-suggested exercise. It was never saved
    anywhere a student could reach it."""
    topic = await service.reject_suggestion(topic_id, index, teacher)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/draft-exercises/{index}/regenerate", response_model=TopicPublic)
async def regenerate_lesson_draft_exercise(
    topic_id: str,
    index: int,
    teacher: User = Depends(require_teacher_or_administrator),
    service: LessonDraftService = Depends(get_lesson_draft_service),
) -> TopicPublic:
    """Replaces one pending suggestion with a fresh draft of the same
    exercise kind - goes through the same concurrent-duplicate guard and
    daily budget check as the initial draft. Never touches any other
    suggestion or any already-accepted/manually-authored exercise."""
    topic = await service.regenerate_suggestion(topic_id, index, teacher)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.delete("/topics/{topic_id}", status_code=204)
async def delete_topic(
    topic_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> None:
    """Administrator only, and only a topic you authored - or any
    legacy/shared topic (`created_by_id is None`), which any Administrator
    may delete even though only Root may edit one (see
    `TopicContentService.get_own_or_shared_topic_for_delete`). Soft-delete -
    it disappears from listings and lookups, but the record itself is kept,
    not erased.
    """
    await service.delete(topic_id, admin)


@router.post("/topics/{topic_id}/thumbnail", response_model=TopicPublic)
async def upload_topic_thumbnail(
    topic_id: str,
    file: UploadFile = File(..., description=f"A cover image, max {MAX_THUMBNAIL_UPLOAD_BYTES // (1024 * 1024)}MB."),
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may edit any). Uploads (or replaces) a lesson's custom cover image.
    Without one, `thumbnail_url` on every topic response falls back to a
    thumbnail derived for free from the lesson's first YouTube URL.
    """
    file_bytes = await file.read(MAX_THUMBNAIL_UPLOAD_BYTES + 1)
    topic = await service.upload_thumbnail(
        topic_id, file_bytes, file.content_type or "application/octet-stream", admin
    )
    return TopicPublic.from_topic(topic, include_internal=True)


@router.delete("/topics/{topic_id}/thumbnail", response_model=TopicPublic)
async def remove_topic_thumbnail(
    topic_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may edit any). Removes a topic's custom cover image, reverting
    `thumbnail_url` to the YouTube-derived fallback (or null, if the topic has
    no `youtube_urls`).
    """
    topic = await service.remove_thumbnail(topic_id, admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/pdf", response_model=TopicPublic)
async def upload_topic_pdf(
    topic_id: str,
    file: UploadFile = File(..., description=f"A lesson source PDF, max {MAX_PDF_UPLOAD_BYTES // (1024 * 1024)}MB."),
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may edit any). Uploads (or replaces) a lesson's source PDF -
    video-optional content (card #16). The PDF's text is extracted
    immediately; a scanned/image-only PDF with no readable text is rejected
    here (422), not silently stored.
    """
    file_bytes = await file.read(MAX_PDF_UPLOAD_BYTES + 1)
    topic = await service.upload_pdf(
        topic_id, file_bytes, file.content_type or "application/octet-stream", file.filename or "lesson.pdf", admin
    )
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/documents", response_model=TopicPublic)
async def upload_topic_document(
    topic_id: str,
    file: UploadFile = File(..., description=f"A PDF, DOCX, or PPTX lesson source, max {MAX_SOURCE_DOCUMENT_UPLOAD_BYTES // (1024 * 1024)}MB."),
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Adds (rather than replaces) a document source.  Extraction is
    synchronous and validation failures are teacher-safe 422 messages."""
    file_bytes = await file.read(MAX_SOURCE_DOCUMENT_UPLOAD_BYTES + 1)
    topic = await service.upload_document(
        topic_id, file_bytes, file.content_type or "application/octet-stream", file.filename or "lesson-source", admin
    )
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/text-source", response_model=TopicPublic)
async def add_topic_text_source(
    topic_id: str,
    body: TextSourceCreate,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    topic = await service.add_text_source(topic_id, body.label, body.text, admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.delete("/topics/{topic_id}/documents/{index}", response_model=TopicPublic)
async def remove_topic_document(
    topic_id: str,
    index: int,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    topic = await service.remove_source_document(topic_id, index, admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/source-video", response_model=TopicPublic)
async def upload_topic_source_video(
    topic_id: str,
    file: UploadFile = File(..., description="Teacher lesson video, max 80 MB."),
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    file_bytes = await file.read(MAX_SOURCE_VIDEO_UPLOAD_BYTES + 1)
    topic = await service.upload_source_video(
        topic_id, file_bytes, file.content_type or "application/octet-stream", file.filename or "lesson-video.mp4", admin
    )
    return TopicPublic.from_topic(topic, include_internal=True)


@router.delete("/topics/{topic_id}/source-video", response_model=TopicPublic)
async def remove_topic_source_video(
    topic_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    return TopicPublic.from_topic(await service.remove_source_video(topic_id, admin), include_internal=True)


@router.delete("/topics/{topic_id}/pdf", response_model=TopicPublic)
async def remove_topic_pdf(
    topic_id: str,
    admin: User = Depends(require_teacher_or_administrator),
    service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may edit any). Removes a topic's source PDF and its extracted text.
    """
    topic = await service.remove_pdf(topic_id, admin)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/generate-transcript", response_model=TopicPublic, status_code=202)
async def generate_transcript(
    topic_id: str,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_teacher_or_administrator),
    service: TranscriptPipelineService = Depends(get_transcript_pipeline_service),
    topic_service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may act on any). Starts transcript generation in the background -
    YouTube captions first, falling back to audio extraction + Gemini
    transcription if the video has none. Returns immediately with the topic
    now in `extracting` state; poll `GET /topics/{topic_id}` for
    `pending_review` (ready to review) or back to `queued` (failed - check
    the server logs, then try again).
    """
    await topic_service.get_own_topic(topic_id, admin)
    topic = await service.start_generation(topic_id)
    background_tasks.add_task(service.run_generation, topic_id)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/approve-transcript", response_model=TopicPublic)
async def approve_transcript(
    topic_id: str,
    background_tasks: BackgroundTasks,
    body: ApproveTranscriptRequest = ApproveTranscriptRequest(),
    admin: User = Depends(require_teacher_or_administrator),
    service: TranscriptPipelineService = Depends(get_transcript_pipeline_service),
    topic_service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may act on any). Approves a topic's reviewed transcript for Mariam to
    use - moves `pending_review` -> `approved_for_ai` and freezes
    `approved_transcript`. Terminal: once approved, there is no API path back
    to an earlier state.

    An optional body's `approved_transcript` lets the reviewing admin ship
    their edited wording instead of the pipeline's raw auto-join; omitted or
    null keeps the previous auto-join behavior.

    Also kicks off content drafting in the background - a second Gemini call
    over the now-frozen transcript, filling `avatar_prompt`/`opening_line`/
    `warm_up_questions`/`vocabulary` (whichever of those the admin hasn't
    already written) as a starting point still editable in the builder. Not
    awaited: it has no bearing on whether approval itself succeeded.
    """
    await topic_service.get_own_topic(topic_id, admin)
    topic = await service.approve_transcript(topic_id, edited_transcript=body.approved_transcript)
    background_tasks.add_task(service.run_content_drafting, topic_id, admin.id)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/draft-content", response_model=TopicPublic, status_code=202)
async def draft_content(
    topic_id: str,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_teacher_or_administrator),
    service: TranscriptPipelineService = Depends(get_transcript_pipeline_service),
    topic_service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may act on any). Manually re-runs content drafting - the same
    "fill only what's still empty" pass that runs automatically right after
    approval (see `TranscriptPipelineService.run_content_drafting`). Useful
    when the first draft missed a field (e.g. a Gemini hiccup left
    vocabulary empty) or an admin cleared a field and wants it redrafted
    without re-approving anything. Never overwrites a field that already has
    a value. 422 if the transcript isn't approved yet - there's nothing to
    draft from.
    """
    await topic_service.get_own_topic(topic_id, admin)
    topic = await service.start_content_drafting(topic_id)
    background_tasks.add_task(service.run_content_drafting, topic_id, admin.id)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/regenerate-content", response_model=TopicPublic, status_code=202)
async def regenerate_content(
    topic_id: str,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_teacher_or_administrator),
    service: TranscriptPipelineService = Depends(get_transcript_pipeline_service),
    topic_service: TopicContentService = Depends(get_topic_content_service),
) -> TopicPublic:
    """Administrator only, and only a topic you authored (teacher isolation;
    Root may act on any). Replaces `avatar_prompt`/`opening_line`/
    `warm_up_questions`/`vocabulary` outright with a fresh draft, including
    any of those that already have a value - unlike `draft-content`, which
    only fills a gap. For when an existing draft is simply wrong (e.g. it
    predates a grounding fix) rather than merely incomplete. 422 if there's
    no approved transcript, or not enough of one, to draft from.
    """
    await topic_service.get_own_topic(topic_id, admin)
    topic = await service.start_regenerate_content(topic_id)
    background_tasks.add_task(service.run_regenerate_content, topic_id, admin.id)
    return TopicPublic.from_topic(topic, include_internal=True)


@router.post("/topics/{topic_id}/review-transcript-english", response_model=TranscriptReviewResult)
async def review_transcript_english(
    topic_id: str,
    body: ReviewTranscriptEnglishRequest,
    admin: User = Depends(require_teacher_or_administrator),
    service: TranscriptReviewService = Depends(get_transcript_review_service),
) -> TranscriptReviewResult:
    """Administrator only. Proofreads the given transcript text for English
    mistakes (grammar, word choice, punctuation) before approval - purely
    advisory, this does not touch the stored topic at all. The admin decides
    whether to apply `corrected_transcript` into their edit box, tweak it
    further, or ignore it and approve the original.
    """
    return await service.review_english(topic_id, body.transcript, requested_by_id=admin.id)
