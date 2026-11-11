"""Deterministic quality gate for an AI-drafted exercise suggestion, run
before it is ever shown to a teacher. Entirely heuristic - no second AI
call - so it adds zero generation cost and stays fast/offline-testable.

Checks: supported kind, schema-level answer correctness (reused, not
duplicated, from `LessonExerciseIn`'s own validator - this module only adds
what that validator can't know: content vs. source/objectives/siblings),
source grounding, objective alignment (when objectives exist), difficulty
fit for the stated level, and duplicate/near-duplicate detection against
both the sibling suggestions in this same batch and the topic's own
already-saved/pending exercises. Trivial-recall phrasing is rejected only
when it is ALSO ungrounded/unaligned - a real comprehension question tied to
an objective is not penalized just for asking "what/when/who".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from app.models.topic_content import ExerciseKind, Level, LessonExercise
from app.core.exercise_drafting import GENERATABLE_KINDS

_WORD_RE = re.compile(r"[a-zA-Z']+")
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "at", "for",
    "and", "or", "but", "do", "does", "did", "what", "when", "where", "who", "how",
    "why", "this", "that", "with", "it", "its", "their", "they", "he", "she", "you",
    "your", "his", "her", "them", "i", "we", "will", "be", "has", "have", "had",
})
_TRIVIAL_RECALL_RE = re.compile(
    r"^(what (time|color|day)|when did|who said|what was the exact)\b", re.IGNORECASE
)
DUPLICATE_SIMILARITY_THRESHOLD = 0.82
MIN_GROUNDING_OVERLAP = 2  # at least this many meaningful words shared with the source
MIN_OBJECTIVE_OVERLAP = 1

# Minimal, keyword-level content-safety net for this slice only - the full
# auditable center content policy (exclusion reporting, source-level
# filtering, Avatar/session enforcement) is a later, separately-scoped phase
# (see docs/ROADMAP.md). This just stops the single worst case: a drafted
# exercise surfacing disallowed subject matter outright.
_UNSAFE_TOPIC_RE = re.compile(
    r"\b(alcohol|wine|beer|pork|bacon|ham sandwich|sexual|nudity|explicit)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 2 and w.lower() not in _STOPWORDS}


def _exercise_text(exercise) -> str:
    """Every piece of text a reader would judge the exercise by - prompt,
    content, explanation and any options - so grounding/alignment/duplicate
    checks see the whole exercise, not just its question line."""
    parts = [exercise.prompt, exercise.content, exercise.explanation, *exercise.options]
    return " ".join(p for p in parts if p)


def _difficulty_bounds(level: Level) -> tuple[int, int]:
    """Rough average-word-length-per-sentence band per level - catches an
    exercise that is wildly off the stated level (a one-word beginner prompt
    masquerading as a question, or a toddler-simple "advanced" item), not a
    precise readability scorer."""
    return {
        Level.BEGINNER: (3, 40),
        Level.INTERMEDIATE: (5, 60),
        Level.ADVANCED: (6, 90),
    }[level]


def validate_exercise_suggestion(
    exercise,
    *,
    source: str,
    objectives: list[str],
    level: Level,
    siblings: list,
) -> list[QualityIssue]:
    """Returns every issue found (empty = passes). `siblings` is every other
    exercise this suggestion must not duplicate - already-saved
    `lesson_exercises`, already-pending `draft_exercises`, and any other
    suggestion drafted in the same batch."""
    issues: list[QualityIssue] = []

    if exercise.kind not in GENERATABLE_KINDS:
        issues.append(QualityIssue("unsupported_kind", f"'{exercise.kind.value}' is not an AI-draftable exercise type"))

    prompt = (exercise.prompt or "").strip()
    if not prompt:
        issues.append(QualityIssue("empty_prompt", "The exercise has no question/prompt text"))

    full_text = _exercise_text(exercise)
    if _UNSAFE_TOPIC_RE.search(full_text):
        issues.append(QualityIssue("content_policy", "This exercise mentions a topic excluded by the center's content policy"))

    exercise_words = _keywords(full_text)
    source_words = _keywords(source)
    grounding_overlap = len(exercise_words & source_words)
    if source_words and grounding_overlap < MIN_GROUNDING_OVERLAP:
        issues.append(QualityIssue(
            "ungrounded",
            "This exercise doesn't share enough real language with the approved lesson source to be clearly grounded in it",
        ))

    if objectives:
        objective_words: set[str] = set()
        for text in objectives:
            objective_words |= _keywords(text)
        objective_overlap = len(exercise_words & objective_words)
        if objective_overlap < MIN_OBJECTIVE_OVERLAP:
            issues.append(QualityIssue(
                "not_objective_aligned",
                "This exercise doesn't connect to any approved learning objective",
            ))
        if _TRIVIAL_RECALL_RE.match(prompt) and objective_overlap < MIN_OBJECTIVE_OVERLAP:
            issues.append(QualityIssue(
                "trivial_recall",
                "This looks like a trivial fact-recall question with no link to a learning objective",
            ))

    _low, high = _difficulty_bounds(level)
    prompt_word_count = len(prompt.split())
    if prompt_word_count and prompt_word_count < 2 and exercise.kind != ExerciseKind.GAME:
        issues.append(QualityIssue("too_short", "The prompt is too short to be a real exercise"))
    if prompt_word_count > high * 6:
        issues.append(QualityIssue(
            "too_complex_for_level",
            f"This prompt looks far more complex than the {level.value} level it was drafted for",
        ))

    candidate_norm = prompt.lower().strip()
    for sibling in siblings:
        sibling_prompt = getattr(sibling, "prompt", None)
        if not sibling_prompt:
            continue
        ratio = difflib.SequenceMatcher(None, candidate_norm, sibling_prompt.lower().strip()).ratio()
        if ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            issues.append(QualityIssue("duplicate", "This exercise is a near-duplicate of an existing one"))
            break

    return issues


def existing_exercises_for_duplicate_check(topic) -> list[LessonExercise]:
    """Every exercise a fresh suggestion must not duplicate: published ones
    and whatever is already sitting in the pending-review queue."""
    return list(topic.lesson_exercises) + [item.exercise for item in topic.draft_exercises]
