"""Deterministic transcript measurements and checks around model-generated feedback."""
import re

from app.models.learning_analysis import AssessmentFeedback, ProgressComparison, TranscriptMetrics
from app.models.live_session import InteractionTurn, LiveSession, SessionState, SpeakingReport

SCORE_FIELDS = ("fluency_score", "vocabulary_score", "grammar_score", "complexity_score", "overall_score")
WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")


def transcript_metrics(session: LiveSession) -> TranscriptMetrics:
    turns = [turn for turn in session.transcript_history if turn.speaker == "student" and turn.text.strip()]
    words = [word.casefold() for turn in turns for word in WORD.findall(turn.text)]
    duration = None
    if session.ended_at is not None and session.ended_at >= session.started_at:
        duration = round((session.ended_at - session.started_at).total_seconds())
    return TranscriptMetrics(
        student_turns=len(turns), student_words=len(words), unique_student_words=len(set(words)),
        average_words_per_turn=round(len(words) / len(turns), 1) if turns else 0,
        session_duration_seconds=duration,
    )


def has_enough_evidence(metrics: TranscriptMetrics) -> bool:
    # A conservative product threshold, not a validated language-proficiency test.
    return metrics.student_turns >= 3 and metrics.student_words >= 30 and metrics.unique_student_words >= 10


def validate_feedback(feedback: AssessmentFeedback, turns: list[InteractionTurn]) -> None:
    """Reject invented quotes, tutor-attributed errors, and false repeated patterns."""
    quotes = [item.evidence for item in feedback.strengths + feedback.improvement_areas + feedback.corrections]
    for pattern in feedback.recurring_patterns:
        if len({item.turn_index for item in pattern.evidence}) < 2:
            raise ValueError("A recurring pattern needs distinct student turns")
        quotes.extend(pattern.evidence)
    for evidence in quotes:
        if evidence.turn_index > len(turns):
            raise ValueError("Evidence refers to an absent turn")
        turn = turns[evidence.turn_index - 1]
        if turn.speaker != "student" or evidence.quote not in turn.text or not evidence.quote.strip():
            raise ValueError("Evidence must quote the student's actual words")
    for correction in feedback.corrections:
        if correction.corrected.strip() == correction.evidence.quote.strip():
            raise ValueError("A correction must change the quoted text")


def comparison_for(session: LiveSession, report: SpeakingReport, history: list[LiveSession]) -> ProgressComparison | None:
    analysis = report.analysis
    if not analysis or analysis.status != "assessed" or not session.topic_id or not analysis.target_level:
        return None
    candidates = [item for item in history if (
        item.id != session.id and item.topic_id == session.topic_id and item.student_id == session.student_id
        and item.state == SessionState.ENDED and not item.is_deleted
        and item.ended_at is not None and item.ended_at <= session.started_at
        and item.report is not None and item.report.analysis is not None
        and item.report.analysis.rubric_version == analysis.rubric_version
        and item.report.analysis.target_level == analysis.target_level
        and item.report.analysis.status == "assessed"
        and all(getattr(item.report, name) is not None for name in SCORE_FIELDS)
    )]
    if not candidates or any(getattr(report, name) is None for name in SCORE_FIELDS):
        return None
    previous = max(candidates, key=lambda item: item.started_at)
    assert previous.report is not None and previous.report.analysis is not None
    deltas = {name.replace("_score", "_change"): getattr(report, name) - getattr(previous.report, name) for name in SCORE_FIELDS}
    return ProgressComparison(
        previous_session_id=previous.id, previous_session_date=previous.started_at, **deltas,
        student_words_change=analysis.metrics.student_words - previous.report.analysis.metrics.student_words,
    )
