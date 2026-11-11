"""Deterministic quality gate for AI-drafted exercise suggestions (Slice 4).
No AI call involved - every case here is a plain heuristic over the
suggestion's own text, so it runs offline and fast."""

from app.core.exercise_quality import existing_exercises_for_duplicate_check, validate_exercise_suggestion
from app.models.topic_content import ExerciseKind, Level, LessonExercise, LessonDraftExerciseSuggestion, LessonDraftStepState, TopicContent

SOURCE = (
    "Maya describes her typical morning routine in great detail. She wakes up at seven, "
    "makes coffee, and reviews her schedule before heading to work. She usually takes the "
    "bus and reads a book during the commute to her office downtown."
)
OBJECTIVES = ["describe a simple daily routine using the present simple tense"]


def _mc(prompt: str, options=("A", "B"), content: str = "", correct_option: int = 0) -> LessonExercise:
    return LessonExercise(
        kind=ExerciseKind.MULTIPLE_CHOICE, prompt=prompt, options=list(options),
        correct_option=correct_option, content=content,
    )


def test_a_well_grounded_objective_aligned_exercise_passes():
    exercise = _mc(
        "Which sentence correctly describes Maya's daily routine using the present simple tense?",
        options=["She wakes up at seven and takes the bus to work", "She woke up at seven yesterday",
                 "She will wake up at seven tomorrow", "She is waking up at seven now"],
    )
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=OBJECTIVES, level=Level.BEGINNER, siblings=[])
    assert issues == []


def test_an_ungrounded_exercise_with_no_overlap_with_the_source_is_rejected():
    exercise = _mc(
        "What is the capital of France and which river runs through it?",
        options=["Paris / Seine", "Lyon / Rhone", "Nice / nothing", "Marseille / nothing"],
    )
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[])
    assert any(i.code == "ungrounded" for i in issues)


def test_trivial_recall_with_no_objective_connection_is_rejected():
    exercise = _mc(
        "What time did Maya wake up?",
        options=["Seven", "Eight", "Nine", "Ten"],
    )
    # Deliberately unrelated objective so there is no overlap at all.
    issues = validate_exercise_suggestion(
        exercise, source=SOURCE, objectives=["negotiate a hotel reservation by phone"], level=Level.BEGINNER, siblings=[],
    )
    assert any(i.code == "trivial_recall" for i in issues)


def test_the_same_trivial_sounding_question_passes_when_it_IS_objective_aligned():
    exercise = _mc(
        "What time does Maya wake up, and how does she describe her morning routine?",
        options=["At seven, using the present simple", "At noon", "At midnight", "She doesn't"],
    )
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=OBJECTIVES, level=Level.BEGINNER, siblings=[])
    assert not any(i.code == "trivial_recall" for i in issues)


def test_a_near_duplicate_of_an_existing_exercise_is_rejected():
    existing = _mc("What does Maya usually do during her bus commute to work?")
    candidate = _mc("What does Maya usually do during her commute to work on the bus?")
    issues = validate_exercise_suggestion(candidate, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[existing])
    assert any(i.code == "duplicate" for i in issues)


def test_an_unsupported_kind_is_rejected():
    exercise = LessonExercise(kind=ExerciseKind.VIDEO, prompt="Watch the lesson video.")
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[])
    assert any(i.code == "unsupported_kind" for i in issues)


def test_content_excluded_by_the_center_policy_is_rejected():
    exercise = _mc("Describe how to order a glass of wine at a restaurant.", options=["A", "B"])
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[])
    assert any(i.code == "content_policy" for i in issues)


def test_a_word_order_exercise_is_graded_on_its_ordering_words_grounding():
    exercise = LessonExercise(
        kind=ExerciseKind.WORD_ORDER, prompt="Put the sentence in order.",
        ordering_words=["Maya", "describes", "her", "typical", "morning", "routine"],
    )
    issues = validate_exercise_suggestion(exercise, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[])
    assert issues == []

    ungrounded = LessonExercise(
        kind=ExerciseKind.WORD_ORDER, prompt="Put the sentence in order.",
        ordering_words=["The", "Eiffel", "Tower", "is", "in", "Paris"],
    )
    issues = validate_exercise_suggestion(ungrounded, source=SOURCE, objectives=[], level=Level.BEGINNER, siblings=[])
    assert any(i.code == "ungrounded" for i in issues)


def test_existing_exercises_for_duplicate_check_covers_both_published_and_pending():
    topic = TopicContent(title="t", level=Level.BEGINNER)
    published = _mc("Published question")
    pending = _mc("Pending question")
    topic.lesson_exercises = [published]
    topic.draft_exercises = [LessonDraftExerciseSuggestion(kind=ExerciseKind.MULTIPLE_CHOICE, exercise=pending, state=LessonDraftStepState.DONE)]
    siblings = existing_exercises_for_duplicate_check(topic)
    assert [s.prompt for s in siblings] == ["Published question", "Pending question"]
