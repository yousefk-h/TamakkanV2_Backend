"""`resolve_topic_statuses`'s precedence rule and `requester_may_open`'s
role/status gate - the two pieces `app.core.entitlement` is built from, in
isolation from `ProgressService`'s own schedule-building logic (see
`tests/services/test_progress_service.py` for that).
"""

from app.core.entitlement import requester_may_open
from app.models.user import PlanType, Role, User
from app.schema.challenge import ChallengeCalendar, LessonDayState, LessonDaySlot, TopicSlot
from app.services.progress_service import resolve_topic_statuses


def make_student(**overrides) -> User:
    defaults = dict(
        email="student@example.com",
        password_hash="x",
        full_name="Student",
        role=Role.STUDENT,
        plan=PlanType.STANDARD,
    )
    defaults.update(overrides)
    return User(**defaults)


class FakeProgressService:
    def __init__(self, statuses: dict[str, LessonDayState]):
        self.statuses = statuses

    async def get_topic_statuses(self, student):
        return self.statuses


# --- resolve_topic_statuses ---------------------------------------------------


def _calendar(*day_slots: LessonDaySlot) -> ChallengeCalendar:
    return ChallengeCalendar(lesson_days=list(day_slots), pending_count=0)


def test_a_topic_seen_only_once_keeps_that_status():
    schedule = _calendar(LessonDaySlot(day=1, topics=[TopicSlot(topic_id="t1", state=LessonDayState.LOCKED)]))
    assert resolve_topic_statuses(schedule) == {"t1": LessonDayState.LOCKED}


def test_done_wins_over_every_other_status_it_appears_with():
    schedule = _calendar(
        LessonDaySlot(day=1, topics=[TopicSlot(topic_id="t1", state=LessonDayState.DONE)]),
        LessonDaySlot(day=2, topics=[TopicSlot(topic_id="t1", state=LessonDayState.CURRENT)]),
    )
    assert resolve_topic_statuses(schedule) == {"t1": LessonDayState.DONE}


def test_current_beats_missed():
    schedule = _calendar(
        LessonDaySlot(day=1, topics=[TopicSlot(topic_id="t1", state=LessonDayState.MISSED)]),
        LessonDaySlot(day=2, topics=[TopicSlot(topic_id="t1", state=LessonDayState.CURRENT)]),
    )
    assert resolve_topic_statuses(schedule) == {"t1": LessonDayState.CURRENT}


def test_missed_beats_locked():
    schedule = _calendar(
        LessonDaySlot(day=1, topics=[TopicSlot(topic_id="t1", state=LessonDayState.MISSED)]),
        LessonDaySlot(day=2, topics=[TopicSlot(topic_id="t1", state=LessonDayState.LOCKED)]),
    )
    assert resolve_topic_statuses(schedule) == {"t1": LessonDayState.MISSED}


def test_each_topic_resolves_independently():
    schedule = _calendar(
        LessonDaySlot(
            day=1,
            topics=[TopicSlot(topic_id="t1", state=LessonDayState.DONE), TopicSlot(topic_id="t2", state=LessonDayState.MISSED)],
        ),
        LessonDaySlot(day=2, topics=[TopicSlot(topic_id="t2", state=LessonDayState.CURRENT)]),
    )
    assert resolve_topic_statuses(schedule) == {"t1": LessonDayState.DONE, "t2": LessonDayState.CURRENT}


def test_an_empty_schedule_resolves_to_no_statuses_at_all():
    assert resolve_topic_statuses(_calendar()) == {}


# --- requester_may_open --------------------------------------------------------


async def test_an_administrator_may_always_open_any_topic():
    admin = User(email="a@example.com", password_hash="x", full_name="Admin", role=Role.ADMINISTRATOR)
    service = FakeProgressService({})  # never consulted for a non-Student

    assert await requester_may_open(admin, "any-topic", service) is True


async def test_root_may_always_open_any_topic():
    root = User(email="r@example.com", password_hash="x", full_name="Root", role=Role.ROOT)
    service = FakeProgressService({})

    assert await requester_may_open(root, "any-topic", service) is True


async def test_a_student_may_open_a_current_topic():
    student = make_student()
    service = FakeProgressService({"t1": LessonDayState.CURRENT})

    assert await requester_may_open(student, "t1", service) is True


async def test_a_student_may_open_a_done_topic():
    student = make_student()
    service = FakeProgressService({"t1": LessonDayState.DONE})

    assert await requester_may_open(student, "t1", service) is True


async def test_a_student_may_open_a_missed_rolled_over_topic():
    student = make_student()
    service = FakeProgressService({"t1": LessonDayState.MISSED})

    assert await requester_may_open(student, "t1", service) is True


async def test_a_student_may_not_open_a_locked_topic():
    student = make_student()
    service = FakeProgressService({"t1": LessonDayState.LOCKED})

    assert await requester_may_open(student, "t1", service) is False


async def test_a_student_may_not_open_a_topic_that_is_not_on_their_calendar_at_all():
    student = make_student()
    service = FakeProgressService({"some-other-topic": LessonDayState.CURRENT})

    assert await requester_may_open(student, "t1", service) is False
