"""Exercise answers, trusted scoring, and teacher review with topic ownership."""

import hashlib
import json
from datetime import timezone
from typing import Optional

from beanie.odm.operators.update.general import Set
from app.core.exceptions import ConflictException, ForbiddenException, NotFoundException, UnprocessableException
from app.models.exercise_attempt import ExerciseAttempt
from app.models.topic_content import ExerciseKind, LessonExercise, TopicContent
from app.models.user import Role, User
from app.schema.exercise_attempt import ExerciseAttemptCreate, ExerciseReviewRequest
from app.services.enrollment_service import EnrollmentService, get_enrollment_service
from app.services.topic_content_service import TopicContentService, get_topic_content_service
from app.utils.time import utcnow

ANSWER_KINDS = frozenset({ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.IMAGE_COMPLETION, ExerciseKind.LISTENING,
                          ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.GAME, ExerciseKind.WRITING,
                          ExerciseKind.REFLECTION, ExerciseKind.WORD_ORDER})
CHOICE_KINDS = ANSWER_KINDS - {ExerciseKind.GAME, ExerciseKind.WRITING, ExerciseKind.REFLECTION, ExerciseKind.WORD_ORDER}
# Deterministically graded by index-list, same answer shape as GAME
# (list[int]) but a different correctness rule: the student must reconstruct
# the one correct order, not match unordered pairs.
ORDER_KINDS = frozenset({ExerciseKind.WORD_ORDER})


def exercise_hash(exercise: LessonExercise) -> str:
    payload = json.dumps(exercise.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExerciseAttemptService:
    def __init__(self, topics: TopicContentService, enrollments: EnrollmentService) -> None:
        self.topics = topics
        self.enrollments = enrollments

    async def submit(self, student: User, body: ExerciseAttemptCreate) -> ExerciseAttempt:
        context = await self.enrollments.resolve_context(student, body.enrollment_id)
        topic, _, _ = await self.topics.get_one_for_requester(body.topic_id, student, context)
        if body.exercise_index >= len(topic.lesson_exercises):
            raise NotFoundException("Exercise not found in this lesson")
        expected_time = body.topic_updated_at
        if expected_time.tzinfo is not None:
            expected_time = expected_time.astimezone(timezone.utc).replace(tzinfo=None)
        if topic.updated_at != expected_time:
            raise ConflictException("This lesson changed. Refresh it before submitting your answer")
        exercise = topic.lesson_exercises[body.exercise_index]
        if exercise.kind not in ANSWER_KINDS:
            raise UnprocessableException("This skill does not accept a typed answer")
        deadline = exercise.deadline_at
        if deadline is not None:
            if deadline.tzinfo is not None:
                deadline = deadline.astimezone(timezone.utc).replace(tzinfo=None)
            if utcnow() > deadline:
                raise ForbiddenException("The deadline for this activity has passed")
        # A no-resubmission lesson is enforced here, before accepting a new
        # answer. The UI may hide its button, but that is never the policy
        # boundary. The context query mirrors the supersession rule below.
        if not exercise.allow_resubmit:
            prior_base = {
                "student_id": student.id, "topic_id": topic.id,
                "exercise_index": body.exercise_index, "exercise_hash": exercise_hash(exercise),
                "is_deleted": False, "superseded": False,
            }
            if context.enrollment and context.legacy_attributable:
                prior_query: dict = {"$or": [
                    {**prior_base, "enrollment_id": context.enrollment.id},
                    {**prior_base, "program_id": context.program_id, "enrollment_id": None},
                ]}
            elif context.enrollment:
                prior_query = {**prior_base, "enrollment_id": context.enrollment.id}
            else:
                prior_query = {**prior_base, "program_id": context.program_id, "enrollment_id": None}
            if await ExerciseAttempt.find_one(prior_query):
                raise ConflictException("This activity does not allow another submission")
        answer = body.answer
        correct = None
        correct_option = None
        score = None
        reviewed = False
        answer_display = ""
        if exercise.kind in CHOICE_KINDS:
            if type(answer) is not int or not 0 <= answer < len(exercise.options):
                raise UnprocessableException("Select one valid answer option")
            correct = answer == exercise.correct_option
            correct_option = exercise.correct_option
            answer_display = exercise.options[answer]
            score = 100 if correct else 0
            reviewed = True
        elif exercise.kind == ExerciseKind.GAME:
            if (not isinstance(answer, list) or len(answer) != len(exercise.pairs)
                    or any(type(value) is not int or not 0 <= value < len(exercise.pairs) for value in answer)
                    or len(set(answer)) != len(answer)):
                raise UnprocessableException("Match each pair exactly once")
            correct = all(value == index for index, value in enumerate(answer))
            answer_display = "; ".join(f"{pair.left} → {exercise.pairs[answer[index]].right}" for index, pair in enumerate(exercise.pairs))
            score = 100 if correct else 0
            reviewed = True
        elif exercise.kind in ORDER_KINDS:
            word_count = len(exercise.ordering_words)
            if (not isinstance(answer, list) or len(answer) != word_count
                    or any(type(value) is not int or not 0 <= value < word_count for value in answer)
                    or len(set(answer)) != len(answer)):
                raise UnprocessableException("Arrange every word exactly once")
            correct = answer == list(range(word_count))
            answer_display = " ".join(exercise.ordering_words[i] for i in answer)
            score = 100 if correct else 0
            reviewed = True
        elif not isinstance(answer, str) or not answer.strip():
            raise UnprocessableException("Write an answer before submitting")
        elif exercise.kind == ExerciseKind.WRITING and len(answer.strip().split()) < exercise.min_words:
            raise UnprocessableException(f"Write at least {exercise.min_words} words")
        if isinstance(answer, str):
            answer_display = answer.strip()
        attempt = ExerciseAttempt(student_id=student.id, student_name=student.full_name,
                                  program_id=context.program_id, program_level_id=context.program_level_id,
                                  enrollment_id=context.enrollment.id if context.enrollment else None,
                                  topic_id=topic.id, topic_title=topic.title,
                                  exercise_index=body.exercise_index, exercise_hash=exercise_hash(exercise),
                                  exercise_kind=exercise.kind, exercise_prompt=exercise.prompt,
                                  answer=answer.strip() if isinstance(answer, str) else answer,
                                  answer_display=answer_display,
                                  correct=correct, correct_option=correct_option, score=score, reviewed=reviewed)
        await attempt.insert()
        # Supersede this student's previous attempt at the same exercise
        # WITHIN THE SAME ENROLLMENT CONTEXT only - never a different
        # enrollment's, even on the same Program/stage (e.g. two Cohorts).
        # A pre-existing legacy row (no enrollment_id) is also superseded
        # when it's unambiguously this context's - see
        # `EnrollmentContext.legacy_attributable` - so a migrated account
        # doesn't end up with two "current" attempts for one exercise.
        base_match = {
            "student_id": student.id, "topic_id": topic.id,
            "exercise_index": body.exercise_index, "exercise_hash": attempt.exercise_hash,
            "_id": {"$ne": attempt.id}, "superseded": False,
        }
        if attempt.enrollment_id and context.legacy_attributable:
            supersede_query = {"$or": [
                {**base_match, "enrollment_id": attempt.enrollment_id},
                {**base_match, "program_id": context.program_id, "enrollment_id": None},
            ]}
        elif attempt.enrollment_id:
            supersede_query = {**base_match, "enrollment_id": attempt.enrollment_id}
        else:
            supersede_query = {**base_match, "program_id": context.program_id, "enrollment_id": None}
        await ExerciseAttempt.find(supersede_query).update(Set({"superseded": True}))
        return attempt

    async def mine(self, student: User, topic_id: str, enrollment_id: Optional[str] = None) -> list[ExerciseAttempt]:
        context = await self.enrollments.resolve_context(student, enrollment_id)
        topic, _, _ = await self.topics.get_one_for_requester(topic_id, student, context)
        base = {"student_id": student.id, "topic_id": topic_id, "is_deleted": False, "superseded": False}
        if context.enrollment is not None and context.legacy_attributable:
            query: dict = {"$or": [
                {**base, "enrollment_id": context.enrollment.id},
                {**base, "program_id": context.program_id, "enrollment_id": None},
            ]}
        elif context.enrollment is not None:
            query = {**base, "enrollment_id": context.enrollment.id}
        else:
            query = {**base, "program_id": context.program_id, "enrollment_id": None}
        attempts = await ExerciseAttempt.find(query).sort("-created_at").to_list()
        hashes = {index: exercise_hash(exercise) for index, exercise in enumerate(topic.lesson_exercises)}
        current: list[ExerciseAttempt] = []
        seen: set[int] = set()
        for attempt in attempts:
            if attempt.exercise_index not in seen and hashes.get(attempt.exercise_index) == attempt.exercise_hash:
                current.append(attempt)
                seen.add(attempt.exercise_index)
        return current

    async def review_queue(self, teacher: User, pending_only: bool, limit: int, skip: int) -> list[ExerciseAttempt]:
        query: dict = {"is_deleted": False, "superseded": False}
        if teacher.role != Role.ADMINISTRATOR:
            owned_ids = await TopicContent.get_pymongo_collection().distinct("_id", {"created_by_id": teacher.id, "is_deleted": False})
            if not owned_ids:
                return []
            query["topic_id"] = {"$in": owned_ids}
        if pending_only:
            query["reviewed"] = False
        return await ExerciseAttempt.find(query).sort("-created_at").skip(skip).limit(limit).to_list()

    async def review(self, teacher: User, attempt_id: str, body: ExerciseReviewRequest) -> ExerciseAttempt:
        attempt = await ExerciseAttempt.find_one({"_id": attempt_id, "is_deleted": False, "superseded": False})
        if attempt is None:
            raise NotFoundException("Exercise answer not found")
        await self.topics.get_own_topic(attempt.topic_id, teacher)
        attempt.score = body.score
        attempt.feedback = body.feedback.strip()
        attempt.reviewed = True
        attempt.reviewed_by_id = teacher.id
        await attempt.save()
        return attempt


def get_exercise_attempt_service() -> ExerciseAttemptService:
    return ExerciseAttemptService(get_topic_content_service(), get_enrollment_service())
