"""Bounded, evidence-linked feedback; acoustic measurements are intentionally absent."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EvidenceQuote(BaseModel):
    turn_index: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=600)


class EvidenceInsight(BaseModel):
    observation: str = Field(min_length=1, max_length=700)
    evidence: EvidenceQuote


class LanguageCorrection(BaseModel):
    category: Literal["grammar", "vocabulary", "clarity"]
    evidence: EvidenceQuote
    corrected: str = Field(min_length=1, max_length=700)
    explanation: str = Field(min_length=1, max_length=700)


class RecurringPattern(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceQuote] = Field(min_length=2, max_length=5)
    practice_action: str = Field(min_length=1, max_length=700)


class PracticeGoal(BaseModel):
    focus: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=700)
    success_criterion: str = Field(min_length=1, max_length=500)
    minutes: int = Field(ge=1, le=30)


class AssessmentFeedback(BaseModel):
    status: Literal["assessed", "insufficient_evidence"]
    strengths: list[EvidenceInsight] = Field(max_length=3)
    improvement_areas: list[EvidenceInsight] = Field(max_length=3)
    corrections: list[LanguageCorrection] = Field(max_length=5)
    recurring_patterns: list[RecurringPattern] = Field(max_length=3)
    next_steps: list[PracticeGoal] = Field(min_length=1, max_length=3)


class TranscriptMetrics(BaseModel):
    student_turns: int = Field(ge=0)
    student_words: int = Field(ge=0)
    unique_student_words: int = Field(ge=0)
    average_words_per_turn: float = Field(ge=0)
    # Wall-clock session length, NOT time spent speaking or a speech-rate denominator.
    session_duration_seconds: int | None = Field(default=None, ge=0)


class ProgressComparison(BaseModel):
    previous_session_id: str
    previous_session_date: datetime
    overall_change: int
    fluency_change: int
    vocabulary_change: int
    grammar_change: int
    complexity_change: int
    student_words_change: int


class LearningAnalysis(BaseModel):
    rubric_version: Literal["tamakkan-text-v1"] = "tamakkan-text-v1"
    basis: Literal["transcript_only"] = "transcript_only"
    pronunciation_status: Literal["not_assessed_no_audio"] = "not_assessed_no_audio"
    target_level: str | None = None
    status: Literal["assessed", "insufficient_evidence"]
    metrics: TranscriptMetrics
    feedback: AssessmentFeedback | None = None
    comparison: ProgressComparison | None = None
