"""Computes a student's streak, badges, and 30-day challenge position from
their real activity - no separate "progress" document is ever written; see
`app.schema.progress` for why this is recomputed per request instead.

Badge thresholds are deliberately simple, single constants rather than
per-plan/per-level configuration - nobody has asked for that yet, and this
mirrors the "no speculative flexibility" choice already made for
`app.core.session_scoring`'s fixed rubric.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.services.enrollment_service import EnrollmentContext

from app.core.challenge import CHALLENGE_TOTAL_DAYS, END_OF_WEEK_CHECKIN_DAYS
from app.models.calendar_template import CalendarTemplate, TemplateKind
from app.models.cohort import Cohort
from app.models.enrollment import Enrollment, EnrollmentStatus
from app.models.program import Program
from app.models.live_session import LiveSession, SessionState
from app.models.recording_submission import RecordingSubmission
from app.models.topic_content import TopicContent
from app.models.user import PlanType, User
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.recording_submission_repository import RecordingSubmissionRepository
from app.repositories.topic_content_repository import TopicContentRepository
from app.schema.alerts import StudentAlerts
from app.schema.challenge import ChallengeCalendar, LessonDaySlot, LessonDayState, TopicSlot
from app.schema.progress import Badge, BadgeKey, ChallengeMilestone, StudentProgress
from app.utils.time import utcnow

STREAK_BADGE_DAYS = 7
VOCAB_MASTER_TERMS = 150
TOP_SCORER_MIN_SCORE = 90
FAST_IMPROVER_MIN_GAIN = 20
FAST_IMPROVER_WINDOW_DAYS = 30
CHALLENGE_MILESTONE_DAYS = [10, 20, 30]

# A session reaches ENDED whether the student talked the whole time, hung up
# the instant Mariam connected, or went idle until the timeout fired - the
# state machine treats those identically, but only the first one is actually
# "the course completed." Gated on turn counts and wall-clock duration only,
# never on transcript *content* - Gemini Live's STT occasionally hallucinates
# a stray word from background noise, so a real back-and-forth is judged by
# how much was exchanged, not what it says.
MIN_CONVERSATION_TURNS_PER_SPEAKER = 2
MIN_CONVERSATION_SECONDS = 60


@dataclass
class _StreakResult:
    current: int
    longest: int
    # The date the longest run last extended to - a badge's `earned_at`, not
    # a display value, so it stays a plain `date` rather than a `datetime`.
    longest_run_end: date | None


def _compute_streaks(active_dates: list[date]) -> _StreakResult:
    """`active_dates` must already be sorted ascending and deduplicated."""
    if not active_dates:
        return _StreakResult(current=0, longest=0, longest_run_end=None)

    longest = run = 1
    longest_run_end = active_dates[0]
    for previous, current in zip(active_dates, active_dates[1:]):
        run = run + 1 if (current - previous).days == 1 else 1
        if run > longest:
            longest = run
            longest_run_end = current

    # "Current" only survives if the most recent active day was today or
    # yesterday - two calendar days with nothing logged breaks it, same rule
    # a habit-tracker streak normally uses.
    today = utcnow().date()
    if (today - active_dates[-1]).days > 1:
        return _StreakResult(current=0, longest=longest, longest_run_end=longest_run_end)

    current_run = 1
    for i in range(len(active_dates) - 1, 0, -1):
        if (active_dates[i] - active_dates[i - 1]).days == 1:
            current_run += 1
        else:
            break
    return _StreakResult(current=current_run, longest=longest, longest_run_end=longest_run_end)


def _first_vocab_threshold_crossing(
    sessions_oldest_first: list[LiveSession],
    topics_by_id: dict[str, TopicContent],
) -> tuple[datetime | None, int]:
    """Walks sessions oldest-first, counting each distinct topic's vocabulary
    exactly once (the first time the student has an ended session for it),
    and returns the timestamp the running total first reached
    `VOCAB_MASTER_TERMS`, plus the final total either way.
    """
    seen_topic_ids: set[str] = set()
    total = 0
    crossed_at: datetime | None = None
    for session in sessions_oldest_first:
        if not session.topic_id or session.topic_id in seen_topic_ids:
            continue
        seen_topic_ids.add(session.topic_id)
        topic = topics_by_id.get(session.topic_id)
        if topic is None:
            continue
        total += len(topic.vocabulary)
        if crossed_at is None and total >= VOCAB_MASTER_TERMS:
            crossed_at = session.started_at
    return crossed_at, total


def _program_today(start_date: Optional[datetime], total_days: int = CHALLENGE_TOTAL_DAYS) -> int:
    """1-based, capped to `CHALLENGE_TOTAL_DAYS`, anchored to the date the
    assigning admin set as "day 1" (`User.program_start_date`) - not the
    student's own first activity. A teacher-chosen start date is a
    deliberate decision, not an accident of account-creation timing, so a
    student who hasn't shown up since it passed really is behind - unlike
    Phase 1's activity-anchored version, this is allowed to read as "already
    behind" from day one.

    `None` (no plan ever assigned) or a start date still in the future both
    return `0`, not `1` - the day-by-day loop below locks every real day
    (`day > today`) when `today` is `0`, so "the program hasn't started"
    needs no special-casing anywhere else.
    """
    if start_date is None:
        return 0
    return max(0, min(total_days, (utcnow().date() - start_date.date()).days + 1))


def _had_a_real_conversation(session: LiveSession) -> bool:
    """Whether an ENDED session should actually count as completing its
    topic - see the module-level constants above for why this checks turn
    counts and duration only, never transcript content. Callers are expected
    to have already filtered to `state == ENDED and ended_at is not None`;
    this re-checks `ended_at` defensively rather than trusting that."""
    if session.ended_at is None:
        return False
    if (session.ended_at - session.started_at).total_seconds() < MIN_CONVERSATION_SECONDS:
        return False
    student_turns = sum(1 for turn in session.transcript_history if turn.speaker == "student")
    mariam_turns = sum(1 for turn in session.transcript_history if turn.speaker == "mariam")
    return student_turns >= MIN_CONVERSATION_TURNS_PER_SPEAKER and mariam_turns >= MIN_CONVERSATION_TURNS_PER_SPEAKER


def _build_schedule(
    template: Optional[CalendarTemplate],
    sessions: list[LiveSession],
    start_date: Optional[datetime],
) -> ChallengeCalendar:
    """Pure computation behind `ProgressService.get_lesson_schedule` - takes
    already-fetched rows so `get_alerts_for_students` can drive it from one
    batched query per collection instead of re-fetching per student.

    The backlog is a plain FIFO of individual topic ids - a day can add any
    number of its own courses to it, and every course currently in the
    backlog is shown as "due" on the day being processed. A course drops out
    the moment it's completed (`DONE`); otherwise it stays in the backlog for
    the next day (`CURRENT` on today, `MISSED` - and counted in
    `pending_count` - on any earlier day), which is exactly the Phase 1
    single-topic-per-day rule generalized: with 0-or-1 new topic per day this
    reproduces that behavior exactly.

    Days beyond today are locked; their courses are only a preview of what's
    next (the backlog as it stands right now, or that day's own template
    entry if the backlog is empty) - not a commitment about what will
    actually land there once earlier days are resolved.
    """
    if template is None:
        return ChallengeCalendar(lesson_days=[], pending_count=0, has_template=False)
    if not template.days:
        return ChallengeCalendar(lesson_days=[], pending_count=0, has_template=True, total_days=template.duration_days, rest_days=template.rest_days)
    base_assignment: dict[int, list[str]] = {entry.day: entry.topic_ids for entry in template.days}

    ended = [s for s in sessions if s.state == SessionState.ENDED and s.ended_at is not None]
    completed_topic_ids = {s.topic_id for s in ended if s.topic_id and _had_a_real_conversation(s)}
    today = _program_today(start_date, template.duration_days)

    lesson_days: list[LessonDaySlot] = []
    pending_count = 0
    backlog: deque[str] = deque()
    for day in range(1, template.duration_days + 1):
        if day in template.rest_days:
            continue
        days_new_topic_ids = base_assignment.get(day, [])

        if day > today:
            preview_ids = list(backlog) if backlog else days_new_topic_ids
            lesson_days.append(LessonDaySlot(day=day, topics=[TopicSlot(topic_id=tid, state=LessonDayState.LOCKED) for tid in preview_ids]))
            continue

        backlog.extend(days_new_topic_ids)  # this day's own courses queue in behind whatever's already owed
        todays_topic_ids = list(backlog)  # everything currently owed is due today
        slots: list[TopicSlot] = []
        still_owed: deque[str] = deque()
        for topic_id in todays_topic_ids:
            if topic_id in completed_topic_ids:
                slots.append(TopicSlot(topic_id=topic_id, state=LessonDayState.DONE))
            elif day == today:
                slots.append(TopicSlot(topic_id=topic_id, state=LessonDayState.CURRENT))
                still_owed.append(topic_id)
            else:
                slots.append(TopicSlot(topic_id=topic_id, state=LessonDayState.MISSED))
                pending_count += 1
                still_owed.append(topic_id)
        backlog = still_owed
        lesson_days.append(LessonDaySlot(day=day, topics=slots))

    return ChallengeCalendar(lesson_days=lesson_days, pending_count=pending_count, total_days=template.duration_days, rest_days=template.rest_days)


_STATUS_PRECEDENCE = {LessonDayState.LOCKED: 0, LessonDayState.MISSED: 1, LessonDayState.CURRENT: 2, LessonDayState.DONE: 3}


def _for_current_enrollment(student: User, records: list) -> list:
    """Keep progress writes from the student's current Program/stage.
    Rows created before enrollment context existed remain readable as legacy
    history, preserving historical data during migration."""
    return [
        record for record in records
        if getattr(record, "program_id", None) is None
        or (
            getattr(record, "program_id", None) == student.program_id
            and getattr(record, "program_level_id", None) in (None, student.program_level_id)
        )
    ]


def resolve_topic_statuses(schedule: ChallengeCalendar) -> dict[str, LessonDayState]:
    """The one overall status for each topic mentioned anywhere in a
    student's calendar. A topic can appear on more than one day's slot while
    it's stuck in the rollover backlog, so this picks whichever status
    actually matters: `DONE` wins outright; otherwise `CURRENT` (still owed,
    today) beats `MISSED` (owed on a past day) beats `LOCKED` (not reached
    yet). Used both to gate whether a Student may open a lesson
    (`app.core.entitlement`) and to badge the Lessons library.
    """
    statuses: dict[str, LessonDayState] = {}
    for day_slot in schedule.lesson_days:
        for topic_slot in day_slot.topics:
            existing = statuses.get(topic_slot.topic_id)
            if existing is None or _STATUS_PRECEDENCE[topic_slot.state] > _STATUS_PRECEDENCE[existing]:
                statuses[topic_slot.topic_id] = topic_slot.state
    return statuses


def _build_alerts(
    template: Optional[CalendarTemplate],
    sessions: list[LiveSession],
    submissions: list[RecordingSubmission],
    start_date: Optional[datetime],
) -> StudentAlerts:
    """Pure computation behind `ProgressService.get_alerts` - same
    already-fetched-rows shape as `_build_schedule`, for the same reason.
    """
    schedule = _build_schedule(template, sessions, start_date)
    total_days = template.duration_days if template else CHALLENGE_TOTAL_DAYS
    checkin_days = set(template.rest_days) if template else END_OF_WEEK_CHECKIN_DAYS
    today = _program_today(start_date, total_days)
    submitted_days = {sub.day for sub in submissions if sub.day is not None}
    missed_submission_days = sorted(d for d in checkin_days if d < today and d not in submitted_days)
    return StudentAlerts(
        pending_rollover_count=schedule.pending_count,
        missed_submission_days=missed_submission_days,
        has_alerts=schedule.pending_count > 0 or bool(missed_submission_days),
    )


def _first_fast_improvement(sessions_oldest_first_with_report: list[LiveSession]) -> datetime | None:
    """A "+20 points within 30 days" badge: the earliest later session whose
    score beat some earlier session (within the window) by the required gain.
    Quadratic in session count, which is fine - a student's own report
    history is never more than a few hundred sessions.
    """
    for i, later in enumerate(sessions_oldest_first_with_report):
        for earlier in sessions_oldest_first_with_report[:i]:
            gap_days = (later.started_at - earlier.started_at).days
            if 0 <= gap_days <= FAST_IMPROVER_WINDOW_DAYS:
                assert later.report is not None and earlier.report is not None
                if later.report.overall_score is None or earlier.report.overall_score is None:
                    continue
                later_rubric = later.report.analysis.rubric_version if later.report.analysis else "legacy"
                earlier_rubric = earlier.report.analysis.rubric_version if earlier.report.analysis else "legacy"
                if later_rubric != earlier_rubric:
                    continue
                if later.report.analysis:
                    if not later.topic_id or not later.report.analysis.target_level:
                        continue
                    if later.topic_id != earlier.topic_id or later.report.analysis.target_level != earlier.report.analysis.target_level:
                        continue
                if later.report.overall_score - earlier.report.overall_score >= FAST_IMPROVER_MIN_GAIN:
                    return later.started_at
    return None


class ProgressService:
    def __init__(
        self,
        session_repository: LiveSessionRepository,
        submission_repository: RecordingSubmissionRepository,
        topic_repository: TopicContentRepository,
        calendar_template_repository: CalendarTemplateRepository,
    ) -> None:
        self.session_repository = session_repository
        self.submission_repository = submission_repository
        self.topic_repository = topic_repository
        self.calendar_template_repository = calendar_template_repository

    async def get_progress(self, student: User) -> StudentProgress:
        sessions = await self.session_repository.get_all_for_student(student.id)
        ended = sorted(
            (s for s in sessions if s.state == SessionState.ENDED and s.ended_at is not None),
            key=lambda s: s.started_at,
        )
        submissions = await self.submission_repository.get_all_for_student(student.id)

        active_dates = sorted({s.started_at.date() for s in ended} | {sub.submitted_at.date() for sub in submissions})
        streaks = _compute_streaks(active_dates)

        reported = [s for s in ended if s.report is not None and s.report.overall_score is not None]
        topic_ids = list({s.topic_id for s in ended if s.topic_id})
        topics_by_id = {t.id: t for t in await self.topic_repository.get_many_by_ids(topic_ids)}

        vocab_crossed_at, _ = _first_vocab_threshold_crossing(ended, topics_by_id)
        top_scorer = next((s for s in reported if s.report and s.report.overall_score >= TOP_SCORER_MIN_SCORE), None)
        fast_improver_at = _first_fast_improvement(reported)
        level_complete_at = await self._level_complete_at(student, _for_current_enrollment(student, ended))

        badges = [
            Badge(key=BadgeKey.FIRST_CLASS, earned=bool(ended), earned_at=ended[0].started_at if ended else None),
            Badge(
                key=BadgeKey.STREAK,
                earned=streaks.longest >= STREAK_BADGE_DAYS,
                earned_at=datetime.combine(streaks.longest_run_end, datetime.min.time())
                if streaks.longest >= STREAK_BADGE_DAYS and streaks.longest_run_end
                else None,
            ),
            Badge(key=BadgeKey.VOCAB_MASTER, earned=vocab_crossed_at is not None, earned_at=vocab_crossed_at),
            Badge(key=BadgeKey.TOP_SCORER, earned=top_scorer is not None, earned_at=top_scorer.started_at if top_scorer else None),
            Badge(key=BadgeKey.FAST_IMPROVER, earned=fast_improver_at is not None, earned_at=fast_improver_at),
            Badge(key=BadgeKey.LEVEL_COMPLETE, earned=level_complete_at is not None, earned_at=level_complete_at),
        ]

        template = await self._resolve_template(student)
        effective_start = await self._effective_program_start_date(student)
        total_days = template.duration_days if template else CHALLENGE_TOTAL_DAYS
        challenge_day = _program_today(effective_start, total_days)
        milestones = [ChallengeMilestone(day=d, reached=challenge_day >= d) for d in CHALLENGE_MILESTONE_DAYS]
        _program_id, program_name = await self._resolve_program_context(student)
        has_multiple_enrollments = await self._has_multiple_active_enrollments(student)

        today = utcnow().date()
        active_date_set = set(active_dates)
        last_7_days_active = [(today - timedelta(days=offset)) in active_date_set for offset in range(6, -1, -1)]

        return StudentProgress(
            current_streak=streaks.current,
            longest_streak=streaks.longest,
            total_active_days=len(active_dates),
            challenge_day=challenge_day,
            challenge_total_days=total_days,
            program_start_date=effective_start.date() if effective_start else None,
            program_name=program_name,
            has_multiple_enrollments=has_multiple_enrollments,
            last_7_days_active=last_7_days_active,
            last_7_days_dates=[today - timedelta(days=offset) for offset in range(6, -1, -1)],
            badges=badges,
            milestones=milestones,
        )

    async def _resolve_template(self, student: User) -> Optional[CalendarTemplate]:
        """The calendar this student actually follows. Tries the legacy
        `plan`/`program_id`/`assigned_level` path first (zero behavior change
        for every account that predates Cohorts/Enrollments); a student
        enrolled ONLY through a Cohort - no `assign_plan` call ever made for
        them - falls back to their single active Enrollment's effective
        schedule, the same canonical resolution `EnrollmentService`/"My
        Learning" already uses. See `_resolve_enrollment_fallback` for why
        this never guesses among several active enrollments."""
        legacy = await self._resolve_legacy_template(student)
        if legacy is not None:
            return legacy
        fallback = await self._resolve_enrollment_fallback(student)
        return fallback[0] if fallback else None

    async def _resolve_enrollment_fallback(self, student: User) -> Optional[tuple[CalendarTemplate, datetime]]:
        """(template, anchor date) from the student's Enrollment, for a
        student with no usable legacy plan at all. Deliberately `None` for 0
        or 2+ active enrollments: Student Home/the Challenge page is a
        single-calendar view and must never guess which one to show - a
        multi-enrollment student already has the enrollment-aware "My
        Learning" page (`EnrollmentService`) for exactly that case."""
        active = await Enrollment.find({
            "student_id": student.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }).to_list()
        if len(active) != 1:
            return None
        enrollment = active[0]
        template = await self.calendar_template_repository.get_effective_schedule(
            enrollment.program_id, enrollment.program_level_id, enrollment.cohort_id
        )
        if template is None:
            return None
        from app.services.enrollment_service import EnrollmentService  # local import avoids a module cycle at import time

        cohort = await Cohort.find_one({"_id": enrollment.cohort_id}) if enrollment.cohort_id else None
        anchor = EnrollmentService.schedule_anchor_date(enrollment, cohort) or enrollment.joined_at
        return template, anchor

    async def _resolve_program_context(self, student: User) -> tuple[Optional[str], Optional[str]]:
        """(program_id, program_name) for whichever Program this student's
        single calendar actually belongs to - legacy `program_id` first, then
        the same single-active-Enrollment fallback `_resolve_template` uses.
        `program_id` is `None` for a student with 0 or 2+ active enrollments
        and no legacy program either (see `has_multiple_enrollments` on
        `StudentProgress` for how the frontend tells those two apart)."""
        program_id = student.program_id
        if program_id is None:
            active = await Enrollment.find({
                "student_id": student.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
            }).to_list()
            if len(active) == 1:
                program_id = active[0].program_id
        if program_id is None:
            return None, None
        program = await Program.find_one({"_id": program_id, "is_deleted": False})
        return program_id, program.name if program else None

    async def _has_multiple_active_enrollments(self, student: User) -> bool:
        count = await Enrollment.find({
            "student_id": student.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }).count()
        return count > 1

    async def _effective_program_start_date(self, student: User) -> Optional[datetime]:
        """`student.program_start_date` when set (legacy path, unchanged);
        otherwise the single-enrollment fallback's anchor date, so the
        Challenge day/date and the day-by-day calendar agree with whatever
        template `_resolve_template` actually picked."""
        if student.program_start_date is not None:
            return student.program_start_date
        fallback = await self._resolve_enrollment_fallback(student)
        return fallback[1] if fallback else None

    async def _resolve_legacy_template(self, student: User) -> Optional[CalendarTemplate]:
        """The calendar this student actually follows: a Standard student
        follows whichever template their own admin has activated for their
        level; a Pro student follows the specific template their admin
        assigned them (`UserService.assign_plan`). `None` either way means
        nobody has set one up yet.
        """
        if student.plan == PlanType.STANDARD:
            if student.assigned_level is None:
                return None
            if student.program_id:
                if student.program_level_id:
                    program_level_template = await self.calendar_template_repository.get_active_standard_for_program_level(
                        student.program_id, student.program_level_id
                    )
                    if program_level_template is not None:
                        return program_level_template
                program_template = await self.calendar_template_repository.get_active_standard_for_program(
                    student.program_id, student.assigned_level
                )
                if program_template is not None:
                    return program_template
            if student.created_by_id is None:
                return None
            return await self.calendar_template_repository.get_active_standard(student.created_by_id, student.assigned_level)
        if student.plan == PlanType.PRO:
            if student.assigned_plan_template_id is None:
                return None
            return await self.calendar_template_repository.get_by_id(student.assigned_plan_template_id)
        return None

    @staticmethod
    def _minutes_from_template(template: Optional[CalendarTemplate], daily_budget: int, topic_id: str) -> int:
        if template is None:
            return daily_budget
        entry = next((e for e in template.days if topic_id in e.topic_ids), None)
        if entry is None:
            return daily_budget
        if topic_id in entry.custom_minutes:
            return entry.custom_minutes[topic_id]
        automatic_ids = [tid for tid in entry.topic_ids if tid not in entry.custom_minutes]
        if not automatic_ids:
            return daily_budget
        remaining = daily_budget - sum(entry.custom_minutes.values())
        return max(1, remaining // len(automatic_ids))

    async def get_topic_session_minutes(self, student: User, topic_id: str) -> int:
        """How many minutes this student's next Mariam session for
        `topic_id` should run - the teacher's `custom_minutes` override if
        one was set for it, otherwise an even split of whatever's left of
        the student's daily budget among that day's other automatic
        (non-overridden) courses. Computed from the topic's original day
        grouping in the template, not from where rollover currently shows
        it - falling behind changes *when* a course unlocks, never how long
        its session gets. Falls back to the full daily budget if the
        student has no template, or the topic isn't on it at all (shouldn't
        happen given entitlement already requires it to be).
        """
        daily_budget = student.duration_override_minutes or student.max_session_duration_minutes
        template = await self._resolve_template(student)
        return self._minutes_from_template(template, daily_budget, topic_id)

    async def get_topic_session_minutes_for_context(self, student: User, topic_id: str, context: "EnrollmentContext") -> int:
        """Enrollment-aware counterpart to `get_topic_session_minutes` - looks
        up the Cohort/Program stage's own effective schedule
        (`CalendarTemplateRepository.get_effective_schedule`) instead of the
        student's single legacy calendar. Falls straight through to the
        legacy computation when `context.enrollment is None` (no Enrollment
        exists for this student at all) - zero behavior change there.
        """
        daily_budget = student.duration_override_minutes or student.max_session_duration_minutes
        if context.enrollment is None:
            template = await self._resolve_template(student)
        else:
            template = await self.calendar_template_repository.get_effective_schedule(
                context.program_id, context.program_level_id, context.cohort_id
            )
        return self._minutes_from_template(template, daily_budget, topic_id)

    async def get_lesson_schedule(self, student: User) -> ChallengeCalendar:
        """The student's own day-by-day calendar, taken directly from their
        assigned `CalendarTemplate` - see `_resolve_template` and
        `_build_schedule` for the rollover/backlog rules. No template
        assigned yet (or one with an empty `days` list) means there's
        nothing to schedule at all.
        """
        template = await self._resolve_template(student)
        sessions = _for_current_enrollment(student, await self.session_repository.get_all_for_student(student.id))
        effective_start = await self._effective_program_start_date(student)
        return _build_schedule(template, sessions, effective_start)

    async def get_topic_statuses(self, student: User) -> dict[str, LessonDayState]:
        """Thin wrapper used by `app.core.entitlement` - see
        `resolve_topic_statuses` for the actual precedence logic.
        """
        return resolve_topic_statuses(await self.get_lesson_schedule(student))

    async def get_topic_statuses_for_context(self, student: User, context: "EnrollmentContext") -> dict[str, LessonDayState]:
        """Enrollment-aware counterpart to `get_topic_statuses`, used when a
        caller (session start, exercise submission, topic read) resolved an
        explicit `EnrollmentContext` - see `EnrollmentService.resolve_context`.
        `context.enrollment is None` means "no Enrollment exists for this
        student at all" (never migrated / center hasn't adopted Cohorts), so
        this falls straight through to the exact legacy computation above -
        zero behavior change for any account that predates Enrollments.

        When there IS a resolved enrollment, the schedule/session filtering
        both key off that specific enrollment's `(program_id, program_level_id,
        cohort_id)` rather than the student's single scalar assignment, so a
        student enrolled in two Programs (or two Cohorts of the same Program)
        gets independent, non-leaking statuses per enrollment.
        """
        if context.enrollment is None:
            return await self.get_topic_statuses(student)
        template = await self.calendar_template_repository.get_effective_schedule(
            context.program_id, context.program_level_id, context.cohort_id
        )
        all_sessions = await self.session_repository.get_all_for_student(student.id)
        sessions = [
            s for s in all_sessions
            if s.enrollment_id == context.enrollment.id
            or (
                context.legacy_attributable
                and s.enrollment_id is None
                and s.program_id == context.program_id
                and s.program_level_id == context.program_level_id
            )
        ]
        cohort = await Cohort.find_one({"_id": context.cohort_id}) if context.cohort_id else None
        from app.services.enrollment_service import EnrollmentService  # local import avoids a module cycle at import time

        anchor = EnrollmentService.schedule_anchor_date(context.enrollment, cohort)
        schedule = _build_schedule(template, sessions, anchor)
        return resolve_topic_statuses(schedule)

    async def get_calendar_topics_and_statuses(self, student: User) -> tuple[list[str], dict[str, LessonDayState]]:
        """Every topic anywhere in the student's own calendar, in day order,
        paired with its current status (missing from the dict means it
        hasn't been reached yet - treat as `LOCKED`).

        Unlike `get_lesson_schedule`'s day-by-day view - which, for a
        student who's fallen behind, only ever previews the *next*
        backlogged course on a locked day, not everything still further out
        - this always lists the whole template. `TopicContentService
        .list_for_requester` needs the full catalogue, not just whatever the
        schedule has surfaced so far.
        """
        template = await self._resolve_template(student)
        if template is None or not template.days:
            return [], {}
        ordered_ids: list[str] = []
        seen: set[str] = set()
        for entry in sorted(template.days, key=lambda e: e.day):
            for topic_id in entry.topic_ids:
                if topic_id not in seen:
                    seen.add(topic_id)
                    ordered_ids.append(topic_id)
        statuses = await self.get_topic_statuses(student)
        return ordered_ids, statuses

    async def get_alerts(self, student: User) -> StudentAlerts:
        """Single-student version of `get_alerts_for_students` - the student
        detail page's own "Alerts" section.
        """
        template = await self._resolve_template(student)
        sessions = _for_current_enrollment(student, await self.session_repository.get_all_for_student(student.id))
        submissions = _for_current_enrollment(student, await self.submission_repository.get_all_for_student(student.id))
        effective_start = await self._effective_program_start_date(student)
        return _build_alerts(template, sessions, submissions, effective_start)

    async def get_alerts_for_students(self, students: list[User]) -> dict[str, StudentAlerts]:
        """The admin roster's badge - one batched query per collection
        instead of `get_alerts` called once per student, which at a couple
        hundred students would mean hundreds of round trips for a single
        page load.
        """
        if not students:
            return {}
        student_ids = [s.id for s in students]

        sessions_by_student: dict[str, list[LiveSession]] = defaultdict(list)
        for session in await self.session_repository.get_all_for_admin(student_ids):
            sessions_by_student[session.student_id].append(session)

        submissions_by_student: dict[str, list[RecordingSubmission]] = defaultdict(list)
        for submission in await self.submission_repository.search(student_ids):
            submissions_by_student[submission.student_id].append(submission)

        # Standard: one active-template lookup per distinct admin, not per
        # student - in practice this bulk call is always one admin's own
        # roster, so this is usually a single query.
        standard_by_admin_and_level: dict[tuple[str, str], CalendarTemplate] = {}
        admin_ids = {s.created_by_id for s in students if s.plan == PlanType.STANDARD and s.created_by_id}
        for admin_id in admin_ids:
            templates = await self.calendar_template_repository.list_for_admin(admin_id, kind=TemplateKind.STANDARD)
            for template in templates:
                if template.is_active and template.level is not None:
                    standard_by_admin_and_level[(admin_id, template.level.value)] = template

        # Pro: one batch fetch of every distinct assigned template.
        pro_template_ids = list({s.assigned_plan_template_id for s in students if s.assigned_plan_template_id})
        pro_templates_by_id = {t.id: t for t in await self.calendar_template_repository.get_many_by_ids(pro_template_ids)}
        program_template_by_program_and_level: dict[tuple[str, str], CalendarTemplate] = {}
        program_template_by_stage: dict[tuple[str, str], CalendarTemplate] = {}
        for program_id, program_level_id in {
            (s.program_id, s.program_level_id)
            for s in students
            if s.plan == PlanType.STANDARD and s.program_id and s.program_level_id
        }:
            template = await self.calendar_template_repository.get_active_standard_for_program_level(
                program_id, program_level_id
            )
            if template:
                program_template_by_stage[(program_id, program_level_id)] = template
        for program_id, level in {
            (s.program_id, s.assigned_level)
            for s in students
            if s.plan == PlanType.STANDARD and s.program_id and s.assigned_level is not None
        }:
            template = await self.calendar_template_repository.get_active_standard_for_program(program_id, level)
            if template:
                program_template_by_program_and_level[(program_id, level.value)] = template

        alerts: dict[str, StudentAlerts] = {}
        for student in students:
            if student.plan == PlanType.STANDARD and student.program_id and student.program_level_id:
                template = program_template_by_stage.get((student.program_id, student.program_level_id))
            elif student.plan == PlanType.STANDARD and student.program_id and student.assigned_level is not None:
                template = program_template_by_program_and_level.get((student.program_id, student.assigned_level.value))
            elif student.plan == PlanType.STANDARD and student.assigned_level is not None and student.created_by_id:
                template = standard_by_admin_and_level.get((student.created_by_id, student.assigned_level.value))
            elif student.plan == PlanType.PRO and student.assigned_plan_template_id:
                template = pro_templates_by_id.get(student.assigned_plan_template_id)
            else:
                template = None
            alerts[student.id] = _build_alerts(
                template,
                _for_current_enrollment(student, sessions_by_student[student.id]),
                _for_current_enrollment(student, submissions_by_student[student.id]),
                student.program_start_date,
            )
        return alerts

    async def _level_complete_at(self, student: User, ended_oldest_first: list[LiveSession]) -> datetime | None:
        """Earned once the student has an ended session for every topic in
        their own assigned calendar template - Standard only; a Pro custom
        template deliberately spans any level, so "complete your level"
        doesn't mean anything for it. Recomputed live from the template as
        it stands now, so a topic added to the calendar later simply
        un-earns the badge until it's covered too - there's no "locked in at
        time of completion" snapshot here.
        """
        if student.plan != PlanType.STANDARD:
            return None
        template = await self._resolve_template(student)
        if template is None or not template.days:
            return None
        entitled_ids = {tid for entry in template.days for tid in entry.topic_ids}
        covered_at: dict[str, datetime] = {}
        for session in ended_oldest_first:
            if session.topic_id in entitled_ids and session.topic_id not in covered_at and _had_a_real_conversation(session):
                covered_at[session.topic_id] = session.started_at
        if entitled_ids.issubset(covered_at.keys()):
            return max(covered_at.values())
        return None


def get_progress_service() -> ProgressService:
    return ProgressService(
        LiveSessionRepository(), RecordingSubmissionRepository(), TopicContentRepository(), CalendarTemplateRepository()
    )
