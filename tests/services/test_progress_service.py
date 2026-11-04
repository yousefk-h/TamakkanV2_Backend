"""`ProgressService` behind fakes for its three repositories - everything it
computes (streaks, badges, challenge day) is pure arithmetic over whatever
sessions/submissions/topics the fakes hand back, so no real Mongo connection
is needed here.
"""

from datetime import datetime, timedelta

from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.live_session import InteractionTurn, LiveSession, SessionState, SpeakingReport
from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.models.topic_content import Level, TopicContent, VocabularyItem
from app.models.user import PlanType, Role, User
from app.schema.challenge import LessonDayState
from app.schema.progress import BadgeKey
from app.services.progress_service import CHALLENGE_MILESTONE_DAYS, ProgressService
from app.utils.time import utcnow

TODAY = utcnow()
ADMIN_ID = "admin-1"


class FakeSessionRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    async def get_all_for_student(self, student_id):
        return [s for s in self.sessions if s.student_id == student_id]

    async def get_all_for_admin(self, student_ids):
        return [s for s in self.sessions if s.student_id in student_ids]


class FakeSubmissionRepository:
    def __init__(self, submissions=None):
        self.submissions = submissions or []

    async def get_all_for_student(self, student_id):
        return [s for s in self.submissions if s.student_id == student_id]

    async def search(self, student_ids, topic_id=None, reviewed=None):
        return [s for s in self.submissions if s.student_id in student_ids]


class FakeTopicRepository:
    def __init__(self, topics=None):
        self.topics = topics or []

    async def get_many_by_ids(self, topic_ids):
        return [t for t in self.topics if t.id in topic_ids]


class FakeCalendarTemplateRepository:
    def __init__(self, templates=None):
        self.templates = templates or []

    async def get_by_id(self, template_id):
        return next((t for t in self.templates if t.id == template_id), None)

    async def get_active_standard(self, owner_admin_id, level):
        return next(
            (
                t
                for t in self.templates
                if t.kind == TemplateKind.STANDARD and t.owner_admin_id == owner_admin_id and t.level == level and t.is_active
            ),
            None,
        )

    async def list_for_admin(self, owner_admin_id, kind=None):
        return [t for t in self.templates if t.owner_admin_id == owner_admin_id and (kind is None or t.kind == kind)]

    async def get_many_by_ids(self, template_ids):
        return [t for t in self.templates if t.id in template_ids]


def make_student(**overrides) -> User:
    defaults = dict(
        email="student@example.com",
        password_hash="x",
        full_name="Student",
        role=Role.STUDENT,
        plan=PlanType.STANDARD,
        assigned_level=Level.BEGINNER,
        created_by_id=ADMIN_ID,
        created_at=TODAY - timedelta(days=60),
        # "Today" is anchored to this, not activity - see `_program_today`.
        # Most tests want day 1 unless they override it to anchor elsewhere.
        program_start_date=TODAY,
    )
    defaults.update(overrides)
    return User(**defaults)


def _as_topic_ids(topic_ids) -> list[str]:
    return topic_ids if isinstance(topic_ids, list) else [topic_ids]


def make_standard_template(
    days: list[tuple[int, str | list[str]]], level=Level.BEGINNER, owner_admin_id=ADMIN_ID, is_active=True
) -> CalendarTemplate:
    """`days` is a list of `(day, topic_ids)` pairs - deliberately not
    contiguous, so a test can leave earlier days unscheduled on purpose.
    `topic_ids` may be a single id (one course that day) or a list (multiple
    courses that day, each tracked/rolled over independently).
    """
    return CalendarTemplate(
        name="Template",
        kind=TemplateKind.STANDARD,
        owner_admin_id=owner_admin_id,
        level=level,
        is_active=is_active,
        days=[CalendarDayEntry(day=day, topic_ids=_as_topic_ids(topic_ids)) for day, topic_ids in days],
    )


def make_custom_template(days: list[tuple[int, str | list[str]]], owner_admin_id=ADMIN_ID) -> CalendarTemplate:
    return CalendarTemplate(
        name="Custom",
        kind=TemplateKind.CUSTOM,
        owner_admin_id=owner_admin_id,
        days=[CalendarDayEntry(day=day, topic_ids=_as_topic_ids(topic_ids)) for day, topic_ids in days],
    )


def _real_conversation_turns(started_at: datetime) -> list[InteractionTurn]:
    """Enough back-and-forth to clear `_had_a_real_conversation`'s turn-count
    floor - `make_session` defaults to this so every existing schedule/badge
    test keeps meaning "a real class happened" unless it opts into a thinner
    transcript via `transcript_history=`."""
    return [
        InteractionTurn(speaker="mariam", text="Hi! Ready to practice?", timestamp=started_at),
        InteractionTurn(speaker="student", text="Yes, let's talk.", timestamp=started_at),
        InteractionTurn(speaker="mariam", text="Great, tell me about your day.", timestamp=started_at),
        InteractionTurn(speaker="student", text="It was good, thanks.", timestamp=started_at),
    ]


def make_session(
    student: User,
    started_at: datetime,
    topic_id=None,
    report=None,
    state=SessionState.ENDED,
    duration=timedelta(minutes=10),
    transcript_history=None,
) -> LiveSession:
    ended_at = started_at + duration if state == SessionState.ENDED else None
    return LiveSession(
        student=student,
        student_id=student.id,
        topic_id=topic_id,
        state=state,
        started_at=started_at,
        ended_at=ended_at,
        report=report,
        transcript_history=(transcript_history if transcript_history is not None else _real_conversation_turns(started_at))
        if state == SessionState.ENDED
        else [],
    )


def make_report(overall_score: int, **overrides) -> SpeakingReport:
    defaults = dict(
        fluency_score=overall_score,
        vocabulary_score=overall_score,
        grammar_score=overall_score,
        complexity_score=overall_score,
        overall_score=overall_score,
        summary="ok",
        generated_at=TODAY,
    )
    defaults.update(overrides)
    return SpeakingReport(**defaults)


def make_topic(vocab_terms: int, level=Level.BEGINNER) -> TopicContent:
    return TopicContent(
        title="t",
        level=level,
        vocabulary=[VocabularyItem(term=f"w{i}", theme="x", example_sentence="x") for i in range(vocab_terms)],
    )


def make_submission(student: User, submitted_at: datetime) -> RecordingSubmission:
    return RecordingSubmission(
        student=student,
        student_id=student.id,
        kind=SubmissionKind.END_OF_WEEK_VIDEO,
        day=5,
        label="End-of-week check-in",
        media_url="http://x",
        cloudinary_public_id="x",
        duration_seconds=10,
        submitted_at=submitted_at,
    )


def build_service(sessions=None, submissions=None, topics=None, templates=None) -> ProgressService:
    return ProgressService(
        FakeSessionRepository(sessions or []),
        FakeSubmissionRepository(submissions or []),
        FakeTopicRepository(topics or []),
        FakeCalendarTemplateRepository(templates or []),
    )


async def test_no_activity_yields_zeroed_progress_and_no_badges():
    student = make_student()
    service = build_service()

    progress = await service.get_progress(student)

    assert progress.current_streak == 0
    assert progress.longest_streak == 0
    assert progress.total_active_days == 0
    assert all(not b.earned for b in progress.badges)


async def test_consecutive_days_of_sessions_build_a_streak_and_first_class_badge():
    student = make_student()
    sessions = [make_session(student, TODAY - timedelta(days=offset)) for offset in (2, 1, 0)]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert progress.current_streak == 3
    assert progress.longest_streak == 3
    assert progress.total_active_days == 3
    first_class = next(b for b in progress.badges if b.key == BadgeKey.FIRST_CLASS)
    assert first_class.earned
    assert first_class.earned_at == sessions[0].started_at


async def test_a_missed_day_breaks_the_current_streak_but_keeps_the_longest():
    student = make_student()
    sessions = [
        make_session(student, TODAY - timedelta(days=5)),
        make_session(student, TODAY - timedelta(days=4)),
        make_session(student, TODAY - timedelta(days=3)),
        # gap at day 2, then active again today
        make_session(student, TODAY),
    ]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert progress.current_streak == 1
    assert progress.longest_streak == 3


async def test_an_end_of_week_submission_counts_as_an_active_day_toward_the_streak():
    student = make_student()
    sessions = [make_session(student, TODAY - timedelta(days=1))]
    submissions = [make_submission(student, TODAY)]
    service = build_service(sessions=sessions, submissions=submissions)

    progress = await service.get_progress(student)

    assert progress.current_streak == 2


async def test_streak_badge_earns_at_seven_consecutive_days():
    student = make_student()
    sessions = [make_session(student, TODAY - timedelta(days=offset)) for offset in range(6, -1, -1)]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    streak_badge = next(b for b in progress.badges if b.key == BadgeKey.STREAK)
    assert streak_badge.earned
    assert progress.longest_streak == 7


async def test_top_scorer_badge_earns_on_a_ninety_plus_report():
    student = make_student()
    sessions = [
        make_session(student, TODAY - timedelta(days=1), report=make_report(70)),
        make_session(student, TODAY, report=make_report(92)),
    ]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    top_scorer = next(b for b in progress.badges if b.key == BadgeKey.TOP_SCORER)
    assert top_scorer.earned
    assert top_scorer.earned_at == sessions[1].started_at


async def test_top_scorer_badge_does_not_earn_below_the_threshold():
    student = make_student()
    sessions = [make_session(student, TODAY, report=make_report(89))]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert not next(b for b in progress.badges if b.key == BadgeKey.TOP_SCORER).earned


async def test_fast_improver_badge_earns_on_a_twenty_point_gain_within_the_window():
    student = make_student()
    sessions = [
        make_session(student, TODAY - timedelta(days=10), report=make_report(60)),
        make_session(student, TODAY, report=make_report(81)),
    ]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert next(b for b in progress.badges if b.key == BadgeKey.FAST_IMPROVER).earned


async def test_fast_improver_badge_does_not_earn_outside_the_thirty_day_window():
    student = make_student()
    sessions = [
        make_session(student, TODAY - timedelta(days=40), report=make_report(60)),
        make_session(student, TODAY, report=make_report(81)),
    ]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert not next(b for b in progress.badges if b.key == BadgeKey.FAST_IMPROVER).earned


async def test_vocab_master_badge_earns_once_cumulative_distinct_topic_vocabulary_crosses_threshold():
    student = make_student()
    topics = [make_topic(80), make_topic(80)]
    sessions = [
        make_session(student, TODAY - timedelta(days=1), topic_id=topics[0].id),
        make_session(student, TODAY, topic_id=topics[1].id),
    ]
    service = build_service(sessions=sessions, topics=topics)

    progress = await service.get_progress(student)

    vocab_badge = next(b for b in progress.badges if b.key == BadgeKey.VOCAB_MASTER)
    assert vocab_badge.earned
    assert vocab_badge.earned_at == sessions[1].started_at


async def test_vocab_master_badge_only_counts_each_topic_once_even_with_repeat_sessions():
    student = make_student()
    topic = make_topic(200)
    sessions = [
        make_session(student, TODAY - timedelta(days=1), topic_id=topic.id),
        make_session(student, TODAY, topic_id=topic.id),
    ]
    service = build_service(sessions=sessions, topics=[topic])

    progress = await service.get_progress(student)

    vocab_badge = next(b for b in progress.badges if b.key == BadgeKey.VOCAB_MASTER)
    assert vocab_badge.earned
    assert vocab_badge.earned_at == sessions[0].started_at


async def test_level_complete_badge_earns_once_every_template_topic_has_a_session():
    student = make_student(plan=PlanType.STANDARD, assigned_level=Level.BEGINNER)
    topics = [make_topic(1, level=Level.BEGINNER), make_topic(1, level=Level.BEGINNER)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    sessions = [
        make_session(student, TODAY - timedelta(days=1), topic_id=topics[0].id),
        make_session(student, TODAY, topic_id=topics[1].id),
    ]
    service = build_service(sessions=sessions, topics=topics, templates=[template])

    progress = await service.get_progress(student)

    level_badge = next(b for b in progress.badges if b.key == BadgeKey.LEVEL_COMPLETE)
    assert level_badge.earned
    assert level_badge.earned_at == sessions[1].started_at


async def test_level_complete_badge_does_not_earn_with_a_topic_left_untouched():
    student = make_student(plan=PlanType.STANDARD, assigned_level=Level.BEGINNER)
    topics = [make_topic(1, level=Level.BEGINNER), make_topic(1, level=Level.BEGINNER)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    sessions = [make_session(student, TODAY, topic_id=topics[0].id)]
    service = build_service(sessions=sessions, topics=topics, templates=[template])

    progress = await service.get_progress(student)

    assert not next(b for b in progress.badges if b.key == BadgeKey.LEVEL_COMPLETE).earned


async def test_level_complete_badge_does_not_earn_from_a_too_short_session():
    student = make_student(plan=PlanType.STANDARD, assigned_level=Level.BEGINNER)
    topics = [make_topic(1, level=Level.BEGINNER), make_topic(1, level=Level.BEGINNER)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    sessions = [
        make_session(student, TODAY - timedelta(days=1), topic_id=topics[0].id, duration=timedelta(seconds=5)),
        make_session(student, TODAY, topic_id=topics[1].id),
    ]
    service = build_service(sessions=sessions, topics=topics, templates=[template])

    progress = await service.get_progress(student)

    assert not next(b for b in progress.badges if b.key == BadgeKey.LEVEL_COMPLETE).earned


async def test_level_complete_badge_is_never_awarded_to_a_pro_plan_student():
    student = make_student(plan=PlanType.PRO, assigned_level=None, assigned_plan_template_id="tmpl-1")
    topic = make_topic(1, level=Level.BEGINNER)
    template = make_custom_template([(1, topic.id)])
    template.id = "tmpl-1"
    sessions = [make_session(student, TODAY, topic_id=topic.id)]
    service = build_service(sessions=sessions, topics=[topic], templates=[template])

    progress = await service.get_progress(student)

    assert not next(b for b in progress.badges if b.key == BadgeKey.LEVEL_COMPLETE).earned


async def test_only_ended_sessions_count_toward_activity():
    student = make_student()
    sessions = [make_session(student, TODAY, state=SessionState.CONNECTING)]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert progress.total_active_days == 0
    assert not next(b for b in progress.badges if b.key == BadgeKey.FIRST_CLASS).earned


async def test_challenge_day_is_zero_when_the_program_has_not_started():
    # No `program_start_date` at all - not `created_at` - is what "hasn't
    # started" means now; an account created weeks ago with no plan assigned
    # yet shouldn't read as already behind.
    never_assigned = make_student(program_start_date=None, created_at=TODAY - timedelta(days=60))
    service = build_service()

    progress = await service.get_progress(never_assigned)

    assert progress.challenge_day == 0
    assert progress.program_start_date is None
    assert all(not m.reached for m in progress.milestones)


async def test_challenge_day_is_anchored_to_the_teacher_set_start_date_and_capped_at_thirty():
    fresh_student = make_student(program_start_date=TODAY - timedelta(days=2))
    service = build_service()

    progress = await service.get_progress(fresh_student)

    assert progress.challenge_day == 3
    assert progress.challenge_total_days == 30
    assert [m.day for m in progress.milestones] == CHALLENGE_MILESTONE_DAYS
    assert all(not m.reached for m in progress.milestones)

    long_enrolled_student = make_student(program_start_date=TODAY - timedelta(days=90))
    progress = await service.get_progress(long_enrolled_student)
    assert progress.challenge_day == 30
    assert all(m.reached for m in progress.milestones)


async def test_challenge_day_is_zero_when_the_start_date_is_still_in_the_future():
    not_yet = make_student(program_start_date=TODAY + timedelta(days=5))
    service = build_service()

    progress = await service.get_progress(not_yet)

    assert progress.challenge_day == 0
    assert all(not m.reached for m in progress.milestones)


async def test_last_7_days_active_marks_exactly_the_days_with_activity_oldest_first():
    student = make_student()
    sessions = [
        make_session(student, TODAY - timedelta(days=5)),
        make_session(student, TODAY - timedelta(days=1)),
        make_session(student, TODAY),
    ]
    service = build_service(sessions=sessions)

    progress = await service.get_progress(student)

    assert len(progress.last_7_days_active) == 7
    assert progress.last_7_days_active == [False, True, False, False, False, True, True]


def _slot(schedule, day):
    return next(s for s in schedule.lesson_days if s.day == day)


def _topic_ids(schedule, day) -> list[str]:
    return [t.topic_id for t in _slot(schedule, day).topics]


def _states(schedule, day) -> list[LessonDayState]:
    return [t.state for t in _slot(schedule, day).topics]


async def test_lesson_schedule_assigns_the_templates_first_day_entry_to_day_one():
    student = make_student()  # program starts today -> today is day 1
    topic = make_topic(5)
    template = make_standard_template([(1, topic.id)])
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topic.id]
    assert _states(schedule, 1) == [LessonDayState.CURRENT]
    assert schedule.pending_count == 0
    # Every later day just previews the still-unresolved backlog head (day
    # 1's own lesson) - the preview never assumes it gets cleared on time.
    assert _states(schedule, 2) == [LessonDayState.LOCKED]
    assert _topic_ids(schedule, 2) == [topic.id]
    assert _states(schedule, 3) == [LessonDayState.LOCKED]
    assert _topic_ids(schedule, 3) == [topic.id]


async def test_a_template_entry_lands_exactly_on_its_assigned_day():
    # Anchor "today" to day 3 without completing anything.
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topic = make_topic(5)
    template = make_standard_template([(3, topic.id)])  # nothing scheduled for days 1-2
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == []
    assert _topic_ids(schedule, 2) == []
    assert _topic_ids(schedule, 3) == [topic.id]
    assert _states(schedule, 3) == [LessonDayState.CURRENT]
    assert schedule.pending_count == 0


async def test_end_of_week_check_in_days_never_get_a_lesson_slot():
    student = make_student()
    template = make_standard_template([(1, make_topic(5).id)])
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert {5, 12, 19, 26}.isdisjoint({s.day for s in schedule.lesson_days})


async def test_an_uncompleted_day_rolls_the_same_topic_into_the_next_day():
    # A start date 2 days ago anchors "today" to day 3 without completing
    # either topic.
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topics = [make_topic(5), make_topic(5)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topics[0].id]
    assert _states(schedule, 1) == [LessonDayState.MISSED]
    # Day 2 is also in the past by now, so its own topic joins day 1's
    # still-unresolved one in the same missed batch rather than waiting for
    # day 1 to clear first - both are owed as of day 2.
    assert _topic_ids(schedule, 2) == [topics[0].id, topics[1].id]
    assert _states(schedule, 2) == [LessonDayState.MISSED, LessonDayState.MISSED]
    assert _topic_ids(schedule, 3) == [topics[0].id, topics[1].id]
    assert _states(schedule, 3) == [LessonDayState.CURRENT, LessonDayState.CURRENT]
    assert schedule.pending_count == 3
    # Future days preview the whole backlog, not just its head.
    assert _topic_ids(schedule, 6) == [topics[0].id, topics[1].id]
    assert _states(schedule, 6) == [LessonDayState.LOCKED, LessonDayState.LOCKED]


async def test_completing_a_day_advances_to_the_next_days_own_topic():
    # A start date 2 days ago anchors "today" to day 3.
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topics = [make_topic(5), make_topic(5)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    sessions = [make_session(student, TODAY - timedelta(days=2), topic_id=topics[0].id)]
    service = build_service(sessions=sessions, templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topics[0].id]
    assert _states(schedule, 1) == [LessonDayState.DONE]
    assert _topic_ids(schedule, 2) == [topics[1].id]
    assert _states(schedule, 2) == [LessonDayState.MISSED]
    assert _topic_ids(schedule, 3) == [topics[1].id]
    assert _states(schedule, 3) == [LessonDayState.CURRENT]
    assert schedule.pending_count == 1


async def test_a_session_that_ended_almost_immediately_does_not_complete_the_topic():
    """Connecting and hanging up right away reaches ENDED same as a real
    class - `_had_a_real_conversation`'s duration floor is what tells them
    apart, not the state machine."""
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topic = make_topic(5)
    template = make_standard_template([(1, topic.id)])
    sessions = [make_session(student, TODAY - timedelta(days=2), topic_id=topic.id, duration=timedelta(seconds=5))]
    service = build_service(sessions=sessions, templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _states(schedule, 1) == [LessonDayState.MISSED]  # not DONE - too short to count
    assert schedule.pending_count == 2  # missed on both day 1 and day 2, same as any uncompleted topic


async def test_a_session_with_no_real_back_and_forth_does_not_complete_the_topic():
    """Long enough to pass the duration floor, but if the student (or
    Mariam) never actually said more than a token amount, it's an idle call,
    not a completed lesson - and this must not be judged by transcript
    wording, only by how many turns each side produced."""
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topic = make_topic(5)
    template = make_standard_template([(1, topic.id)])
    thin_transcript = [InteractionTurn(speaker="mariam", text="Hi!", timestamp=TODAY)]
    sessions = [
        make_session(student, TODAY - timedelta(days=2), topic_id=topic.id, transcript_history=thin_transcript)
    ]
    service = build_service(sessions=sessions, templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _states(schedule, 1) == [LessonDayState.MISSED]
    assert schedule.pending_count == 2  # missed on both day 1 and day 2, same as any uncompleted topic


async def test_a_later_days_own_topic_surfaces_once_the_earlier_backlog_clears():
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topics = [make_topic(5), make_topic(5)]
    template = make_standard_template([(1, topics[0].id), (2, topics[1].id)])
    sessions = [make_session(student, TODAY - timedelta(days=2), topic_id=topics[0].id)]
    service = build_service(sessions=sessions, templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topics[0].id]
    assert _states(schedule, 1) == [LessonDayState.DONE]
    assert _topic_ids(schedule, 2) == [topics[1].id]
    assert _states(schedule, 2) == [LessonDayState.MISSED]
    assert _topic_ids(schedule, 3) == [topics[1].id]
    assert _states(schedule, 3) == [LessonDayState.CURRENT]
    assert schedule.pending_count == 1


async def test_a_multi_course_day_tracks_each_course_independently():
    """Two courses on the same day: completing one shouldn't complete or
    roll over the other - each is tracked and rolled forward on its own.
    """
    student = make_student(program_start_date=TODAY - timedelta(days=1))  # anchor "today" to day 2
    topics = [make_topic(5), make_topic(5)]
    template = make_standard_template([(1, [topics[0].id, topics[1].id])])
    sessions = [make_session(student, TODAY - timedelta(days=1), topic_id=topics[0].id)]
    service = build_service(sessions=sessions, templates=[template])

    schedule = await service.get_lesson_schedule(student)

    day1_by_topic = {t.topic_id: t.state for t in _slot(schedule, 1).topics}
    assert day1_by_topic[topics[0].id] == LessonDayState.DONE
    assert day1_by_topic[topics[1].id] == LessonDayState.MISSED
    # Only the unfinished course rolls into day 2 - the completed one drops
    # out of the backlog entirely.
    assert _topic_ids(schedule, 2) == [topics[1].id]
    assert _states(schedule, 2) == [LessonDayState.CURRENT]
    assert schedule.pending_count == 1


async def test_a_multi_course_days_new_topics_queue_in_behind_an_existing_backlog():
    """Day 2 adds two of its own courses behind whatever day 1 left owed -
    the backlog just keeps growing, not capped at one course wide."""
    student = make_student(program_start_date=TODAY - timedelta(days=1))  # anchor "today" to day 2
    topics = [make_topic(5), make_topic(5), make_topic(5)]
    template = make_standard_template([(1, topics[0].id), (2, [topics[1].id, topics[2].id])])
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topics[0].id]
    assert _states(schedule, 1) == [LessonDayState.MISSED]
    assert _topic_ids(schedule, 2) == [topics[0].id, topics[1].id, topics[2].id]
    assert _states(schedule, 2) == [LessonDayState.CURRENT, LessonDayState.CURRENT, LessonDayState.CURRENT]
    assert schedule.pending_count == 1


async def test_lesson_schedule_is_empty_when_the_student_has_no_level_assigned():
    student = make_student(plan=PlanType.STANDARD, assigned_level=None)
    service = build_service(templates=[make_standard_template([(1, make_topic(5).id)])])

    schedule = await service.get_lesson_schedule(student)

    assert schedule.lesson_days == []
    assert schedule.pending_count == 0
    assert schedule.has_template is False


async def test_lesson_schedule_is_empty_when_the_levels_template_is_not_active():
    student = make_student()
    inactive = make_standard_template([(1, make_topic(5).id)], is_active=False)
    service = build_service(templates=[inactive])

    schedule = await service.get_lesson_schedule(student)

    assert schedule.lesson_days == []
    assert schedule.has_template is False


async def test_a_pro_student_follows_their_assigned_custom_template():
    topic = make_topic(5)
    template = make_custom_template([(1, topic.id)])
    template.id = "tmpl-1"
    student = make_student(plan=PlanType.PRO, assigned_level=None, assigned_plan_template_id="tmpl-1")
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)

    assert _topic_ids(schedule, 1) == [topic.id]
    assert schedule.has_template is True


async def test_lesson_schedule_is_empty_for_a_pro_student_with_no_template_assigned():
    student = make_student(plan=PlanType.PRO, assigned_level=None, assigned_plan_template_id=None)
    service = build_service()

    schedule = await service.get_lesson_schedule(student)

    assert schedule.lesson_days == []
    assert schedule.has_template is False


async def test_get_topic_session_minutes_splits_the_daily_budget_evenly():
    student = make_student()  # 15-minute default daily budget
    topics = [make_topic(1), make_topic(1)]
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=ADMIN_ID, level=Level.BEGINNER, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topics[0].id, topics[1].id])],
    )
    service = build_service(templates=[template])

    assert await service.get_topic_session_minutes(student, topics[0].id) == 7
    assert await service.get_topic_session_minutes(student, topics[1].id) == 7


async def test_get_topic_session_minutes_takes_a_custom_override_as_is():
    student = make_student()
    topics = [make_topic(1), make_topic(1)]
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=ADMIN_ID, level=Level.BEGINNER, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=[topics[0].id, topics[1].id], custom_minutes={topics[0].id: 3})],
    )
    service = build_service(templates=[template])

    assert await service.get_topic_session_minutes(student, topics[0].id) == 3
    # The other course's automatic share shrinks to whatever's left, not the
    # daily budget split evenly across both.
    assert await service.get_topic_session_minutes(student, topics[1].id) == 12


async def test_get_topic_session_minutes_falls_back_to_the_daily_budget_when_the_topic_is_not_scheduled():
    student = make_student()
    template = CalendarTemplate(
        name="T", kind=TemplateKind.STANDARD, owner_admin_id=ADMIN_ID, level=Level.BEGINNER, is_active=True,
        days=[CalendarDayEntry(day=1, topic_ids=["some-other-topic"])],
    )
    service = build_service(templates=[template])

    assert await service.get_topic_session_minutes(student, "not-on-the-calendar") == 15


async def test_get_topic_session_minutes_falls_back_to_the_daily_budget_with_no_template():
    student = make_student()
    service = build_service()

    assert await service.get_topic_session_minutes(student, "any-topic") == 15


async def test_get_alerts_reports_pending_rollovers_matching_the_schedule():
    # Anchor "today" to day 3 without completing anything.
    student = make_student(program_start_date=TODAY - timedelta(days=2))
    topic = make_topic(5)
    template = make_standard_template([(1, topic.id)])
    service = build_service(templates=[template])

    schedule = await service.get_lesson_schedule(student)
    alerts = await service.get_alerts(student)

    assert alerts.pending_rollover_count == schedule.pending_count
    assert alerts.pending_rollover_count == 2
    assert alerts.has_alerts is True


async def test_get_alerts_reports_a_missed_end_of_week_submission():
    # Anchor "today" to day 7 (past day 5, the first end-of-week check-in)
    # without ever submitting a day-5 check-in.
    student = make_student(program_start_date=TODAY - timedelta(days=6))
    service = build_service()

    alerts = await service.get_alerts(student)

    assert alerts.missed_submission_days == [5]
    assert alerts.has_alerts is True


async def test_get_alerts_does_not_flag_a_submitted_end_of_week_day():
    student = make_student(program_start_date=TODAY - timedelta(days=6))
    submissions = [make_submission(student, TODAY - timedelta(days=1))]  # day=5, per make_submission
    service = build_service(submissions=submissions)

    alerts = await service.get_alerts(student)

    assert alerts.missed_submission_days == []


async def test_get_alerts_does_not_flag_a_future_end_of_week_day():
    student = make_student()  # program starts today -> today = day 1
    service = build_service()

    alerts = await service.get_alerts(student)

    assert alerts.missed_submission_days == []
    assert alerts.has_alerts is False


async def test_get_alerts_for_students_matches_each_students_own_get_alerts():
    standard_topic = make_topic(5)
    standard_template = make_standard_template([(1, standard_topic.id)])
    standard_student = make_student(email="standard@example.com", program_start_date=TODAY - timedelta(days=6))

    pro_topic = make_topic(5)
    pro_template = make_custom_template([(1, pro_topic.id)])
    pro_template.id = "tmpl-pro"
    pro_student = make_student(
        email="pro@example.com",
        plan=PlanType.PRO,
        assigned_level=None,
        assigned_plan_template_id="tmpl-pro",
        program_start_date=TODAY - timedelta(days=1),
    )

    service = build_service(templates=[standard_template, pro_template])

    bulk = await service.get_alerts_for_students([standard_student, pro_student])

    assert bulk[standard_student.id] == await service.get_alerts(standard_student)
    assert bulk[pro_student.id] == await service.get_alerts(pro_student)


async def test_get_alerts_for_students_with_no_students_returns_an_empty_dict():
    service = build_service()
    assert await service.get_alerts_for_students([]) == {}
