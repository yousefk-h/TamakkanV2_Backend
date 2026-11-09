"""The Teacher/Admin "Student Learning Journey" view (complete student
activity, not just AI practice) - assembled entirely from existing
persisted records (Enrollment, the effective Calendar schedule,
ExerciseAttempt, LiveSession, Homework). No field here is ever computed by
calling an LLM; every number is a deterministic read/aggregation over
already-stored data. Where something genuinely isn't tracked (homework
submission/completion - confirmed absent from the data model, not merely
unqueried here), that is stated explicitly rather than invented.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.topic_content import ExerciseKind
from app.schema.challenge import LessonDayState
from app.schema.enrollment import EnrollmentPublic


class LessonExerciseSummary(BaseModel):
    """One exercise's full attempt history, collapsed to the numbers a
    teacher actually needs - the latest attempt (what the student most
    recently submitted), the best score across every attempt, and the
    count. Every individual attempt is still available in full via
    `GET /users/{student_id}/exercise-history` - this is the summary, not
    a replacement for that history."""

    exercise_index: int
    kind: ExerciseKind
    prompt: str
    attempt_count: int
    latest_correct: Optional[bool] = None
    latest_score: Optional[int] = None
    latest_submitted_at: Optional[datetime] = None
    latest_reviewed: bool = False
    best_score: Optional[int] = None


class LessonProgressSummary(BaseModel):
    topic_id: str
    topic_title: str
    status: LessonDayState
    exercises: list[LessonExerciseSummary] = Field(default_factory=list)
    # "2/3" style fraction over exercises that have a correct/incorrect
    # verdict (open-ended writing/speaking exercises are excluded from the
    # denominator, not silently scored as wrong).
    graded_exercise_count: int = 0
    correct_exercise_count: int = 0


class EnrollmentJourney(BaseModel):
    enrollment: EnrollmentPublic
    has_schedule: bool
    total_days: int = 0
    current_day: int = 0
    lessons: list[LessonProgressSummary] = Field(default_factory=list)


class AiSessionSummary(BaseModel):
    id: str
    topic_id: Optional[str] = None
    topic_title: Optional[str] = None
    enrollment_id: Optional[str] = None
    mode: str
    state: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_minutes: Optional[float] = None
    # Whether a scored report exists - never a fabricated score/summary when
    # one doesn't (e.g. the session ended with too little transcript to
    # score, or scoring itself isn't configured in this environment).
    has_report: bool = False


class HomeworkSummary(BaseModel):
    id: str
    title: str
    kind: str
    due_at: datetime
    topic_id: Optional[str] = None
    # Always False today - Homework submission/completion tracking does not
    # exist in this data model (confirmed by inspection, not merely
    # unqueried). Surfaced explicitly so the UI can say "not tracked yet"
    # rather than imply a status this system has no way to know.
    submission_tracking_available: bool = False


class StudentLearningJourney(BaseModel):
    student_id: str
    full_name: str
    email: str
    is_active: bool
    enrollments: list[EnrollmentJourney] = Field(default_factory=list)
    ai_sessions: list[AiSessionSummary] = Field(default_factory=list)
    homework: list[HomeworkSummary] = Field(default_factory=list)


class ExerciseAttemptHistoryItem(BaseModel):
    """One real submitted attempt, newest first - including a superseded
    (no-longer-active) one, never silently dropped. See
    `app.models.exercise_attempt.ExerciseAttempt.superseded`."""

    id: str
    created_at: datetime
    exercise_index: int
    exercise_kind: ExerciseKind
    exercise_prompt: str
    answer: int | str | list[int]
    answer_display: str
    correct: Optional[bool] = None
    correct_option: Optional[int] = None
    score: Optional[int] = None
    reviewed: bool = False
    superseded: bool = False
    feedback: str = ""
