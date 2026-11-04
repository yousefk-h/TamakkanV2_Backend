"""A student's answer to one version of one teacher-authored lesson skill."""

from typing import Annotated, Optional

from beanie import Indexed
from pydantic import Field

from app.models.base import BaseDocument
from app.models.topic_content import ExerciseKind


class ExerciseAttempt(BaseDocument):
    student_id: Annotated[str, Indexed()]
    student_name: str
    topic_id: Annotated[str, Indexed()]
    topic_title: str
    exercise_index: int
    exercise_hash: str
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
    reviewed_by_id: Optional[str] = None

    class Settings:
        name = "exercise_attempts"
        indexes = [
            [("student_id", 1), ("topic_id", 1), ("created_at", -1)],
            [("topic_id", 1), ("reviewed", 1), ("created_at", -1)],
        ]
