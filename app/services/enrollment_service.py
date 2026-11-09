"""The student-facing read side of Enrollment: "My Learning" lists every
active Enrollment a student holds (legacy-backfilled single enrollment
included) and, per enrollment, a read-only schedule preview resolved through
`CalendarTemplateRepository.get_effective_schedule` - the one deterministic
answer for which calendar an enrollment actually follows.

`LiveSession` and `ExerciseAttempt` now carry `enrollment_id` (see
`resolve_context` below) once a caller resolves one, so completion/progress
CAN be attributed to a specific enrollment going forward. Older/legacy rows
(created before this, or by an account with no Enrollment at all) carry no
`enrollment_id` and are read through the pre-existing scalar-`program_id`
path untouched - see `ProgressService.get_topic_statuses_for_context`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.calendar_template import CalendarTemplate
from app.models.cohort import Cohort
from app.models.enrollment import Enrollment, EnrollmentStatus
from app.models.program import Program
from app.models.topic_content import TopicContent
from app.models.user import User
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.schema.challenge import LessonDayState
from app.schema.enrollment import EnrollmentPublic, EnrollmentSchedule, EnrollmentScheduleDay
from app.services.program_service import ProgramService, get_program_service
from app.utils.time import utcnow


@dataclass
class EnrollmentContext:
    """What a caller (session start, exercise submission, topic read) should
    actually resolve content/scheduling against. `enrollment is None` means
    "legacy path" - `program_id`/`program_level_id` came straight off `User`,
    exactly as every pre-existing caller already behaved, so nothing new is
    ever forced onto an account that has no Enrollment at all."""

    enrollment: Optional[Enrollment]
    program_id: Optional[str]
    program_level_id: Optional[str]
    cohort_id: Optional[str]
    # True when a pre-existing record with no `enrollment_id` at all (written
    # before this Enrollment carried its own records, or by an account that
    # had none yet) can be safely counted as belonging to THIS context -
    # i.e. no sibling active Enrollment shares the same
    # (program_id, program_level_id), so there is nothing else it could
    # plausibly mean. `False` when a student holds two enrollments in the
    # same Program/stage (e.g. two Cohorts) - an old unattributed record is
    # then genuinely ambiguous between them and must stay unattributed to
    # either, never guessed. Always `True` on the pure legacy path
    # (`enrollment is None`) - that is the exact pre-existing behavior.
    legacy_attributable: bool = True


class EnrollmentService:
    def __init__(self, calendar_template_repository: CalendarTemplateRepository, program_service: ProgramService) -> None:
        self.calendar_template_repository = calendar_template_repository
        self.program_service = program_service

    async def list_for_student(self, student: User) -> list[EnrollmentPublic]:
        enrollments = await Enrollment.find({
            "student_id": student.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }).sort("-joined_at").to_list()
        if not enrollments:
            return []
        program_ids = {e.program_id for e in enrollments}
        programs = {p.id: p for p in await Program.find({"_id": {"$in": list(program_ids)}}).to_list()}
        cohort_ids = {e.cohort_id for e in enrollments if e.cohort_id}
        cohorts = {c.id: c for c in await Cohort.find({"_id": {"$in": list(cohort_ids)}}).to_list()} if cohort_ids else {}

        result = []
        for enrollment in enrollments:
            program = programs.get(enrollment.program_id)
            stage_name = None
            if program is not None:
                stage = next((lvl for lvl in self.program_service.effective_levels(program) if lvl.id == enrollment.program_level_id), None)
                stage_name = stage.name if stage else None
            cohort = cohorts.get(enrollment.cohort_id) if enrollment.cohort_id else None
            result.append(EnrollmentPublic.from_enrollment(
                enrollment,
                program_name=program.name if program else None,
                program_level_name=stage_name,
                cohort_name=cohort.name if cohort else None,
            ))
        return result

    async def resolve_context(self, student: User, enrollment_id: Optional[str]) -> EnrollmentContext:
        """The one place that decides which enrollment a content/session
        action resolves against - see the module docstring. Never guesses
        among several active enrollments: a caller who doesn't say which one
        they mean gets an explicit error, not a silently-picked first one."""
        if enrollment_id:
            enrollment = await Enrollment.find_one({"_id": enrollment_id, "is_deleted": False})
            if enrollment is None or enrollment.student_id != student.id:
                raise ForbiddenException("This enrollment does not belong to you")
            if enrollment.status != EnrollmentStatus.ACTIVE:
                raise UnprocessableException("This enrollment is no longer active")
            return await self._with_legacy_attributable(enrollment)
        active = await Enrollment.find({
            "student_id": student.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }).to_list()
        if not active:
            # No Enrollment ever created for this student (never migrated, or
            # a center that hasn't adopted Cohorts) - the exact legacy path
            # every pre-existing caller already used.
            return EnrollmentContext(None, student.program_id, student.program_level_id, None)
        if len(active) == 1:
            return await self._with_legacy_attributable(active[0])
        raise UnprocessableException(
            "You have more than one active enrollment - specify which one with enrollment_id"
        )

    async def _with_legacy_attributable(self, enrollment: Enrollment) -> EnrollmentContext:
        sibling_count = await Enrollment.find({
            "student_id": enrollment.student_id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
            "program_id": enrollment.program_id, "program_level_id": enrollment.program_level_id,
            "_id": {"$ne": enrollment.id},
        }).count()
        return EnrollmentContext(
            enrollment, enrollment.program_id, enrollment.program_level_id, enrollment.cohort_id,
            legacy_attributable=(sibling_count == 0),
        )

    async def _get_own_enrollment(self, enrollment_id: str, student: User) -> Enrollment:
        enrollment = await Enrollment.find_one({"_id": enrollment_id, "is_deleted": False})
        if enrollment is None:
            raise NotFoundException("Enrollment not found")
        if enrollment.student_id != student.id:
            raise ForbiddenException("This enrollment belongs to a different student")
        return enrollment

    async def get_effective_schedule(self, enrollment_id: str, student: User) -> Optional[CalendarTemplate]:
        enrollment = await self._get_own_enrollment(enrollment_id, student)
        return await self.calendar_template_repository.get_effective_schedule(
            enrollment.program_id, enrollment.program_level_id, enrollment.cohort_id
        )

    async def get_schedule_preview(self, enrollment_id: str, student: User) -> EnrollmentSchedule:
        enrollment = await self._get_own_enrollment(enrollment_id, student)
        template = await self.calendar_template_repository.get_effective_schedule(
            enrollment.program_id, enrollment.program_level_id, enrollment.cohort_id
        )
        if template is None:
            return EnrollmentSchedule(has_template=False)
        # Reuse the canonical, enrollment-aware progress resolver.  It is
        # deliberately the authority for done/current/missed/locked rather
        # than having this preview independently infer completion from dates.
        from app.services.progress_service import get_progress_service

        context = await self._with_legacy_attributable(enrollment)
        topic_states = await get_progress_service().get_topic_statuses_for_context(student, context)
        topic_ids = {topic_id for entry in template.days for topic_id in entry.topic_ids}
        topic_titles = {
            topic.id: topic.title
            for topic in await TopicContent.find({"_id": {"$in": list(topic_ids)}}).to_list()
        } if topic_ids else {}
        cohort = await Cohort.find_one({"_id": enrollment.cohort_id}) if enrollment.cohort_id else None
        anchor = self.schedule_anchor_date(enrollment, cohort) or utcnow()
        current_day = max(0, min(template.duration_days, (utcnow().date() - anchor.date()).days + 1))
        days = [
            EnrollmentScheduleDay(
                day=entry.day,
                topic_ids=entry.topic_ids,
                locked=all(topic_states.get(topic_id, LessonDayState.LOCKED) == LessonDayState.LOCKED for topic_id in entry.topic_ids) if entry.topic_ids else entry.day > current_day,
                topic_states={topic_id: topic_states[topic_id] for topic_id in entry.topic_ids if topic_id in topic_states},
                topic_titles={topic_id: topic_titles[topic_id] for topic_id in entry.topic_ids if topic_id in topic_titles},
            )
            for entry in sorted(template.days, key=lambda e: e.day)
        ]
        return EnrollmentSchedule(
            has_template=True,
            total_days=template.duration_days,
            rest_days=template.rest_days,
            current_day=current_day,
            days=days,
        )

    @staticmethod
    def schedule_anchor_date(enrollment: Enrollment, cohort: Optional[Cohort]) -> Optional[datetime]:
        """Day 1 for this enrollment's lock/unlock preview. A Cohort enrollment
        with `late_join_mode=day_one` anchors to when the student actually
        joined, not the Cohort's own start date, so a late joiner still gets
        the full sequence from day one on their own clock; `current_day`
        (the default) anchors to the Cohort's start date so the student lands
        on whatever day the rest of the Cohort is already on."""
        if cohort is not None and enrollment.late_join_mode and enrollment.late_join_mode.value == "day_one":
            return enrollment.joined_at
        if cohort is not None:
            return cohort.start_date
        return enrollment.joined_at


def get_enrollment_service() -> EnrollmentService:
    return EnrollmentService(CalendarTemplateRepository(), get_program_service())
