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
    answer: StrictInt | AnswerText | list[StrictInt]


class ExerciseReviewRequest(BaseModel):
    score: int = Field(ge=0, le=100)
    feedback: str = Field(default="", max_length=2000)


class ExerciseAttemptPublic(BaseSchema):
    student_id: str
    student_name: str
    topic_id: str
    topic_title: str
    exercise_index: int
    exercise_kind: ExerciseKind
    exercise_prompt: str
    answer: int | str | list[int]
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
