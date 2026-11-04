"""Read-only aggregates. Dates and period boundaries are UTC calendar days."""
from datetime import date, datetime

from pydantic import BaseModel


class AnalysisMetric(BaseModel):
    rubric_version: str
    target_level: str | None = None
    status: str


class ReportMetric(BaseModel):
    overall_score: int | None
    reviewed: bool = False
    analysis: AnalysisMetric | None = None


class SessionMetric(BaseModel):
    """A `LiveSession` projection for `AnalyticsRepository.sessions` -
    `Settings.projection` is Beanie's own mechanism for a bounded read (see
    that repository's module docstring): only these fields are fetched from
    Mongo, never the full document (no transcript, no PII).
    """

    student_id: str
    topic_id: str | None = None
    started_at: datetime
    ended_at: datetime
    report: ReportMetric | None = None

    class Settings:
        projection = {
            "student_id": 1, "topic_id": 1, "started_at": 1, "ended_at": 1,
            "report.overall_score": 1, "report.reviewed": 1,
            "report.analysis.rubric_version": 1, "report.analysis.target_level": 1,
            "report.analysis.status": 1,
        }


class SubmissionMetric(BaseModel):
    student_id: str
    submitted_at: datetime
    reviewed: bool = False


class DailyActivity(BaseModel):
    date: date
    active_students: int
    completed_sessions: int
    submissions: int
    session_minutes: float


class ScoreGroup(BaseModel):
    rubric: str
    level: str | None
    report_count: int
    student_count: int
    average_score: float


class ActivityAnalytics(BaseModel):
    generated_at: datetime
    timezone: str = "UTC"
    days: int
    start_date: date
    end_date: date
    roster_students: int
    active_students: int
    previous_active_students: int
    returning_students: int
    returning_rate: float | None
    completed_sessions: int
    topics_practiced: int
    session_minutes: float
    submissions: int
    reports_available: int
    unscored_sessions: int
    reports_awaiting_review: int
    submissions_awaiting_review: int
    daily: list[DailyActivity]
    score_groups: list[ScoreGroup]
