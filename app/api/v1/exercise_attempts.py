"""Student lesson answers and teacher review queue."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.security import require_role, require_teacher_or_administrator
from app.models.user import Role, User
from app.schema.exercise_attempt import ExerciseAttemptCreate, ExerciseAttemptPublic, ExerciseReviewRequest
from app.services.exercise_attempt_service import ExerciseAttemptService, get_exercise_attempt_service

router = APIRouter()


@router.post("/exercise-attempts", response_model=ExerciseAttemptPublic, status_code=201)
async def submit_exercise_answer(
    body: ExerciseAttemptCreate,
    student: User = Depends(require_role(Role.STUDENT)),
    service: ExerciseAttemptService = Depends(get_exercise_attempt_service),
) -> ExerciseAttemptPublic:
    attempt = await service.submit(student, body)
    return ExerciseAttemptPublic.from_attempt(attempt)


@router.get("/exercise-attempts/me", response_model=list[ExerciseAttemptPublic])
async def my_lesson_answers(
    topic_id: str,
    enrollment_id: Optional[str] = Query(None, description="Required only if you have more than one active enrollment."),
    student: User = Depends(require_role(Role.STUDENT)),
    service: ExerciseAttemptService = Depends(get_exercise_attempt_service),
) -> list[ExerciseAttemptPublic]:
    return [ExerciseAttemptPublic.from_attempt(item) for item in await service.mine(student, topic_id, enrollment_id)]


@router.get("/exercise-attempts/review", response_model=list[ExerciseAttemptPublic])
async def list_exercise_answers_for_review(
    pending_only: bool = Query(True),
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0),
    teacher: User = Depends(require_teacher_or_administrator),
    service: ExerciseAttemptService = Depends(get_exercise_attempt_service),
) -> list[ExerciseAttemptPublic]:
    return [ExerciseAttemptPublic.from_attempt(item) for item in await service.review_queue(teacher, pending_only, limit, skip)]


@router.patch("/exercise-attempts/{attempt_id}/review", response_model=ExerciseAttemptPublic)
async def review_exercise_answer(
    attempt_id: str,
    body: ExerciseReviewRequest,
    teacher: User = Depends(require_teacher_or_administrator),
    service: ExerciseAttemptService = Depends(get_exercise_attempt_service),
) -> ExerciseAttemptPublic:
    return ExerciseAttemptPublic.from_attempt(await service.review(teacher, attempt_id, body))
