from app.core.exercise_drafting import DraftExerciseOutput
from app.schema.topic import LessonExerciseIn


def test_provider_contract_keeps_nested_exercise_schema_out_of_gemini_request():
    """Gemini receives a bounded JSON object, then our normal model validates it."""
    schema = DraftExerciseOutput.model_json_schema()
    assert schema["properties"]["exercises"]["items"]["additionalProperties"] is True
    assert "$defs" not in schema


def test_raw_provider_item_still_requires_a_valid_lesson_exercise():
    raw = {
        "kind": "writing",
        "prompt": "Write three sentences about your daily routine.",
        "min_words": 20,
    }
    exercise = LessonExerciseIn.model_validate(raw)
    assert exercise.kind.value == "writing"
    assert exercise.min_words == 20
