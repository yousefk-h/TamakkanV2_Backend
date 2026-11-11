"""Student answer submission and teacher feedback contracts."""

from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, Field, StrictInt, StrictStr

from app.models.exercise_attempt import ExerciseAttempt
from app.models.topic_content import ExerciseKind
from app.schema.base import BaseSchema

AnswerText = Annotated[StrictStr, Field(max_length=5000)]


class ExerciseAttemptCreate(BaseModel):
    topic_id: str
    exercise_index: int = Field(ge=0, le=49)
    topic_updated_at: datetime
    # list[StrictInt]: GAME's pair-match indices. list[AnswerText]: WORD_ORDER's
    # chosen words, in order (never indices - see that kind's own note on why
    # the client never sees a position that maps back to the true order).
    answer: StrictInt | AnswerText | list[StrictInt] | Annotated[list[AnswerText], Field(max_length=20)]
    # Required only when the student has more than one active Enrollment -
    # see `EnrollmentService.resolve_context`. Omitted otherwise (0 or 1
    # active enrollment resolves unambiguously on its own).
    enrollment_id: Optional[str] = None


class ExerciseReviewRequest(BaseModel):
    score: int = Field(ge=0, le=100)
    feedback: str = Field(default="", max_length=2000)


class ExerciseAttemptPublic(BaseSchema):
    student_id: str
    student_name: str
    enrollment_id: Optional[str] = None
    topic_id: str
    topic_title: str
    exercise_index: int
    exercise_kind: ExerciseKind
    exercise_prompt: str
    answer: int | str | list[int] | list[str]
    answer_display: str
    correct: Optional[bool]
    correct_option: Optional[int]
    score: Optional[int]
    reviewed: bool
    feedback: str
    reviewed_by_id: Optional[str]

    @classmethod
    def from_attempt(cls, attempt: ExerciseAttempt) -> "ExerciseAttemptPublic":
        return cls.model_validate(attempt.model_dump())
