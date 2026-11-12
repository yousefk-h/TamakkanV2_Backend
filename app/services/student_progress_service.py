"""The complete, deterministic Student Learning Journey (not just AI
practice) a Teacher/Admin sees on a student's profile - assembled from
existing Enrollment, Calendar-schedule, ExerciseAttempt, LiveSession, and
Homework records. Nothing here calls an AI provider; every value is a
database read or a plain aggregation over one. See
`app.schema.student_progress`'s own module docstring for the "never
invent historical data" rule this follows.
"""

from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.enrollment import Enrollment, EnrollmentStatus
from app.models.exercise_attempt import ExerciseAttempt
from app.models.homework import Homework
from app.models.live_session import LiveSession
from app.models.program import Program
from app.models.topic_content import TopicContent
from app.models.user import Role, User
from app.schema.enrollment import EnrollmentPublic
from app.schema.student_progress import (
    AiSessionSummary,
    EnrollmentJourney,
    ExerciseAttemptHistoryItem,
    HomeworkSummary,
    LessonExerciseSummary,
    LessonProgressSummary,
    StudentLearningJourney,
)
from app.services.cohort_service import CohortService, accessible_stage_ids
from app.services.enrollment_service import EnrollmentService
from app.services.exercise_attempt_service import correct_answer_display


class StudentProgressService:
    def __init__(self, cohort_service: CohortService, enrollment_service: EnrollmentService) -> None:
        self.cohort_service = cohort_service
        self.enrollment_service = enrollment_service

    async def _authorized_enrollment_ids(self, student: User, caller: User) -> Optional[set[str]]:
        """`None` means "every enrollment" (Center Admin/Root, or a Teacher
        who personally created this student - the existing, narrower
        ownership relationship `UserService.get_own_student` already uses
        elsewhere). A `set[str]` restricts the journey to just those
        Enrollment ids - a stage-scoped additional teacher must not see a
        student's progress in a stage/Cohort they aren't assigned to, even
        for a student they otherwise have some reach into."""
        if caller.role in (Role.ADMINISTRATOR, Role.ROOT):
            return None
        if caller.role != Role.TEACHER:
            raise ForbiddenException("You are not authorized to view this student's progress")
        if student.created_by_id == caller.id:
            return None
        cohorts = await self.cohort_service.list_for_teacher(caller)
        stage_map = {c.id: set(accessible_stage_ids(c, caller)) for c in cohorts}
        if not stage_map:
            raise ForbiddenException("You are not authorized to view this student's progress")
        enrollments = await Enrollment.find({
            "student_id": student.id, "is_deleted": False, "cohort_id": {"$in": list(stage_map)},
        }).to_list()
        visible = {e.id for e in enrollments if e.program_level_id in stage_map.get(e.cohort_id, set())}
        if not visible:
            raise ForbiddenException("You are not authorized to view this student's progress")
        return visible

    async def get_learning_journey(self, student_id: str, caller: User) -> StudentLearningJourney:
        student = await User.find_one({"_id": student_id, "is_deleted": False, "role": Role.STUDENT.value})
        if student is None:
            raise NotFoundException("Student not found")
        visible_ids = await self._authorized_enrollment_ids(student, caller)

        enrollments = await Enrollment.find({"student_id": student_id, "is_deleted": False}).sort("-joined_at").to_list()
        if visible_ids is not None:
            enrollments = [e for e in enrollments if e.id in visible_ids]

        programs = await Program.find({"_id": {"$in": list({e.program_id for e in enrollments})}}).to_list()
        programs_by_id = {p.id: p for p in programs}
        cohort_ids = [e.cohort_id for e in enrollments if e.cohort_id]
        cohorts_by_id = {}
        if cohort_ids:
            from app.models.cohort import Cohort
            cohorts_by_id = {c.id: c for c in await Cohort.find({"_id": {"$in": cohort_ids}}).to_list()}

        enrollment_journeys: list[EnrollmentJourney] = []
        for enrollment in enrollments:
            program = programs_by_id.get(enrollment.program_id)
            cohort = cohorts_by_id.get(enrollment.cohort_id) if enrollment.cohort_id else None
            stage_name = None
            if program is not None:
                stage = next((lvl for lvl in self.cohort_service.program_service.effective_levels(program) if lvl.id == enrollment.program_level_id), None)
                stage_name = stage.name if stage else None
            enrollment_public = EnrollmentPublic.from_enrollment(
                enrollment, program_name=program.name if program else None,
                program_level_name=stage_name, cohort_name=cohort.name if cohort else None,
            )

            schedule = await self.enrollment_service.get_schedule_preview(enrollment.id, student)
            lessons: list[LessonProgressSummary] = []
            if schedule.has_template:
                seen_topics: set[str] = set()
                for day in schedule.days:
                    for topic_id in day.topic_ids:
                        if topic_id in seen_topics:
                            continue
                        seen_topics.add(topic_id)
                        lessons.append(await self._lesson_summary(
                            student_id, enrollment.id, topic_id, day.topic_titles.get(topic_id, topic_id),
                            day.topic_states.get(topic_id),
                        ))
            enrollment_journeys.append(EnrollmentJourney(
                enrollment=enrollment_public, has_schedule=schedule.has_template,
                total_days=schedule.total_days, current_day=schedule.current_day, lessons=lessons,
            ))

        enrollment_id_filter = list(visible_ids) if visible_ids is not None else None
        session_query: dict = {"student_id": student_id}
        if enrollment_id_filter is not None:
            session_query["enrollment_id"] = {"$in": enrollment_id_filter}
        sessions = await LiveSession.find(session_query).sort("-started_at").limit(100).to_list()
        ai_sessions = [
            AiSessionSummary(
                id=s.id, topic_id=s.topic_id, enrollment_id=s.enrollment_id,
                mode=s.mode.value, state=s.state.value, started_at=s.started_at, ended_at=s.ended_at,
                duration_minutes=round((s.ended_at - s.started_at).total_seconds() / 60, 1) if s.ended_at else None,
                has_report=s.report is not None,
            )
            for s in sessions
        ]
        topic_ids_in_sessions = {s.topic_id for s in sessions if s.topic_id}
        if topic_ids_in_sessions:
            from app.models.topic_content import TopicContent
            titles = {t.id: t.title for t in await TopicContent.find({"_id": {"$in": list(topic_ids_in_sessions)}}).to_list()}
            for summary in ai_sessions:
                if summary.topic_id:
                    summary.topic_title = titles.get(summary.topic_id)

        homework_rows = await Homework.find({"participant_ids": student_id}).sort("-due_at").limit(100).to_list()
        if visible_ids is not None:
            # Scope homework the same way sessions/lessons are scoped above -
            # only Cohorts this caller's own visible enrollments cover.
            allowed_cohort_ids = {e.cohort_id for e in enrollments}
            homework_rows = [h for h in homework_rows if h.cohort_id is None or h.cohort_id in allowed_cohort_ids]
        homework = [
            HomeworkSummary(id=h.id, title=h.title, kind=h.kind.value, due_at=h.due_at, topic_id=h.topic_id)
            for h in homework_rows
        ]

        return StudentLearningJourney(
            student_id=student.id, full_name=student.full_name, email=student.email, is_active=student.is_active,
            enrollments=enrollment_journeys, ai_sessions=ai_sessions, homework=homework,
        )

    async def _lesson_summary(
        self, student_id: str, enrollment_id: str, topic_id: str, topic_title: str, status,
    ) -> LessonProgressSummary:
        attempts = await ExerciseAttempt.find({
            "student_id": student_id, "enrollment_id": enrollment_id, "topic_id": topic_id,
        }).sort("-created_at").to_list()
        by_index: dict[int, list[ExerciseAttempt]] = {}
        for attempt in attempts:
            by_index.setdefault(attempt.exercise_index, []).append(attempt)

        exercises: list[LessonExerciseSummary] = []
        graded_count = 0
        correct_count = 0
        for index, rows in sorted(by_index.items()):
            latest = rows[0]  # already sorted newest-first
            best_score = max((r.score for r in rows if r.score is not None), default=None)
            exercises.append(LessonExerciseSummary(
                exercise_index=index, kind=latest.exercise_kind, prompt=latest.exercise_prompt,
                attempt_count=len(rows), latest_correct=latest.correct, latest_score=latest.score,
                latest_submitted_at=latest.created_at, latest_reviewed=latest.reviewed, best_score=best_score,
            ))
            if latest.correct is not None:
                graded_count += 1
                if latest.correct:
                    correct_count += 1

        return LessonProgressSummary(
            topic_id=topic_id, topic_title=topic_title, status=status, exercises=exercises,
            graded_exercise_count=graded_count, correct_exercise_count=correct_count,
        )

    async def get_exercise_history(
        self, student_id: str, topic_id: str, enrollment_id: Optional[str], caller: User,
    ) -> list[ExerciseAttemptHistoryItem]:
        student = await User.find_one({"_id": student_id, "is_deleted": False, "role": Role.STUDENT.value})
        if student is None:
            raise NotFoundException("Student not found")
        visible_ids = await self._authorized_enrollment_ids(student, caller)
        if visible_ids is not None and enrollment_id is not None and enrollment_id not in visible_ids:
            raise ForbiddenException("You are not authorized to view this student's progress")
        query: dict = {"student_id": student_id, "topic_id": topic_id}
        if enrollment_id is not None:
            query["enrollment_id"] = enrollment_id
        attempts = await ExerciseAttempt.find(query).sort("-created_at").to_list()
        # The caller is already authorized to see this student's full
        # history (role/Enrollment check above) - fetching the real,
        # unmasked lesson_exercises here (never the student-masked view) is
        # exactly what lets `correct_answer_display` show the true answer.
        topic = await TopicContent.get(topic_id)
        exercises = topic.lesson_exercises if topic is not None else []
        return [
            ExerciseAttemptHistoryItem(
                id=a.id, created_at=a.created_at, exercise_index=a.exercise_index, exercise_kind=a.exercise_kind,
                exercise_prompt=a.exercise_prompt, answer=a.answer, answer_display=a.answer_display,
                correct=a.correct, correct_option=a.correct_option,
                correct_answer_display=(
                    correct_answer_display(exercises[a.exercise_index]) or None
                    if 0 <= a.exercise_index < len(exercises) else None
                ),
                score=a.score, reviewed=a.reviewed, superseded=a.superseded, feedback=a.feedback,
            )
            for a in attempts
        ]


def get_student_progress_service() -> StudentProgressService:
    from app.services.cohort_service import get_cohort_service
    from app.services.enrollment_service import get_enrollment_service

    return StudentProgressService(get_cohort_service(), get_enrollment_service())
