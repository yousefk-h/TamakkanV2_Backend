"""Activity is a completed session or submitted recording, never a login.

Periods include today's partial UTC day. Sessions are attributed to their start
day, including their full wall-clock duration; this is not student speech time.
Current-roster membership scopes both periods. No model/API calls are needed.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

from app.models.user import Role, User
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.user_repository import UserRepository
from app.schema.analytics import ActivityAnalytics, DailyActivity, ScoreGroup, SessionMetric, SubmissionMetric
from app.utils.time import utcnow


def summarize_activity(student_ids: list[str], sessions: list[SessionMetric], submissions: list[SubmissionMetric], days: int, now: datetime) -> ActivityAnalytics:
    start = datetime.combine(now.date() - timedelta(days=days - 1), time.min)
    previous_start = start - timedelta(days=days)
    allowed = set(student_ids)
    sessions = [s for s in sessions if s.student_id in allowed and previous_start <= s.started_at <= s.ended_at <= now]
    submissions = [s for s in submissions if s.student_id in allowed and previous_start <= s.submitted_at <= now]
    current_sessions = [s for s in sessions if s.started_at >= start]
    current_submissions = [s for s in submissions if s.submitted_at >= start]
    active = {s.student_id for s in current_sessions} | {s.student_id for s in current_submissions}
    previous_active = {s.student_id for s in sessions if s.started_at < start} | {s.student_id for s in submissions if s.submitted_at < start}
    returning = active & previous_active

    daily_students = defaultdict(set)
    daily_sessions = defaultdict(int)
    daily_submissions = defaultdict(int)
    daily_seconds = defaultdict(float)
    score_groups = defaultdict(list)
    for session in current_sessions:
        day = session.started_at.date()
        daily_students[day].add(session.student_id)
        daily_sessions[day] += 1
        daily_seconds[day] += (session.ended_at - session.started_at).total_seconds()
        report = session.report
        if report and report.overall_score is not None and (not report.analysis or report.analysis.status == "assessed"):
            # Never mix the new level-calibrated rubric with older assessments.
            key = (report.analysis.rubric_version, report.analysis.target_level) if report.analysis else ("legacy", None)
            score_groups[key].append((report.overall_score, session.student_id))
    for submission in current_submissions:
        day = submission.submitted_at.date()
        daily_students[day].add(submission.student_id)
        daily_submissions[day] += 1
    daily = []
    for offset in range(days):
        day = start.date() + timedelta(days=offset)
        daily.append(DailyActivity(date=day, active_students=len(daily_students[day]), completed_sessions=daily_sessions[day],
                                   submissions=daily_submissions[day], session_minutes=round(daily_seconds[day] / 60, 1)))
    reports = [s.report for s in current_sessions if s.report is not None]
    scored_count = sum(len(items) for items in score_groups.values())
    return ActivityAnalytics(
        generated_at=now, days=days, start_date=start.date(), end_date=now.date(), roster_students=len(allowed),
        active_students=len(active), previous_active_students=len(previous_active), returning_students=len(returning),
        returning_rate=round(len(returning) / len(previous_active) * 100, 1) if previous_active else None,
        completed_sessions=len(current_sessions), topics_practiced=len({s.topic_id for s in current_sessions if s.topic_id}),
        session_minutes=round(sum(daily_seconds.values()) / 60, 1), submissions=len(current_submissions),
        reports_available=len(reports), unscored_sessions=len(current_sessions) - scored_count,
        reports_awaiting_review=sum(not r.reviewed for r in reports),
        submissions_awaiting_review=sum(not s.reviewed for s in current_submissions), daily=daily,
        score_groups=[ScoreGroup(rubric=rubric, level=level, report_count=len(items), student_count=len({student for _, student in items}),
                                 average_score=round(sum(score for score, _ in items) / len(items), 1))
                      for (rubric, level), items in sorted(score_groups.items(), key=lambda group: (group[0][0], group[0][1] or ""))],
    )


class AnalyticsService:
    def __init__(self):
        self.repository = AnalyticsRepository()
        self.users = UserRepository()

    async def for_admin(self, admin: User, days: int) -> ActivityAnalytics:
        """The Center Admin (`Role.ADMINISTRATOR`) gets center-wide analytics across every Teacher's roster; a
        Teacher gets their own roster only."""
        roster = (
            await self.users.get_all_students() if admin.role == Role.ADMINISTRATOR else await self.users.get_students_created_by(admin.id)
        )
        return await self.for_students([student.id for student in roster], days)

    async def for_students(self, student_ids: list[str], days: int) -> ActivityAnalytics:
        now = utcnow()
        previous_start = datetime.combine(now.date() - timedelta(days=2 * days - 1), time.min)
        sessions = await self.repository.sessions(student_ids, previous_start, now)
        submissions = await self.repository.submissions(student_ids, previous_start, now)
        return summarize_activity(student_ids, sessions, submissions, days, now)


def get_analytics_service() -> AnalyticsService:
    return AnalyticsService()
