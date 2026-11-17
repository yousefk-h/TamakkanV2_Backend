"""The real, per-topic "has this student actually finished this lesson"
check - built for the COMPLETION unlock policy (see `app.models.program
.UnlockPolicy`). This did NOT exist anywhere before: the legacy
`_had_a_real_conversation` heuristic in `app.services.progress_service` is a
weak, Mariam-conversation-only signal (60s/2-turns, no `MAX_DURATION`
requirement) used only to color the SCHEDULE-policy calendar, and is
deliberately left untouched there - reusing it here would let an early
manual hangup unlock content, exactly the bypass
`LiveSessionService._is_meaningful_practice_completion` was written to close
for Practice-with-Mariam checkpoints.

Every check below is server-authoritative and keys off data the student
cannot write directly (`ExerciseAttempt.correct`, `LiveSession.end_reason`/
`transcript_history`, both internal-agent- or grading-written). Deliberately
queries `ExerciseAttempt`/`LiveSession` directly instead of going through
`ExerciseAttemptService.mine()`/`LiveSessionService.get_checkpoint_session_
status()`'s own entitlement-checking callers - this service is itself a
dependency of `ProgressService`'s entitlement computation, so going back
through a function that re-derives entitlement would recurse.
"""

from app.models.enrollment import Enrollment
from app.models.live_session import EndReason, LiveSession, SessionState
from app.models.exercise_attempt import ExerciseAttempt
from app.models.topic_content import CheckpointKind, InteractiveVideoCompletionRule, LessonFlow, TopicContent
from app.models.user import User
from app.services.exercise_attempt_service import ANSWER_KINDS, exercise_hash

# Open-ended kinds a teacher must review before a `correct` verdict ever
# exists (`ExerciseAttemptService.submit`) - for unlock purposes a genuine
# submission is itself the student's part of the work; waiting on a teacher's
# eventual review would make completion-based unlocking depend on teacher
# turnaround time, which is not what "unlock by completion" means.
_OPEN_ENDED_KINDS = frozenset({"writing", "reflection"})


def _is_meaningful_conversation(session: LiveSession) -> bool:
    """Same strict rule as `LiveSessionService._is_meaningful_practice_completion`
    (state ENDED, the non-spoofable `end_reason == MAX_DURATION`, a genuine
    two-sided transcript) - duplicated as a small, dependency-free predicate
    rather than imported, since `LiveSessionService` is a much heavier
    service (LiveKit token minting, etc.) this module has no other reason to
    depend on. Used here for a STANDALONE (non-checkpoint) Mariam practice
    topic; `LiveSessionService` keeps its own copy for checkpoint sessions."""
    if session.state != SessionState.ENDED:
        return False
    if session.end_reason != EndReason.MAX_DURATION:
        return False
    speakers = {turn.speaker for turn in session.transcript_history}
    return "student" in speakers and "mariam" in speakers


class TopicCompletionService:
    """`is_topic_completed` is the one aggregate a COMPLETION-policy
    program's unlock gating calls - one topic at a time, in curriculum
    order, so the caller can stop at the first incomplete node rather than
    evaluating the whole template every time."""

    async def _attempts_by_index(
        self, student: User, topic: TopicContent, enrollment: "Enrollment | None", program_id: str | None, legacy_attributable: bool
    ) -> dict[int, ExerciseAttempt]:
        base = {"student_id": student.id, "topic_id": topic.id, "is_deleted": False, "superseded": False}
        if enrollment is not None and legacy_attributable:
            query: dict = {"$or": [
                {**base, "enrollment_id": enrollment.id},
                {**base, "program_id": program_id, "enrollment_id": None},
            ]}
        elif enrollment is not None:
            query = {**base, "enrollment_id": enrollment.id}
        else:
            query = {**base, "program_id": program_id, "enrollment_id": None}
        attempts = await ExerciseAttempt.find(query).sort("-created_at").to_list()
        hashes = {index: exercise_hash(exercise) for index, exercise in enumerate(topic.lesson_exercises)}
        by_index: dict[int, ExerciseAttempt] = {}
        for attempt in attempts:
            # A stale attempt against an exercise the teacher has since
            # edited must not count toward the NEW version's completion -
            # same guard `ExerciseAttemptService.mine()` already applies.
            if attempt.exercise_index not in by_index and hashes.get(attempt.exercise_index) == attempt.exercise_hash:
                by_index[attempt.exercise_index] = attempt
        return by_index

    @staticmethod
    def _exercise_satisfied(kind_value: str, attempt: ExerciseAttempt | None) -> bool:
        if attempt is None:
            return False
        if kind_value in _OPEN_ENDED_KINDS:
            return True  # no autograde exists; a genuine submission is the student's whole part
        return attempt.correct is True

    async def _standalone_practice_completed(
        self, student: User, topic: TopicContent, enrollment: "Enrollment | None", program_id: str | None,
        program_level_id: str | None, legacy_attributable: bool,
    ) -> bool:
        query: dict = {"student_id": student.id, "topic_id": topic.id, "checkpoint_id": None, "is_deleted": False}
        if enrollment is not None and legacy_attributable:
            or_query = {"$or": [
                {**query, "enrollment_id": enrollment.id},
                {**query, "program_id": program_id, "program_level_id": program_level_id, "enrollment_id": None},
            ]}
        elif enrollment is not None:
            or_query = {**query, "enrollment_id": enrollment.id}
        else:
            or_query = {**query, "program_id": program_id, "program_level_id": program_level_id, "enrollment_id": None}
        sessions = await LiveSession.find(or_query).to_list()
        return any(_is_meaningful_conversation(session) for session in sessions)

    async def _interactive_video_completed(
        self, student: User, topic: TopicContent, enrollment: "Enrollment | None", program_id: str | None, legacy_attributable: bool,
    ) -> bool:
        checkpoints = topic.interactive_video_checkpoints
        if not checkpoints:
            return True  # a published Interactive Video with no checkpoints has nothing to require
        exercise_checkpoints = [c for c in checkpoints if c.kind == CheckpointKind.EXERCISE]
        attempts_by_index: dict[int, ExerciseAttempt] = {}
        if exercise_checkpoints:
            attempts_by_index = await self._attempts_by_index(student, topic, enrollment, program_id, legacy_attributable)
        for checkpoint in checkpoints:
            if checkpoint.kind == CheckpointKind.EXERCISE:
                attempt = attempts_by_index.get(checkpoint.exercise_index)
                if attempt is None:
                    return False
                if checkpoint.completion_rule == InteractiveVideoCompletionRule.CORRECT and attempt.correct is not True:
                    return False
            else:  # PRACTICE_WITH_MARIAM
                sessions = await LiveSession.find({
                    "student_id": student.id, "checkpoint_id": checkpoint.id, "is_deleted": False,
                }).to_list()
                if not any(_is_meaningful_conversation(session) for session in sessions):
                    return False
        return True

    async def is_topic_completed(
        self,
        student: User,
        topic: TopicContent,
        *,
        enrollment: "Enrollment | None",
        program_id: str | None,
        program_level_id: str | None,
        legacy_attributable: bool,
    ) -> bool:
        if topic.lesson_flow == LessonFlow.INTERACTIVE_VIDEO:
            return await self._interactive_video_completed(student, topic, enrollment, program_id, legacy_attributable)
        answerable = [(i, ex) for i, ex in enumerate(topic.lesson_exercises) if ex.kind in ANSWER_KINDS]
        if not answerable:
            return await self._standalone_practice_completed(
                student, topic, enrollment, program_id, program_level_id, legacy_attributable
            )
        attempts_by_index = await self._attempts_by_index(student, topic, enrollment, program_id, legacy_attributable)
        return all(
            self._exercise_satisfied(exercise.kind.value, attempts_by_index.get(index))
            for index, exercise in answerable
        )


def get_topic_completion_service() -> TopicCompletionService:
    return TopicCompletionService()
