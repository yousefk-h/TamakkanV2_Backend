"""Exercise answers, trusted scoring, and teacher review with topic ownership."""

import hashlib
import json
from datetime import timezone

from beanie.odm.operators.update.general import Set
from app.core.exceptions import ConflictException, ForbiddenException, NotFoundException, UnprocessableException
from app.models.exercise_attempt import ExerciseAttempt
from app.models.topic_content import ExerciseKind, LessonExercise, TopicContent
from app.models.user import Role, User
from app.schema.exercise_attempt import ExerciseAttemptCreate, ExerciseReviewRequest
from app.services.topic_content_service import TopicContentService, get_topic_content_service

ANSWER_KINDS = frozenset({ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.IMAGE_COMPLETION, ExerciseKind.LISTENING,
                          ExerciseKind.GRAMMAR, ExerciseKind.READING, ExerciseKind.GAME, ExerciseKind.WRITING,
                          ExerciseKind.REFLECTION})
CHOICE_KINDS = ANSWER_KINDS - {ExerciseKind.GAME, ExerciseKind.WRITING, ExerciseKind.REFLECTION}


def exercise_hash(exercise: LessonExercise) -> str:
    payload = json.dumps(exercise.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExerciseAttemptService:
    def __init__(self, topics: TopicContentService) -> None:
        self.topics = topics

    async def submit(self, student: User, body: ExerciseAttemptCreate) -> ExerciseAttempt:
        topic, _, _ = await self.topics.get_one_for_requester(body.topic_id, student)
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
        elif not isinstance(answer, str) or not answer.strip():
            raise UnprocessableException("Write an answer before submitting")
        elif exercise.kind == ExerciseKind.WRITING and len(answer.strip().split()) < exercise.min_words:
            raise UnprocessableException(f"Write at least {exercise.min_words} words")
        if isinstance(answer, str):
            answer_display = answer.strip()
        attempt = ExerciseAttempt(student_id=student.id, student_name=student.full_name,
                                  topic_id=topic.id, topic_title=topic.title,
                                  exercise_index=body.exercise_index, exercise_hash=exercise_hash(exercise),
                                  exercise_kind=exercise.kind, exercise_prompt=exercise.prompt,
                                  answer=answer.strip() if isinstance(answer, str) else answer,
                                  answer_display=answer_display,
                                  correct=correct, correct_option=correct_option, score=score, reviewed=reviewed)
        await attempt.insert()
        await ExerciseAttempt.find({"student_id": student.id, "topic_id": topic.id,
                                    "exercise_index": body.exercise_index, "exercise_hash": attempt.exercise_hash,
                                    "_id": {"$ne": attempt.id}, "superseded": False}).update(Set({"superseded": True}))
        return attempt

    async def mine(self, student: User, topic_id: str) -> list[ExerciseAttempt]:
        topic, _, _ = await self.topics.get_one_for_requester(topic_id, student)
        attempts = await ExerciseAttempt.find({"student_id": student.id, "topic_id": topic_id, "is_deleted": False, "superseded": False}).sort("-created_at").to_list()
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
    return ExerciseAttemptService(get_topic_content_service())
