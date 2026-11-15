"""Topic/content CRUD (Kanban card #9), single-topic entitlement rejection
(card #14's `get_one_for_requester`), and now the list's own entitlement
scoping (card #15's backend slice - `list_for_requester`): a Student's `GET
/topics` shows only what they're actually entitled to, the same rule as
`get_one_for_requester`, reused rather than re-derived. `get_one`/`list_all`
(no requester) stay available for internal/Administrator use (`update`,
`delete`, the transcript pipeline, ...), which was never entitlement-gated
in the first place - entitlement is a Student concept.
"""

import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.services.enrollment_service import EnrollmentContext

from loguru import logger

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.core.interactive_video import validate_interactive_video_ready, validate_interactive_video_timeline
from app.core.document_extraction import DocumentExtractionError, extract_document_text
from app.core.pdf_extraction import PdfExtractionError, extract_pdf_text
from app.core.storage import DocumentStorage, ImageStorage, UploadFailedError, get_document_storage, get_image_storage, get_lesson_audio_storage
from app.core.youtube import fetch_video_duration_seconds
from app.models.topic_content import (
    CheckpointKind, InteractiveVideoCheckpoint, LessonFlow, LearningObjective, SourceDocument,
    SourceDocumentKind, TopicContent, TranscriptState,
)
from app.models.program_topic import ProgramTopic, ProgramTopicStatus
from app.models.level_profile import LevelProfile
from app.models.user import Role, User
from app.repositories.topic_content_repository import DEFAULT_PAGE_SIZE, TopicContentRepository
from app.repositories.unit_repository import UnitRepository
from app.repositories.user_repository import UserRepository
from app.schema.challenge import LessonDayState
from app.schema.topic import EnrolledStudent, LearningObjectivesUpdate, LessonExerciseIn, TopicAdminDetail, TopicCreate, TopicUpdate
from app.services.progress_service import ProgressService, get_progress_service
from app.utils.time import utcnow

# Generous headroom over any real lesson-cover photo - this is a thumbnail,
# never a source asset, so there is no reason for it to approach the size of
# an actual audio/video submission.
MAX_THUMBNAIL_UPLOAD_BYTES = 5 * 1024 * 1024
# New uploads only: existing thumbnails keep their stored public id.
CLOUDINARY_THUMBNAIL_FOLDER = "Tamakkan/topic-thumbnails"
# A lesson PDF is source material to extract text from, not a source asset
# either - generous headroom over any real lesson document.
MAX_PDF_UPLOAD_BYTES = 20 * 1024 * 1024
CLOUDINARY_PDF_FOLDER = "Tamakkan/topic-pdfs"
MAX_SOURCE_DOCUMENT_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SOURCE_DOCUMENTS = 12
CLOUDINARY_SOURCE_DOCUMENT_FOLDER = "Tamakkan/topic-source-documents"
MAX_SOURCE_VIDEO_UPLOAD_BYTES = 80 * 1024 * 1024
CLOUDINARY_SOURCE_VIDEO_FOLDER = "Tamakkan/topic-videos"


class TopicContentService:
    def __init__(
        self,
        topic_repository: TopicContentRepository,
        progress_service: ProgressService,
        image_storage: Optional[ImageStorage] = None,
        document_storage: Optional[DocumentStorage] = None,
        video_storage=None,
        unit_repository: Optional[UnitRepository] = None,
        user_repository: Optional[UserRepository] = None,
    ) -> None:
        self.topic_repository = topic_repository
        self.progress_service = progress_service
        self.image_storage = image_storage or get_image_storage()
        self.document_storage = document_storage or get_document_storage()
        self.video_storage = video_storage or get_lesson_audio_storage()
        self.unit_repository = unit_repository or UnitRepository()
        self.user_repository = user_repository or UserRepository()

    async def _start_content_revision(self, topic: TopicContent) -> dict:
        """Prepare an editable revision without changing what published
        Program placements currently deliver.

        Legacy published placements did not record a revision. Pin them to
        the current document before the edit, then advance the library topic
        to a new revision. Draft placements always follow the latest draft.
        """
        published = await ProgramTopic.find({
            "topic_id": topic.id,
            "status": ProgramTopicStatus.PUBLISHED.value,
            "is_deleted": False,
        }).to_list()
        snapshots = list(topic.revision_snapshots)
        if published and not any(item.revision == topic.content_revision for item in snapshots):
            snapshots.append(topic.make_revision_snapshot())
        for placement in published:
            if placement.published_revision is None:
                placement.published_revision = topic.content_revision
                await placement.save()
        return {
            "content_revision": topic.content_revision + 1,
            "revision_snapshots": snapshots,
        }

    async def _student_delivery_topic(
        self, topic: TopicContent, student: User, enrollment_context: Optional["EnrollmentContext"] = None
    ) -> TopicContent:
        """Return the revision explicitly published for this student's
        Program. Legacy/custom calendars fall back to the current revision.
        """
        program_id = enrollment_context.program_id if enrollment_context is not None else student.program_id
        delivery = topic
        if program_id:
            placement_query = {
                "program_id": program_id,
                "topic_id": topic.id,
                "status": ProgramTopicStatus.PUBLISHED.value,
                "is_deleted": False,
            }
            # A live Enrollment's stage is part of the delivery identity. A
            # similarly named Topic in another stage of the same Program must
            # never select a revision for this student.
            if enrollment_context is not None and enrollment_context.program_level_id:
                placement_query["program_level_id"] = enrollment_context.program_level_id
            placement = await ProgramTopic.find_one(placement_query)
            if placement is None:
                if enrollment_context is not None:
                    raise ForbiddenException("This lesson is not published for your enrolled program stage")
            else:
                delivery = topic.at_revision(placement.published_revision)

        # Old/manual placements may predate the Interactive Video publication
        # guard. Keep those malformed revisions out of student delivery.
        if delivery.lesson_flow == LessonFlow.INTERACTIVE_VIDEO:
            try:
                validate_interactive_video_ready(delivery)
            except UnprocessableException as exc:
                # A legacy/manual malformed placement must never become a
                # partially functional student lesson merely because it
                # predates the publication guard.
                raise ForbiddenException("This Interactive Video lesson is not available yet") from exc
        return delivery

    async def _resolve_unit_id(self, unit_id: Optional[str], level, admin: User) -> Optional[str]:
        """Validates a client-given `unit_id` before it's ever trusted: the
        unit must exist, must be the teacher's own or legacy/shared (the
        same visibility rule as a Topic - `UnitService.get_own_unit`'s
        reasoning applies here too, since assigning into someone else's
        private unit would be a quieter version of the same cross-teacher
        leak), and must be for the same `Level` as the topic itself, or a
        topic could show up grouped under a unit from a different level.
        """
        if unit_id is None:
            return None
        unit = await self.unit_repository.get_by_id(unit_id)
        if unit is None:
            raise NotFoundException("Unit not found")
        if admin.role not in (Role.ROOT, Role.ADMINISTRATOR) and unit.created_by_id not in (None, admin.id):
            raise ForbiddenException("This unit belongs to a different teacher")
        if unit.level != level:
            raise UnprocessableException("A topic's unit must be for the same level as the topic")
        return unit_id

    async def create(self, body: TopicCreate, created_by: User) -> TopicContent:
        # Ownership always comes from the authenticated Administrator, never
        # from the request body - `TopicCreate` has no field for it at all.
        data = body.model_dump()
        if body.level_profile_id:
            profile = await LevelProfile.find_one({"_id": body.level_profile_id, "is_deleted": False, "is_active": True})
            if profile is None:
                raise UnprocessableException("Selected curriculum level no longer exists")
            data.update(level=profile.base_level, level_name=profile.name, level_guidance=profile.avatar_guidance)
        unit_id = await self._resolve_unit_id(body.unit_id, data["level"], created_by)
        topic = TopicContent(**{**data, "unit_id": unit_id}, created_by_id=created_by.id)
        if topic.youtube_urls:
            topic.video_duration_seconds = await fetch_video_duration_seconds(topic.youtube_urls[0])
        return await self.topic_repository.save(topic)

    async def list_all(
        self,
        transcript_state: Optional[TranscriptState] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        skip: int = 0,
        owner_id: Optional[str] = None,
        search: Optional[str] = None,
        level=None,
    ) -> list[TopicContent]:
        return await self.topic_repository.list_all(
            transcript_state=transcript_state, limit=limit, skip=skip, owner_id=owner_id,
            search=search, level=level,
        )

    async def list_for_requester(
        self,
        requester: User,
        transcript_state: Optional[TranscriptState] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        skip: int = 0,
        search: Optional[str] = None,
        level=None,
    ) -> list[tuple[TopicContent, Optional[LessonDayState], Optional[int]]]:
        """Root and Administrator (Center Admin) see every topic in the center, Administrator included since
        PROMPT-047. A Teacher sees only the topics *they* authored, plus legacy ones with no author recorded yet
        (`created_by_id is None`) - read-only, see `get_own_topic`. Neither carries per-topic status/minutes
        (`None`) - content management isn't entitlement-gated. A Student sees exactly their own calendar's
        topics, in day order, each paired with its current lock/done/current/missed status and its allotted
        Mariam-session minutes; one with no plan (or no active template) assigned yet sees an empty list, not an
        error - there's nothing to show them, not a request problem.

        Pagination applies to the calendar's own topic count, not the whole
        catalogue - a Student's list is already bounded by their template.
        """
        if requester.role != Role.STUDENT:
            owner_id = None if requester.role in (Role.ROOT, Role.ADMINISTRATOR) else requester.id
            topics = await self.list_all(
                transcript_state=transcript_state, limit=limit, skip=skip, owner_id=owner_id,
                search=search, level=level,
            )
            return [(topic, None, None) for topic in topics]

        topic_ids, statuses = await self.progress_service.get_calendar_topics_and_statuses(requester)
        if not topic_ids:
            return []
        topics_by_id = {t.id: t for t in await self.topic_repository.get_many_by_ids(topic_ids)}
        triples = [
            (await self._student_delivery_topic(topics_by_id[tid], requester), statuses.get(tid, LessonDayState.LOCKED), await self.progress_service.get_topic_session_minutes(requester, tid))
            for tid in topic_ids
            if tid in topics_by_id
        ]
        if transcript_state is not None:
            triples = [(topic, status, minutes) for topic, status, minutes in triples if topic.transcript_state == transcript_state]
        return triples[skip : skip + limit]

    async def get_one(self, topic_id: str) -> TopicContent:
        topic = await self.topic_repository.get_by_id(topic_id)
        if topic is None:
            raise NotFoundException("Topic not found")
        return topic

    async def get_one_for_requester(
        self, topic_id: str, requester: User, enrollment_context: Optional["EnrollmentContext"] = None
    ) -> tuple[TopicContent, Optional[LessonDayState], Optional[int]]:
        """The server-side half of card #14's reproduced hole: a Student
        must not be able to open a lesson outside their own calendar, or one
        whose scheduled day hasn't arrived yet, just because they know its
        id. Root and Administrator (Center Admin) are unaffected - entitlement
        is a Student concept, not a content-management gate, and the Center
        Admin sees every topic. A Teacher may read any topic they authored or
        any legacy one (`created_by_id is None`), same visibility as
        `list_for_requester`; another Teacher's topic is hidden (teacher
        isolation).
        """
        topic = await self.get_one(topic_id)
        if requester.role == Role.TEACHER:
            if topic.created_by_id is not None and topic.created_by_id != requester.id:
                raise ForbiddenException("This topic belongs to a different teacher")
            return topic, None, None
        if requester.role != Role.STUDENT:
            return topic, None, None
        if enrollment_context is not None:
            statuses = await self.progress_service.get_topic_statuses_for_context(requester, enrollment_context)
        else:
            statuses = await self.progress_service.get_topic_statuses(requester)
        status = statuses.get(topic_id)
        if status is None:
            raise ForbiddenException("This topic is not part of your assigned plan")
        if status == LessonDayState.LOCKED:
            raise ForbiddenException("This lesson hasn't unlocked yet")
        if enrollment_context is not None:
            minutes = await self.progress_service.get_topic_session_minutes_for_context(requester, topic_id, enrollment_context)
        else:
            minutes = await self.progress_service.get_topic_session_minutes(requester, topic_id)
        return await self._student_delivery_topic(topic, requester, enrollment_context), status, minutes

    async def get_own_topic(self, topic_id: str, admin: User) -> TopicContent:
        """The Teacher write-path ownership gate (teacher isolation): a
        Teacher may only write a topic they authored themselves - a legacy
        topic (`created_by_id is None`) is visible to every Teacher but
        writable by none until Root explicitly re-owns it. Root and
        Administrator (Center Admin) are unrestricted, same as everywhere
        else in this codebase.
        """
        topic = await self.get_one(topic_id)
        if admin.role in (Role.ROOT, Role.ADMINISTRATOR):
            return topic
        if topic.created_by_id != admin.id:
            raise ForbiddenException("This topic belongs to a different teacher")
        return topic

    async def get_admin_detail(self, topic_id: str, admin: User) -> TopicAdminDetail:
        """Who authored this topic and which students currently have it in
        their plan - the admin Topics page's detail view (not shown in the
        list, see `TopicAdminDetail`'s own docstring). Reuses `get_own_topic`
        for the same teacher-isolation read gate as everywhere else: a
        Teacher may open this for their own or a legacy topic, not another
        Teacher's.
        """
        topic = await self.get_own_topic(topic_id, admin)
        creator = await self.user_repository.get_by_id(topic.created_by_id) if topic.created_by_id else None
        students = await self.user_repository.get_students_assigned_to_topic(topic_id)
        return TopicAdminDetail(
            created_by_id=topic.created_by_id,
            created_by_name=creator.full_name if creator else None,
            created_by_email=creator.email if creator else None,
            enrolled_students=[
                EnrolledStudent(id=s.id, full_name=s.full_name, email=s.email) for s in students
            ],
        )

    async def get_own_or_shared_topic_for_delete(self, topic_id: str, admin: User) -> TopicContent:
        """Same ownership gate as `get_own_topic`, but with one deliberate
        exception: any Teacher may delete a legacy/shared topic
        (`created_by_id is None`), not just Root/Administrator. Editing a
        shared topic stays Root/Administrator-only (`get_own_topic`) - a
        shared topic's *content* is still everyone's to rely on, but any
        teacher who doesn't want it cluttering their own catalogue can remove
        it. Deleting it removes it for every Teacher who could see it, not
        just the one who deleted it - soft-delete, same as every other Topic
        deletion, so the record itself is kept, not erased.
        """
        topic = await self.get_one(topic_id)
        if admin.role in (Role.ROOT, Role.ADMINISTRATOR):
            return topic
        if topic.created_by_id is not None and topic.created_by_id != admin.id:
            raise ForbiddenException("This topic belongs to a different teacher")
        return topic

    async def update(self, topic_id: str, body: TopicUpdate, admin: User) -> TopicContent:
        """A partial `$set` of just the provided fields, not a whole-document
        save. A save would write back every field of a snapshot taken before the
        edit, so a `PATCH` landing while the transcript pipeline is mid-run would
        revert whatever that run had written - and vice versa. Only the fields
        the caller actually named are touched.
        """
        topic = await self.get_own_topic(topic_id, admin)  # 404/403 before writing
        changes = body.model_dump(exclude_unset=True)
        if "level_profile_id" in changes:
            profile_id = changes["level_profile_id"]
            if profile_id:
                profile = await LevelProfile.find_one(
                    {"_id": profile_id, "is_deleted": False, "is_active": True}
                )
                if profile is None:
                    raise NotFoundException("Center level not found")
                changes.update(
                    level=profile.base_level,
                    level_name=profile.name,
                    level_guidance=profile.avatar_guidance,
                )
            else:
                changes.update(level_name="", level_guidance="")
        if "youtube_urls" in changes:
            changes["video_duration_seconds"] = (
                await fetch_video_duration_seconds(changes["youtube_urls"][0]) if changes["youtube_urls"] else None
            )
        if "unit_id" in changes:
            changes["unit_id"] = await self._resolve_unit_id(
                changes["unit_id"], changes.get("level", topic.level), admin
            )
        # An exercise list and an Interactive Video timeline are one revision
        # contract. Reject a dangling checkpoint before saving either side.
        if "lesson_exercises" in changes and topic.lesson_flow == LessonFlow.INTERACTIVE_VIDEO:
            # ``model_dump`` intentionally produces JSON-ready dictionaries.
            # Rehydrate them before timeline validation: the validator needs
            # the typed ``LessonExercise`` interface, not raw dicts.  This
            # used to turn an ordinary Studio edit into a 500 after an AI
            # draft had been accepted.
            exercises_for_validation = [LessonExerciseIn.model_validate(item) for item in changes["lesson_exercises"]]
            validate_interactive_video_timeline(topic.interactive_video_checkpoints, exercises_for_validation)
        if changes:
            changes.update(await self._start_content_revision(topic))
            await self.topic_repository.update_fields(topic_id, changes)
        return await self.get_one(topic_id)

    async def update_interactive_video_timeline(
        self, topic_id: str, checkpoints: list[InteractiveVideoCheckpoint], admin: User
    ) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if topic.lesson_flow != LessonFlow.INTERACTIVE_VIDEO:
            raise UnprocessableException("Choose Interactive Video before editing its checkpoint timeline")
        # The client submits an ordered list. Do not silently sort it: a
        # mistake should be corrected by its author rather than changing the
        # intended activity sequence behind their back.
        validate_interactive_video_timeline(checkpoints, topic.lesson_exercises)
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(
            topic_id,
            {
                "interactive_video_checkpoints": [item.model_dump() for item in checkpoints],
                # A manual edit is an explicit teacher decision; stale AI
                # proposals must never look like active checkpoints.
                "interactive_video_drafts": [],
                "interactive_video_draft_generation_key": None,
                **revision,
            },
        )
        return await self.get_one(topic_id)

    async def accept_interactive_video_draft(self, topic_id: str, index: int, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if topic.lesson_flow != LessonFlow.INTERACTIVE_VIDEO:
            raise UnprocessableException("Choose Interactive Video before accepting timeline suggestions")
        if index < 0 or index >= len(topic.interactive_video_drafts):
            raise NotFoundException("No such timeline suggestion")
        selected = topic.interactive_video_drafts[index]
        exercises = list(topic.lesson_exercises)
        if selected.checkpoint_kind == CheckpointKind.PRACTICE_WITH_MARIAM:
            new_checkpoint = InteractiveVideoCheckpoint(
                timestamp_seconds=selected.timestamp_seconds,
                kind=CheckpointKind.PRACTICE_WITH_MARIAM,
                exercise_index=None,
                learning_objective=selected.learning_objective,
                conversation_goal=selected.conversation_goal,
                opening_line=selected.opening_line,
                session_duration_minutes=selected.session_duration_minutes or 2,
            )
        else:
            if selected.exercise is not None:
                # A new AI activity is still only a teacher draft until this
                # explicit acceptance.  Append it and point this one checkpoint
                # at the stable new index; the teacher can edit it in Studio
                # before publishing.
                exercises.append(selected.exercise)
                exercise_index = len(exercises) - 1
            elif selected.exercise_index is not None:
                exercise_index = selected.exercise_index
            else:
                raise UnprocessableException("This timeline suggestion has no usable activity")
            new_checkpoint = InteractiveVideoCheckpoint(
                timestamp_seconds=selected.timestamp_seconds,
                exercise_index=exercise_index,
                completion_rule=selected.completion_rule,
            )
        checkpoints = [*topic.interactive_video_checkpoints, new_checkpoint]
        checkpoints.sort(key=lambda item: item.timestamp_seconds)
        validate_interactive_video_timeline(checkpoints, exercises)
        drafts = [item.model_dump() for item_index, item in enumerate(topic.interactive_video_drafts) if item_index != index]
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "lesson_exercises": [item.model_dump() for item in exercises],
            "interactive_video_checkpoints": [item.model_dump() for item in checkpoints],
            "interactive_video_drafts": drafts,
            **revision,
        })
        return await self.get_one(topic_id)

    async def convert_checkpoint_to_practice_with_mariam(
        self,
        topic_id: str,
        checkpoint_id: str,
        *,
        learning_objective: Optional[str],
        conversation_goal: str,
        opening_line: str,
        session_duration_minutes: int,
        admin: User,
    ) -> TopicContent:
        """Teacher conversion of an existing (exercise-pointing or otherwise
        suggested) checkpoint into a Practice with Mariam checkpoint, in
        place - same timestamp, new kind. `exercise_index` is cleared; the
        referenced lesson exercise itself (if any) is left untouched, just no
        longer linked to this checkpoint."""
        topic = await self.get_own_topic(topic_id, admin)
        if topic.lesson_flow != LessonFlow.INTERACTIVE_VIDEO:
            raise UnprocessableException("Choose Interactive Video before converting a checkpoint")
        index = next((i for i, item in enumerate(topic.interactive_video_checkpoints) if item.id == checkpoint_id), None)
        if index is None:
            raise NotFoundException("No such video checkpoint")
        existing = topic.interactive_video_checkpoints[index]
        converted = InteractiveVideoCheckpoint(
            id=existing.id,
            timestamp_seconds=existing.timestamp_seconds,
            kind=CheckpointKind.PRACTICE_WITH_MARIAM,
            exercise_index=None,
            learning_objective=learning_objective,
            conversation_goal=conversation_goal,
            opening_line=opening_line,
            session_duration_minutes=session_duration_minutes,
        )
        checkpoints = list(topic.interactive_video_checkpoints)
        checkpoints[index] = converted
        validate_interactive_video_timeline(checkpoints, topic.lesson_exercises)
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "interactive_video_checkpoints": [item.model_dump() for item in checkpoints],
            "interactive_video_drafts": [],
            "interactive_video_draft_generation_key": None,
            **revision,
        })
        return await self.get_one(topic_id)

    async def reject_interactive_video_draft(self, topic_id: str, index: int, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if index < 0 or index >= len(topic.interactive_video_drafts):
            raise NotFoundException("No such timeline suggestion")
        drafts = [item.model_dump() for item_index, item in enumerate(topic.interactive_video_drafts) if item_index != index]
        await self.topic_repository.update_fields(topic_id, {"interactive_video_drafts": drafts})
        return await self.get_one(topic_id)

    async def update_learning_objectives(
        self, topic_id: str, body: LearningObjectivesUpdate, admin: User
    ) -> TopicContent:
        """Save only the reviewed objective list.

        Any material change deliberately clears the approval flag, but does
        not touch exercises, source, transcript or an already published
        revision. This keeps an old published delivery stable while the
        teacher reviews the revised curriculum intent.
        """
        topic = await self.get_own_topic(topic_id, admin)
        objectives = [LearningObjective(**objective.model_dump()) for objective in body.objectives]
        previous = [objective.model_dump() for objective in topic.learning_objectives]
        next_values = [objective.model_dump() for objective in objectives]
        changes: dict = {"learning_objectives": next_values}
        if previous != next_values:
            changes["objectives_approved"] = False
        await self.topic_repository.update_fields(topic_id, changes)
        return await self.get_one(topic_id)

    async def approve_learning_objectives(self, topic_id: str, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if not topic.learning_objectives:
            raise UnprocessableException("Add at least one learning objective before approving")
        await self.topic_repository.update_fields(topic_id, {"objectives_approved": True})
        return await self.get_one(topic_id)

    async def accept_objective_draft(self, topic_id: str, index: int, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if index < 0 or index >= len(topic.objective_drafts):
            raise NotFoundException("No such objective suggestion")
        accepted = topic.objective_drafts[index]
        remaining = [item.model_dump() for item_index, item in enumerate(topic.objective_drafts) if item_index != index]
        objectives = [item.model_dump() for item in topic.learning_objectives] + [accepted.model_dump()]
        await self.topic_repository.update_fields(topic_id, {
            "learning_objectives": objectives,
            "objective_drafts": remaining,
            "objectives_approved": False,
        })
        return await self.get_one(topic_id)

    async def reject_objective_draft(self, topic_id: str, index: int, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if index < 0 or index >= len(topic.objective_drafts):
            raise NotFoundException("No such objective suggestion")
        remaining = [item.model_dump() for item_index, item in enumerate(topic.objective_drafts) if item_index != index]
        await self.topic_repository.update_fields(topic_id, {"objective_drafts": remaining})
        return await self.get_one(topic_id)

    async def delete(self, topic_id: str, admin: User) -> None:
        topic = await self.get_own_or_shared_topic_for_delete(topic_id, admin)
        await self.topic_repository.soft_delete(topic)

    async def upload_thumbnail(self, topic_id: str, file_bytes: bytes, content_type: str, admin: User) -> TopicContent:
        """Admin-authored cover image for a lesson - replaces whatever custom
        thumbnail was there before (the old asset is deleted only after the new
        one is safely uploaded and saved, never before, so a failed upload
        never leaves the topic with no thumbnail at all).
        """
        if not content_type.startswith("image/"):
            raise UnprocessableException(f"Expected an image file, got '{content_type}'")
        if len(file_bytes) > MAX_THUMBNAIL_UPLOAD_BYTES:
            raise UnprocessableException("Image is larger than a lesson thumbnail should ever be")

        topic = await self.get_own_topic(topic_id, admin)
        public_id = f"{CLOUDINARY_THUMBNAIL_FOLDER}/{topic_id}-{uuid.uuid4().hex}"
        try:
            result = await self.image_storage.upload(file_bytes, public_id)
        except UploadFailedError as exc:
            raise UnprocessableException(f"Could not upload image: {exc}") from exc

        previous_public_id = topic.custom_thumbnail_public_id
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(
            topic_id,
            {"custom_thumbnail_url": result.secure_url, "custom_thumbnail_public_id": result.public_id, **revision},
        )
        if previous_public_id:
            await self._delete_thumbnail_asset(previous_public_id)
        return await self.get_one(topic_id)

    async def upload_pdf(self, topic_id: str, file_bytes: bytes, content_type: str, filename: str, admin: User) -> TopicContent:
        """Video-optional lesson source (card #16): extracts the PDF's text
        at upload time (not at generation time - a bad/scanned PDF should
        fail here with a clear 422, not silently inside a background job
        much later) and stores both the asset (for reference/download) and
        the extracted text (what `TranscriptPipelineService` actually
        grounds generation in). Same replace-then-delete-old safety as
        `upload_thumbnail`.
        """
        if content_type != "application/pdf":
            raise UnprocessableException(f"Expected a PDF file, got '{content_type}'")
        if len(file_bytes) > MAX_PDF_UPLOAD_BYTES:
            raise UnprocessableException("PDF is larger than a lesson document should ever be")

        try:
            extracted_text = extract_pdf_text(file_bytes)
        except PdfExtractionError as exc:
            raise UnprocessableException(str(exc)) from exc

        topic = await self.get_own_topic(topic_id, admin)
        public_id = f"{CLOUDINARY_PDF_FOLDER}/{topic_id}-{uuid.uuid4().hex}"
        try:
            result = await self.document_storage.upload(file_bytes, public_id)
        except UploadFailedError as exc:
            raise UnprocessableException(f"Could not upload PDF: {exc}") from exc

        previous_public_id = topic.pdf_public_id
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(
            topic_id,
            {
                "pdf_url": result.secure_url,
                "pdf_public_id": result.public_id,
                "pdf_filename": filename,
                "pdf_extracted_text": extracted_text,
                "transcript_state": TranscriptState.QUEUED,
                "transcript_state_started_at": None,
                "transcript_error": None,
                "raw_transcript": [],
                "transcript_source_labels": [],
                "transcript_source": None,
                "approved_transcript": None,
                **revision,
            },
        )
        if previous_public_id:
            await self._delete_pdf_asset(previous_public_id)
        return await self.get_one(topic_id)

    @staticmethod
    def _safe_source_filename(filename: str) -> str:
        return (filename or "lesson-source").replace("\\", "/").rsplit("/", 1)[-1][:200] or "lesson-source"

    @staticmethod
    def _reset_transcript_fields() -> dict:
        return {
            "transcript_state": TranscriptState.QUEUED,
            "transcript_state_started_at": None,
            "transcript_error": None,
            "raw_transcript": [],
            "transcript_source_labels": [],
            "transcript_source": None,
            "approved_transcript": None,
        }

    async def upload_document(self, topic_id: str, file_bytes: bytes, content_type: str, filename: str, admin: User) -> TopicContent:
        """Persist one additional document source.  Extraction happens before
        storage, so unreadable Office files never become orphaned assets."""
        if not file_bytes or len(file_bytes) > MAX_SOURCE_DOCUMENT_UPLOAD_BYTES:
            raise UnprocessableException("Document is empty or larger than 20 MB")
        topic = await self.get_own_topic(topic_id, admin)
        if len(topic.source_documents) >= MAX_SOURCE_DOCUMENTS:
            raise UnprocessableException(f"A lesson can have up to {MAX_SOURCE_DOCUMENTS} additional sources")
        try:
            kind, extracted_text = extract_document_text(file_bytes, content_type, filename)
        except DocumentExtractionError as exc:
            raise UnprocessableException(str(exc)) from exc

        # A transcript review must remain bounded even when several valid files
        # are combined.  The pipeline consumes every stored source verbatim.
        if sum(len(item.extracted_text) for item in topic.all_source_documents) + len(extracted_text) > 1_000_000:
            raise UnprocessableException("Combined source text is too large for one lesson")
        safe_filename = self._safe_source_filename(filename)
        public_id = f"{CLOUDINARY_SOURCE_DOCUMENT_FOLDER}/{topic_id}-{uuid.uuid4().hex}"
        try:
            result = await self.document_storage.upload(file_bytes, public_id)
        except UploadFailedError as exc:
            logger.warning("Document upload failed for topic {}: {}", topic_id, exc)
            raise UnprocessableException("Could not upload this document. Please try again.") from exc
        document = SourceDocument(
            kind=SourceDocumentKind(kind), label=f"{kind.upper()} — {safe_filename}", filename=safe_filename,
            url=result.secure_url, public_id=result.public_id, extracted_text=extracted_text, added_at=utcnow(),
        )
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "source_documents": [*topic.source_documents, document],
            **self._reset_transcript_fields(), **revision,
        })
        return await self.get_one(topic_id)

    async def add_text_source(self, topic_id: str, label: str, text: str, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        cleaned = text.strip()
        if not cleaned:
            raise UnprocessableException("Paste some source text before adding it")
        if len(topic.source_documents) >= MAX_SOURCE_DOCUMENTS:
            raise UnprocessableException(f"A lesson can have up to {MAX_SOURCE_DOCUMENTS} additional sources")
        if sum(len(item.extracted_text) for item in topic.all_source_documents) + len(cleaned) > 1_000_000:
            raise UnprocessableException("Combined source text is too large for one lesson")
        document = SourceDocument(
            kind=SourceDocumentKind.TEXT, label=(label.strip() or "Pasted text")[:200],
            extracted_text=cleaned, added_at=utcnow(),
        )
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "source_documents": [*topic.source_documents, document],
            **self._reset_transcript_fields(), **revision,
        })
        return await self.get_one(topic_id)

    async def remove_source_document(self, topic_id: str, index: int, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        if index < 0 or index >= len(topic.source_documents):
            raise NotFoundException("Source document not found")
        removed = topic.source_documents[index]
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "source_documents": [*topic.source_documents[:index], *topic.source_documents[index + 1:]],
            **self._reset_transcript_fields(), **revision,
        })
        if removed.public_id:
            await self._delete_pdf_asset(removed.public_id)
        return await self.get_one(topic_id)

    async def upload_source_video(self, topic_id: str, file_bytes: bytes, content_type: str, filename: str, admin: User) -> TopicContent:
        allowed = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
        if content_type not in allowed:
            raise UnprocessableException("Use an MP4, WebM, MOV or MKV lesson video")
        if not file_bytes or len(file_bytes) > MAX_SOURCE_VIDEO_UPLOAD_BYTES:
            raise UnprocessableException("Video is empty or larger than 80 MB")
        topic = await self.get_own_topic(topic_id, admin)
        public_id = f"{CLOUDINARY_SOURCE_VIDEO_FOLDER}/{topic_id}-{uuid.uuid4().hex}"
        try:
            result = await self.video_storage.upload(file_bytes, public_id)
        except UploadFailedError as exc:
            raise UnprocessableException(f"Could not upload video: {exc}") from exc
        previous = topic.uploaded_video_public_id
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "uploaded_video_url": result.secure_url,
            "uploaded_video_public_id": result.public_id,
            "uploaded_video_filename": filename,
            "transcript_state": TranscriptState.QUEUED,
            "transcript_state_started_at": None,
            "transcript_error": None,
            "raw_transcript": [],
            "transcript_source_labels": [],
            "transcript_source": None,
            "approved_transcript": None,
            **revision,
        })
        if previous:
            try:
                await self.video_storage.delete(previous)
            except UploadFailedError:
                logger.warning("Could not delete replaced lesson video {}", previous)
        return await self.get_one(topic_id)

    async def create_source_video_upload(
        self, topic_id: str, content_type: str, size_bytes: int, admin: User
    ) -> dict[str, str | int]:
        """Authorize exactly one direct browser upload for a lesson video."""
        allowed = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
        if content_type not in allowed:
            raise UnprocessableException("Use an MP4, WebM, MOV or MKV lesson video")
        if size_bytes <= 0 or size_bytes > MAX_SOURCE_VIDEO_UPLOAD_BYTES:
            raise UnprocessableException("Video is empty or larger than 80 MB")
        await self.get_own_topic(topic_id, admin)
        public_id = f"{CLOUDINARY_SOURCE_VIDEO_FOLDER}/{topic_id}-{uuid.uuid4().hex}"
        return self.video_storage.issue_direct_video_upload(public_id)

    async def finalize_source_video_upload(
        self, topic_id: str, public_id: str, filename: str, admin: User
    ) -> TopicContent:
        """Attach a provider-uploaded video after proving its server prefix.

        The URL is derived by the server instead of trusting a browser supplied
        URL.  A signed upload can only target the random ``public_id`` issued
        immediately above, and this second check prevents one topic from
        pointing at another topic's video.
        """
        topic = await self.get_own_topic(topic_id, admin)
        prefix = f"{CLOUDINARY_SOURCE_VIDEO_FOLDER}/{topic_id}-"
        if not public_id.startswith(prefix):
            raise UnprocessableException("That upload does not belong to this lesson")
        previous = topic.uploaded_video_public_id
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "uploaded_video_url": self.video_storage.direct_video_url(public_id),
            "uploaded_video_public_id": public_id,
            "uploaded_video_filename": filename,
            "transcript_state": TranscriptState.QUEUED,
            "transcript_state_started_at": None,
            "transcript_error": None,
            "raw_transcript": [],
            "transcript_source_labels": [],
            "transcript_source": None,
            "approved_transcript": None,
            **revision,
        })
        if previous and previous != public_id:
            try:
                await self.video_storage.delete(previous)
            except UploadFailedError:
                logger.warning("Could not delete replaced lesson video {}", previous)
        return await self.get_one(topic_id)

    async def remove_source_video(self, topic_id: str, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(topic_id, {
            "uploaded_video_url": None,
            "uploaded_video_public_id": None,
            "uploaded_video_filename": None,
            "transcript_state": TranscriptState.QUEUED,
            "transcript_state_started_at": None,
            "transcript_error": None,
            "raw_transcript": [],
            "transcript_source_labels": [],
            "transcript_source": None,
            "approved_transcript": None,
            **revision,
        })
        if topic.uploaded_video_public_id:
            try:
                await self.video_storage.delete(topic.uploaded_video_public_id)
            except UploadFailedError:
                logger.warning("Could not delete lesson video {}", topic.uploaded_video_public_id)
        return await self.get_one(topic_id)

    async def remove_pdf(self, topic_id: str, admin: User) -> TopicContent:
        topic = await self.get_own_topic(topic_id, admin)
        revision = await self._start_content_revision(topic)
        if topic.pdf_public_id:
            await self._delete_pdf_asset(topic.pdf_public_id)
        await self.topic_repository.update_fields(
            topic_id,
            {
                "pdf_url": None,
                "pdf_public_id": None,
                "pdf_filename": None,
                "pdf_extracted_text": None,
                "transcript_state": TranscriptState.QUEUED,
                "transcript_state_started_at": None,
                "transcript_error": None,
                "raw_transcript": [],
                "transcript_source_labels": [],
                "transcript_source": None,
                "approved_transcript": None,
                **revision,
            },
        )
        return await self.get_one(topic_id)

    async def _delete_pdf_asset(self, public_id: str) -> None:
        try:
            await self.document_storage.delete(public_id)
        except UploadFailedError as exc:
            logger.error("Orphaned PDF asset {} - delete failed: {}", public_id, exc)

    async def remove_thumbnail(self, topic_id: str, admin: User) -> TopicContent:
        """Reverts to the free YouTube-derived thumbnail (or no thumbnail, if
        the topic has no `youtube_urls`) by clearing the custom one.
        """
        topic = await self.get_own_topic(topic_id, admin)
        if topic.custom_thumbnail_public_id:
            await self._delete_thumbnail_asset(topic.custom_thumbnail_public_id)
        revision = await self._start_content_revision(topic)
        await self.topic_repository.update_fields(
            topic_id, {"custom_thumbnail_url": None, "custom_thumbnail_public_id": None, **revision}
        )
        return await self.get_one(topic_id)

    async def _delete_thumbnail_asset(self, public_id: str) -> None:
        """Best-effort - same reasoning as `RecordingSubmissionService
        ._delete_orphan`: the database write (the thing a student/admin
        actually sees) has already succeeded, and a Cloudinary blip here must
        not turn that success into an error response.
        """
        try:
            await self.image_storage.delete(public_id)
        except UploadFailedError as exc:
            logger.error("Orphaned thumbnail asset {} - delete failed: {}", public_id, exc)


def get_topic_content_service() -> TopicContentService:
    return TopicContentService(TopicContentRepository(), get_progress_service())
