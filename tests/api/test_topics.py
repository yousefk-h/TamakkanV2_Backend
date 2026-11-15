"""Topic/content CRUD (Kanban card #9) and the transcript pipeline (card #11)."""

import io
from datetime import timedelta

import pytest

from app.core.application import app
from app.core.content_drafting import DraftedContent
from app.core.security import hash_password
from app.core.storage import DocumentUploadResult, ImageUploadResult, UploadFailedError
from app.core.transcription import TranscriptionError
from app.core.transcript_english_review import TranscriptCorrection, TranscriptReviewResult
from app.models.topic_content import Level, TopicContent, TranscriptSegment, VocabularyItem
from app.models.user import PlanType, Role, User
from app.repositories.topic_content_repository import TopicContentRepository
from app.services.progress_service import get_progress_service
from app.services.topic_content_service import TopicContentService, get_topic_content_service
from app.services.exercise_draft_service import ExerciseDraftService, get_exercise_draft_service
from app.services.objective_draft_service import ObjectiveDraftService, get_objective_draft_service
from app.services.interactive_video_draft_service import InteractiveVideoDraftService, get_interactive_video_draft_service
from app.schema.topic import LessonExerciseIn, TopicListItem
from app.services.transcript_pipeline_service import (
    TranscriptPipelineService,
    get_transcript_pipeline_service,
)
from app.services.transcript_review_service import (
    TranscriptReviewService,
    get_transcript_review_service,
)
from tests.core.test_pdf_extraction import build_minimal_pdf
from tests.core.test_document_extraction import build_docx, build_pptx
from tests.services.test_transcript_pipeline_service import (
    FakeContentDrafter,
    FakeTranscriber,
)
from tests.services.test_transcript_review_service import FakeEnglishReviewer

TOPIC_PAYLOAD = {
    "title": "Talking about Music",
    "level": "intermediate",
    "youtube_urls": ["https://youtube.com/watch?v=abc123"],
    "warm_up_questions": ["What's your favorite genre?"],
}


async def _login(client, role: Role, email: str, password: str = "correct-password", **extra) -> str:
    # No entitlement defaults here on purpose: most of this module's Student
    # cases are *about* what an unentitled student can see, so a plan has to be
    # opted into explicitly (unlike `tests/api/test_submissions.py`, where every
    # student needs one just to reach the endpoint under test).
    user = User(email=email, password_hash=hash_password(password), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


def _override_transcript_service(
    transcriber=None,
    content_drafter=None,
    extraction_timeout=timedelta(minutes=30),
):
    app.dependency_overrides[get_transcript_pipeline_service] = lambda: TranscriptPipelineService(
        TopicContentRepository(),
        transcriber or FakeTranscriber(),
        content_drafter or FakeContentDrafter(),
        extraction_timeout=extraction_timeout,
    )


@pytest.fixture(autouse=True)
def _clear_transcript_override():
    yield
    app.dependency_overrides.pop(get_transcript_pipeline_service, None)


async def test_administrator_can_create_a_topic(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Talking about Music"
    assert body["level"] == "intermediate"
    assert body["youtube_urls"] == TOPIC_PAYLOAD["youtube_urls"]


async def test_topic_library_search_returns_bounded_card_metadata(client):
    token = await _login(client, Role.TEACHER, "library-search@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    matching = client.post("/api/v1/topics", json={
        "title": "At the pharmacy",
        "level": "intermediate",
        "youtube_urls": ["https://youtube.com/watch?v=abc123"],
        "lesson_exercises": [
            {"kind": "listening", "prompt": "Which medicine did Mariam request?", "content": "Mariam asks for medicine.", "audio_url": "https://example.com/audio.mp3", "options": ["Tea", "Medicine"], "correct_option": 1},
            {"kind": "speaking", "prompt": "Ask the pharmacist for help"},
        ],
    }, headers=headers)
    assert matching.status_code == 200, matching.text
    client.post("/api/v1/topics", json={"title": "Daily routine", "level": "beginner"}, headers=headers)

    response = client.get("/api/v1/topics", params={
        "search": "medicine", "level": "intermediate", "limit": 1, "skip": 0,
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    card = response.json()[0]
    assert card["id"] == matching.json()["id"]
    assert card["author_name"] == "Test User"
    assert card["has_video"] is True
    assert card["has_audio"] is True
    assert card["exercise_count"] == 2
    assert card["exercise_kinds"] == ["listening", "speaking"]


async def test_teacher_can_author_and_reorder_lesson_exercises(client):
    token = await _login(client, Role.TEACHER, "exercise-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json={"title": "Daily routines", "level": "beginner"}, headers=headers)
    assert created.status_code == 200
    topic_id = created.json()["id"]
    exercises = [
        {"kind": "multiple_choice", "prompt": "When does she wake up?", "options": ["7 am", "9 am"], "correct_option": 0, "explanation": "The source says seven."},
        {"kind": "reflection", "prompt": "Describe your morning.", "options": [], "correct_option": None, "explanation": ""},
    ]
    updated = client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": exercises}, headers=headers)
    assert updated.status_code == 200
    assert [{key: item[key] for key in authored} for item, authored in zip(updated.json()["lesson_exercises"], exercises)] == exercises
    fetched = client.get(f"/api/v1/topics/{topic_id}", headers=headers)
    assert [item["kind"] for item in fetched.json()["lesson_exercises"]] == ["multiple_choice", "reflection"]
    reordered = client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": exercises[::-1]}, headers=headers)
    assert reordered.status_code == 200
    assert reordered.json()["lesson_exercises"][0]["kind"] == "reflection"
    from app.utils.time import utcnow
    teacher_id = client.get("/api/v1/me", headers=headers).json()["id"]
    student_token = await _login(client, Role.STUDENT, "exercise-student@example.com", plan=PlanType.STANDARD,
                                 assigned_level=Level.BEGINNER, program_start_date=utcnow(), created_by_id=teacher_id)
    template = client.post("/api/v1/calendar-templates", json={"name": "Beginner plan", "kind": "standard", "level": "beginner",
                                                           "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=headers)
    assert template.status_code == 200
    activated = client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=headers)
    assert activated.status_code == 200
    student_view = client.get(f"/api/v1/topics/{topic_id}", headers={"Authorization": f"Bearer {student_token}"})
    assert student_view.status_code == 200
    assert [item["kind"] for item in student_view.json()["lesson_exercises"]] == ["reflection", "multiple_choice"]
    assert student_view.json()["avatar_prompt"] is None


async def test_lesson_exercises_support_mixed_legacy_and_new_kinds_end_to_end(client):
    """Regression for the Slice 5 integration report: a teacher saves a lesson
    mixing pre-existing kinds (multiple_choice/reading/game) with the new
    ones (word_order/error_correction/dialogue_completion) in one PATCH, the
    whole list round-trips after a reload, survives a reorder, and - once the
    topic is actually placed on an activated calendar (this app's real
    "publish" step, same as `test_teacher_can_author_and_reorder_lesson_exercises`
    above) - a Student sees every kind with its answer key masked."""
    token = await _login(client, Role.TEACHER, "mixed-kinds-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json={"title": "Mixed kinds lesson", "level": "intermediate"}, headers=headers)
    assert created.status_code == 200
    topic_id = created.json()["id"]
    exercises = [
        {"kind": "multiple_choice", "prompt": "Pick one", "options": ["a", "b"], "correct_option": 0, "explanation": ""},
        {"kind": "reading", "prompt": "Read this", "options": ["Early", "Late"], "correct_option": 0, "explanation": "", "content": "Maya wakes up early."},
        {"kind": "game", "prompt": "Match", "options": [], "correct_option": None, "explanation": "", "pairs": [{"left": "hi", "right": "hello"}, {"left": "bye", "right": "goodbye"}]},
        {"kind": "word_order", "prompt": "Put in order", "options": [], "correct_option": None, "explanation": "", "ordering_words": ["Maya", "wakes", "up", "early"]},
        {"kind": "error_correction", "prompt": "Fix it", "options": [], "correct_option": None, "explanation": "Tense", "content": "Maya wake up early.", "accepted_answers": ["Maya wakes up early."]},
        {"kind": "dialogue_completion", "prompt": "Complete it", "options": [], "correct_option": None, "explanation": "",
         "dialogue_turns": [{"speaker": "A", "text": "What time do you wake up?", "is_blank": False, "accepted_answers": []},
                            {"speaker": "B", "text": "", "is_blank": True, "accepted_answers": ["I wake up early."]}]},
    ]
    kinds = [item["kind"] for item in exercises]

    saved = client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": exercises}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert [item["kind"] for item in saved.json()["lesson_exercises"]] == kinds

    reloaded = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    assert [item["kind"] for item in reloaded["lesson_exercises"]] == kinds
    assert reloaded["lesson_exercises"][3]["ordering_words"] == ["Maya", "wakes", "up", "early"]
    assert reloaded["lesson_exercises"][5]["dialogue_turns"][1]["accepted_answers"] == ["I wake up early."]

    reordered = client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": exercises[::-1]}, headers=headers)
    assert reordered.status_code == 200
    assert [item["kind"] for item in reordered.json()["lesson_exercises"]] == kinds[::-1]

    from app.utils.time import utcnow
    teacher_id = client.get("/api/v1/me", headers=headers).json()["id"]
    student_token = await _login(client, Role.STUDENT, "mixed-kinds-student@example.com", plan=PlanType.STANDARD,
                                 assigned_level=Level.INTERMEDIATE, program_start_date=utcnow(), created_by_id=teacher_id)
    template = client.post("/api/v1/calendar-templates", json={"name": "Mixed kinds plan", "kind": "standard", "level": "intermediate",
                                                           "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=headers)
    assert template.status_code == 200
    activated = client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=headers)
    assert activated.status_code == 200

    student_view = client.get(f"/api/v1/topics/{topic_id}", headers={"Authorization": f"Bearer {student_token}"}).json()
    student_kinds = [item["kind"] for item in student_view["lesson_exercises"]]
    assert sorted(student_kinds) == sorted(kinds[::-1]), "every new kind reaches the Student alongside the old ones"
    by_kind = {item["kind"]: item for item in student_view["lesson_exercises"]}
    assert by_kind["word_order"]["ordering_words"] != ["Maya", "wakes", "up", "early"], "answer key stays masked"
    assert sorted(by_kind["word_order"]["ordering_words"]) == ["Maya", "early", "up", "wakes"]
    assert by_kind["error_correction"]["accepted_answers"] == []
    assert by_kind["dialogue_completion"]["dialogue_turns"][1]["accepted_answers"] == []


async def test_lesson_exercises_reject_incomplete_answer_keys(client):
    token = await _login(client, Role.TEACHER, "invalid-exercise@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/v1/topics", json={"title": "Bad quiz", "level": "beginner", "lesson_exercises": [
        {"kind": "multiple_choice", "prompt": "Choose one", "options": ["A", "B"], "correct_option": 4},
    ]}, headers=headers)
    assert response.status_code == 422


async def test_teacher_can_save_the_reference_skill_palette_in_order(client):
    token = await _login(client, Role.TEACHER, "palette-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    answer = {"options": ["yes", "no"], "correct_option": 0}
    blocks = [
        {"kind": "video", "prompt": "Watch the video."},
        {"kind": "vocab", "prompt": "Learn these words."},
        {"kind": "image_completion", "prompt": "What is shown?", "media_url": "https://example.com/picture.jpg", **answer},
        {"kind": "listening", "prompt": "What did you hear?", "content": "I go to school.", **answer},
        {"kind": "grammar", "prompt": "Choose the correct sentence.", "content": "Use the present simple.", "arabic_note": "المضارع البسيط", **answer},
        {"kind": "reading", "prompt": "What happened?", "content": "Maya went to school.", **answer},
        {"kind": "writing", "prompt": "Write about your day.", "min_words": 20},
        {"kind": "speaking", "prompt": "Describe your routine to Mariam."},
        {"kind": "game", "prompt": "Match the pairs.", "pairs": [{"left": "hello", "right": "مرحبا"}, {"left": "bye", "right": "وداعا"}]},
        {"kind": "series", "prompt": "Episode 1", "content": "Maya meets a friend."},
    ]
    created = client.post("/api/v1/topics", json={"title": "Full lesson", "level": "beginner", "lesson_exercises": blocks}, headers=headers)
    assert created.status_code == 200
    assert [item["kind"] for item in created.json()["lesson_exercises"]] == [item["kind"] for item in blocks]
    fetched = client.get(f"/api/v1/topics/{created.json()['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["lesson_exercises"][8]["pairs"][0] == {"left": "hello", "right": "مرحبا"}


async def test_ai_exercise_draft_is_grounded_teacher_scoped_and_not_saved(client):
    class FakeDrafter:
        async def draft(self, **kwargs):
            assert "morning routine" in kwargs["source"]
            assert kwargs["kind"].value == "reading"
            assert kwargs["count"] == 1
            return [LessonExerciseIn(
                kind="reading", prompt="What does Maya describe about her morning routine?",
                content="Maya describes her typical morning routine in the passage.",
                options=["Her morning routine", "Her evening routine"], correct_option=0,
            )]

    teacher_token = await _login(client, Role.TEACHER, "ai-exercise-teacher@example.com")
    other_token = await _login(client, Role.TEACHER, "ai-exercise-other@example.com")
    headers = {"Authorization": f"Bearer {teacher_token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    body = {"kind": "reading", "count": 1}
    route = f"/api/v1/topics/{topic['id']}/draft-exercises"
    assert client.post(route, json=body, headers=headers).status_code == 422  # no approved source
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her morning routine. " * 5
    await stored.save()
    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), FakeDrafter())
    try:
        assert client.post(route, json=body, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
        response = client.post(route, json=body, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 200
    assert response.json()["exercises"][0]["prompt"] == "What does Maya describe about her morning routine?"
    assert client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()["lesson_exercises"] == []


async def test_a_low_quality_draft_is_retried_once_then_succeeds(client):
    """Slice 4: the quality gate rejects an ungrounded/trivial first attempt;
    the service retries once (its one bounded retry), and a genuinely better
    second attempt is accepted and returned."""
    class FlakyDrafter:
        def __init__(self):
            self.attempts = 0

        async def draft(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                return [LessonExerciseIn(kind="reading", prompt="What is the capital of France?",
                                         content="Paris.", options=["Paris", "Lyon"], correct_option=0)]
            return [LessonExerciseIn(
                kind="reading", prompt="What does Maya do as part of her typical morning routine?",
                content="Maya describes her typical morning routine in detail.",
                options=["Her morning routine", "Nothing relevant"], correct_option=0,
            )]

    token = await _login(client, Role.TEACHER, "ai-exercise-retry@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her typical morning routine. " * 5
    await stored.save()

    drafter = FlakyDrafter()
    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), drafter)
    try:
        response = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "reading", "count": 1}, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 200, response.text
    assert drafter.attempts == 2
    assert response.json()["exercises"][0]["prompt"] == "What does Maya do as part of her typical morning routine?"


async def test_ai_can_draft_a_grounded_word_order_exercise(client):
    """Slice 5: the AI exercise-drafting pipeline (and its Slice 4 quality
    gate) accepts a well-grounded, correctly-shaped word_order suggestion."""
    class FakeDrafter:
        async def draft(self, **kwargs):
            assert kwargs["kind"].value == "word_order"
            return [LessonExerciseIn(
                kind="word_order", prompt="Put the sentence in order.",
                ordering_words=["Maya", "describes", "her", "typical", "morning", "routine"],
            )]

    token = await _login(client, Role.TEACHER, "ai-word-order@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her typical morning routine. " * 5
    await stored.save()

    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), FakeDrafter())
    try:
        response = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "word_order", "count": 1}, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 200, response.text
    assert response.json()["exercises"][0]["ordering_words"] == ["Maya", "describes", "her", "typical", "morning", "routine"]


async def test_ai_can_draft_a_grounded_error_correction_exercise(client):
    class FakeDrafter:
        async def draft(self, **kwargs):
            assert kwargs["kind"].value == "error_correction"
            return [LessonExerciseIn(
                kind="error_correction", prompt="Find and fix the mistake.",
                content="Maya describe her typical morning routine every day.",
                accepted_answers=["Maya describes her typical morning routine every day."],
                explanation="Third-person singular present simple needs -s.",
            )]

    token = await _login(client, Role.TEACHER, "ai-error-correction@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her typical morning routine every day. " * 5
    await stored.save()

    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), FakeDrafter())
    try:
        response = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "error_correction", "count": 1}, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 200, response.text
    assert response.json()["exercises"][0]["accepted_answers"] == ["Maya describes her typical morning routine every day."]


async def test_ai_can_draft_a_grounded_dialogue_completion_exercise(client):
    class FakeDrafter:
        async def draft(self, **kwargs):
            assert kwargs["kind"].value == "dialogue_completion"
            return [LessonExerciseIn(
                kind="dialogue_completion", prompt="Complete the dialogue about Maya's morning routine.",
                dialogue_turns=[
                    {"speaker": "Sam", "text": "What does Maya usually do every morning?"},
                    {"speaker": "Alex", "is_blank": True, "accepted_answers": ["She describes her typical morning routine every day."]},
                ],
            )]

    token = await _login(client, Role.TEACHER, "ai-dialogue@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her typical morning routine every day. " * 5
    await stored.save()

    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), FakeDrafter())
    try:
        response = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "dialogue_completion", "count": 1}, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 200, response.text
    assert response.json()["exercises"][0]["dialogue_turns"][1]["is_blank"] is True


async def test_a_persistently_low_quality_draft_is_rejected_not_retried_forever(client):
    """A drafter that NEVER produces a grounded exercise is rejected after
    exactly the bounded retry count - never saved, never an infinite loop."""
    class AlwaysBadDrafter:
        def __init__(self):
            self.attempts = 0

        async def draft(self, **kwargs):
            self.attempts += 1
            return [LessonExerciseIn(kind="reading", prompt="What is the capital of France?",
                                     content="Paris.", options=["Paris", "Lyon"], correct_option=0)]

    token = await _login(client, Role.TEACHER, "ai-exercise-always-bad@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her typical morning routine. " * 5
    await stored.save()

    drafter = AlwaysBadDrafter()
    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), drafter)
    try:
        response = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "reading", "count": 1}, headers=headers)
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
    assert response.status_code == 502, response.text
    assert "quality" in response.text.lower() or "ungrounded" in response.text.lower()
    # Bounded: exactly 2 attempts (MAX_DRAFT_ATTEMPTS), never an unbounded retry loop.
    assert drafter.attempts == 2
    assert client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()["lesson_exercises"] == []


async def test_learning_objectives_are_ordered_reviewed_and_gate_ai_drafting(client):
    """Objectives are teacher-only state: edits invalidate approval, while
    existing lesson work stays intact and dependent drafting is blocked."""
    token = await _login(client, Role.TEACHER, "objectives-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json={
        "title": "Directions", "level": "beginner",
        "lesson_exercises": [{"kind": "reflection", "prompt": "Describe your route."}],
    }, headers=headers).json()
    topic_id = created["id"]
    saved = client.put(f"/api/v1/topics/{topic_id}/objectives", json={"objectives": [
        {"text": "Ask for directions to a place.", "source": "manual"},
        {"text": "Give simple directions using left and right.", "source": "ai"},
    ]}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert [item["text"] for item in saved.json()["learning_objectives"]] == [
        "Ask for directions to a place.", "Give simple directions using left and right.",
    ]
    assert saved.json()["objectives_approved"] is False
    approved = client.post(f"/api/v1/topics/{topic_id}/objectives/approve", headers=headers)
    assert approved.status_code == 200
    assert approved.json()["objectives_approved"] is True
    # A re-order is a real curriculum change, so approval is explicitly lost;
    # the authored exercise survives untouched.
    reordered = client.put(f"/api/v1/topics/{topic_id}/objectives", json={"objectives": saved.json()["learning_objectives"][::-1]}, headers=headers)
    assert reordered.status_code == 200
    assert reordered.json()["objectives_approved"] is False
    assert reordered.json()["lesson_exercises"][0]["kind"] == "reflection"
    stored = await TopicContent.get(topic_id)
    stored.approved_transcript = "The museum is next to the bank. Turn left at the park. " * 3
    await stored.save()
    blocked = client.post(f"/api/v1/topics/{topic_id}/draft-exercises", json={"kind": "reading", "count": 1}, headers=headers)
    assert blocked.status_code == 422
    assert "objectives" in blocked.text.lower()


async def test_a_student_sees_learning_objectives_only_once_approved(client):
    """Course Content (Priority 2 of the E2E brief): a Student should see
    this lesson's approved Learning Objectives, never a draft - the field
    used to be internal-only (never sent to a Student at all, approved or
    not), which is why Course Content always looked empty."""
    from app.utils.time import utcnow

    teacher_token = await _login(client, Role.TEACHER, "objectives-student-view-teacher@example.com")
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    teacher_id = client.get("/api/v1/me", headers=teacher_headers).json()["id"]
    created = client.post("/api/v1/topics", json={"title": "Directions", "level": "intermediate"}, headers=teacher_headers).json()
    topic_id = created["id"]
    saved = client.put(f"/api/v1/topics/{topic_id}/objectives", json={"objectives": [
        {"text": "Ask for directions to a place.", "source": "manual"},
    ]}, headers=teacher_headers)
    assert saved.status_code == 200, saved.text

    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "T", "kind": "standard", "level": "intermediate", "days": [{"day": 1, "topic_ids": [topic_id]}]},
        headers=teacher_headers,
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers=teacher_headers)
    student_token = await _login(
        client, Role.STUDENT, "objectives-student-view@example.com",
        plan=PlanType.STANDARD, assigned_level=Level.INTERMEDIATE, program_start_date=utcnow(), created_by_id=teacher_id,
    )
    student_headers = {"Authorization": f"Bearer {student_token}"}

    before_approval = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers).json()
    assert before_approval["learning_objectives"] == [], "unapproved objectives must never reach a Student"

    approved = client.post(f"/api/v1/topics/{topic_id}/objectives/approve", headers=teacher_headers)
    assert approved.status_code == 200, approved.text

    after_approval = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers).json()
    assert [item["text"] for item in after_approval["learning_objectives"]] == ["Ask for directions to a place."]
    assert after_approval["objective_drafts"] == [], "drafts stay authoring-only regardless of approval"


async def test_objective_suggestions_are_transient_and_cost_controlled(client):
    class FakeObjectiveDrafter:
        last_usage = None

        async def draft(self, **kwargs):
            assert "bank" in kwargs["source"]
            return ["Ask for directions to the bank.", "Give directions using left and right."]

    teacher = await _login(client, Role.TEACHER, "objective-ai@example.com")
    other = await _login(client, Role.TEACHER, "objective-other@example.com")
    headers = {"Authorization": f"Bearer {teacher}"}
    topic = client.post("/api/v1/topics", json={"title": "Directions", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "The bank is beside the museum. Turn right after the park. " * 3
    await stored.save()
    app.dependency_overrides[get_objective_draft_service] = lambda: ObjectiveDraftService(
        get_topic_content_service(), FakeObjectiveDrafter()
    )
    try:
        assert client.post(f"/api/v1/topics/{topic['id']}/suggest-objectives", headers={"Authorization": f"Bearer {other}"}).status_code == 403
        response = client.post(f"/api/v1/topics/{topic['id']}/suggest-objectives", headers=headers)
    finally:
        app.dependency_overrides.pop(get_objective_draft_service, None)
    assert response.status_code == 200, response.text
    assert response.json() == [{"text": "Ask for directions to the bank."}, {"text": "Give directions using left and right."}]
    fetched = client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()
    assert fetched["learning_objectives"] == []
    assert len(fetched["objective_drafts"]) == 2
    accepted = client.post(f"/api/v1/topics/{topic['id']}/objective-drafts/0/accept", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["learning_objectives"][0]["source"] == "ai"
    rejected = client.post(f"/api/v1/topics/{topic['id']}/objective-drafts/0/reject", headers=headers)
    assert rejected.status_code == 200
    assert rejected.json()["objective_drafts"] == []


async def test_interactive_video_checkpoint_suggestions_stay_private_until_accepted(client):
    from app.core.interactive_video_drafting import TimelineProposal

    class FakeTimelineDrafter:
        last_usage = None

        def __init__(self):
            self.calls = 0

        async def draft(self, **kwargs):
            self.calls += 1
            assert "[0.0s-20.0s]" in kwargs["timestamped_source"]
            return [TimelineProposal(timestamp_seconds=8, exercise_index=0, completion_rule="submitted", rationale="The greeting is introduced here.")]

    teacher = await _login(client, Role.TEACHER, "interactive-ai@example.com")
    other = await _login(client, Role.TEACHER, "interactive-ai-other@example.com")
    headers = {"Authorization": f"Bearer {teacher}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "What greeting did you hear?", "options": ["Hello", "Bye"], "correct_option": 0}],
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    assert stored is not None
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.approved_transcript = "A greeting lesson with enough approved words for the teacher to review. " * 3
    stored.raw_transcript = [TranscriptSegment(text="Hello and welcome to the lesson.", start=0, duration=20)]
    await stored.save()

    drafter = FakeTimelineDrafter()
    app.dependency_overrides[get_interactive_video_draft_service] = lambda: InteractiveVideoDraftService(
        get_topic_content_service(), drafter
    )
    try:
        assert client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers={"Authorization": f"Bearer {other}"}).status_code == 403
        first = client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers=headers)
        second = client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers=headers)
    finally:
        app.dependency_overrides.pop(get_interactive_video_draft_service, None)
    assert first.status_code == 200, first.text
    assert first.json()[0]["exercise_index"] == 0
    assert second.status_code == 200 and drafter.calls == 1

    before_acceptance = client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()
    assert before_acceptance["interactive_video_checkpoints"] == []
    accepted = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/0/accept", headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["interactive_video_checkpoints"][0]["timestamp_seconds"] == 8
    assert accepted.json()["interactive_video_drafts"] == []
    # A Studio edit travels through the generic Topic PATCH route. It must
    # validate JSON-ready exercises as typed models, not crash on a dict.
    edited = client.patch(f"/api/v1/topics/{topic['id']}", headers=headers, json={
        "lesson_exercises": [{
            "kind": "multiple_choice", "prompt": "Which greeting did you hear?",
            "options": ["Hello", "Bye"], "correct_option": 0,
        }],
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["lesson_exercises"][0]["prompt"] == "Which greeting did you hear?"


async def test_interactive_video_ai_can_propose_an_editable_activity_and_checkpoint(client):
    """The teacher need not author every question before asking AI where it
    belongs.  The proposal stays private; accepting it creates a normal
    editable exercise plus its one linked checkpoint."""
    from app.core.interactive_video_drafting import TimelineProposal

    class FakeTimelineDrafter:
        last_usage = None

        async def draft(self, **kwargs):
            assert "[10.0s-30.0s]" in kwargs["timestamped_source"]
            return [TimelineProposal(
                timestamp_seconds=18,
                completion_rule="submitted",
                rationale="The speaker models the target greeting here.",
                exercise={
                    "kind": "writing", "prompt": "Write one greeting you could use at work.",
                    "min_words": 5,
                },
            )]

    token = await _login(client, Role.TEACHER, "interactive-ai-new-activity@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    assert stored is not None
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.approved_transcript = "A greeting lesson with enough approved words for the teacher to review. " * 3
    stored.raw_transcript = [TranscriptSegment(text="Hello and welcome to the lesson.", start=10, duration=20)]
    await stored.save()

    app.dependency_overrides[get_interactive_video_draft_service] = lambda: InteractiveVideoDraftService(
        get_topic_content_service(), FakeTimelineDrafter()
    )
    try:
        suggested = client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers=headers)
        assert suggested.status_code == 200, suggested.text
        assert suggested.json()[0]["exercise"]["kind"] == "writing"
        assert client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()["lesson_exercises"] == []
        accepted = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/0/accept", headers=headers)
    finally:
        app.dependency_overrides.pop(get_interactive_video_draft_service, None)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["lesson_exercises"][0]["prompt"] == "Write one greeting you could use at work."
    assert accepted.json()["interactive_video_checkpoints"][0]["exercise_index"] == 0


async def test_interactive_video_ai_can_propose_and_accept_a_practice_with_mariam_checkpoint(client):
    """A Gemini drafter mock returning a `checkpoint_kind="practice_with_mariam"`
    proposal is accepted into a real Practice with Mariam checkpoint with the
    right fields (no exercise attached); rejecting works the same as any other
    draft."""
    from app.core.interactive_video_drafting import TimelineProposal

    class FakePracticeDrafter:
        last_usage = None

        async def draft(self, **kwargs):
            return [TimelineProposal(
                timestamp_seconds=12,
                checkpoint_kind="practice_with_mariam",
                rationale="A short spoken round about the greeting just modeled.",
                learning_action="apply",
                learning_goal="Greet a new colleague appropriately.",
                source_evidence="The presenter models a formal workplace greeting.",
                conversation_goal="Have the student greet Mariam the way a new colleague would.",
                opening_line="Hi! Imagine we just met at work - how would you greet me?",
            )]

    token = await _login(client, Role.TEACHER, "interactive-ai-practice@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    assert stored is not None
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.approved_transcript = "A greeting lesson with enough approved words for the teacher to review. " * 3
    stored.raw_transcript = [TranscriptSegment(text="Hello and welcome to the lesson.", start=0, duration=20)]
    await stored.save()

    app.dependency_overrides[get_interactive_video_draft_service] = lambda: InteractiveVideoDraftService(
        get_topic_content_service(), FakePracticeDrafter()
    )
    try:
        suggested = client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers=headers)
        assert suggested.status_code == 200, suggested.text
        suggestion = suggested.json()[0]
        assert suggestion["checkpoint_kind"] == "practice_with_mariam"
        assert suggestion["exercise"] is None
        assert suggestion["conversation_goal"]
        assert suggestion["opening_line"]

        accepted = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/0/accept", headers=headers)
        assert accepted.status_code == 200, accepted.text
        checkpoint = accepted.json()["interactive_video_checkpoints"][0]
        assert checkpoint["kind"] == "practice_with_mariam"
        assert checkpoint["exercise_index"] is None
        assert checkpoint["conversation_goal"] == "Have the student greet Mariam the way a new colleague would."
        assert checkpoint["opening_line"] == "Hi! Imagine we just met at work - how would you greet me?"
        assert accepted.json()["lesson_exercises"] == []
    finally:
        app.dependency_overrides.pop(get_interactive_video_draft_service, None)


async def test_rejecting_a_practice_with_mariam_draft_adds_no_checkpoint(client):
    from app.core.interactive_video_drafting import TimelineProposal

    class FakePracticeDrafter:
        last_usage = None

        async def draft(self, **kwargs):
            return [TimelineProposal(
                timestamp_seconds=12, checkpoint_kind="practice_with_mariam",
                rationale="A short spoken round.", conversation_goal="Practice greeting.",
                opening_line="Hi there!",
            )]

    token = await _login(client, Role.TEACHER, "interactive-ai-practice-reject@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.approved_transcript = "A greeting lesson with enough approved words for the teacher to review. " * 3
    stored.raw_transcript = [TranscriptSegment(text="Hello and welcome to the lesson.", start=0, duration=20)]
    await stored.save()

    app.dependency_overrides[get_interactive_video_draft_service] = lambda: InteractiveVideoDraftService(
        get_topic_content_service(), FakePracticeDrafter()
    )
    try:
        client.post(f"/api/v1/topics/{topic['id']}/suggest-interactive-video-timeline", headers=headers)
        rejected = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/0/reject", headers=headers)
    finally:
        app.dependency_overrides.pop(get_interactive_video_draft_service, None)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["interactive_video_checkpoints"] == []
    assert rejected.json()["interactive_video_drafts"] == []


async def test_accept_all_interactive_video_drafts_accepts_every_valid_draft(client):
    from app.models.topic_content import CheckpointKind, InteractiveVideoCompletionRule, InteractiveVideoTimelineSuggestion

    token = await _login(client, Role.TEACHER, "accept-all@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [
            {"kind": "multiple_choice", "prompt": "Q1?", "options": ["A", "B"], "correct_option": 0},
            {"kind": "multiple_choice", "prompt": "Q2?", "options": ["A", "B"], "correct_option": 0},
            {"kind": "multiple_choice", "prompt": "Q3?", "options": ["A", "B"], "correct_option": 0},
        ],
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    assert stored is not None
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.interactive_video_drafts = [
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=5, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=0,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="r1",
        ),
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=10, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=1,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="r2",
        ),
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=15, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=2,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="r3",
        ),
    ]
    await stored.save()

    result = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/accept-all", headers=headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["accepted_count"] == 3
    assert body["failures"] == []

    refreshed = client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()
    assert refreshed["interactive_video_drafts"] == []
    assert len(refreshed["interactive_video_checkpoints"]) == 3
    assert sorted(cp["timestamp_seconds"] for cp in refreshed["interactive_video_checkpoints"]) == [5, 10, 15]


async def test_accept_all_interactive_video_drafts_reports_failures_and_keeps_successes(client):
    from app.models.topic_content import CheckpointKind, InteractiveVideoCompletionRule, InteractiveVideoTimelineSuggestion

    token = await _login(client, Role.TEACHER, "accept-all-partial@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [
            {"kind": "multiple_choice", "prompt": "Q1?", "options": ["A", "B"], "correct_option": 0},
            {"kind": "multiple_choice", "prompt": "Q2?", "options": ["A", "B"], "correct_option": 0},
        ],
    }, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    assert stored is not None
    stored.uploaded_video_url = "https://cdn.example.test/greetings.mp4"
    stored.interactive_video_drafts = [
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=5, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=0,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="r1",
        ),
        # Invalid: no such lesson exercise at index 99.
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=10, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=99,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="bad",
        ),
        InteractiveVideoTimelineSuggestion(
            timestamp_seconds=15, checkpoint_kind=CheckpointKind.EXERCISE, exercise_index=1,
            completion_rule=InteractiveVideoCompletionRule.SUBMITTED, rationale="r3",
        ),
    ]
    await stored.save()

    result = client.post(f"/api/v1/topics/{topic['id']}/interactive-video-drafts/accept-all", headers=headers)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["accepted_count"] == 2
    assert len(body["failures"]) == 1
    assert body["failures"][0]["index"] == 1
    assert body["failures"][0]["timestamp_seconds"] == 10
    assert body["failures"][0]["reason"]

    # Re-fetch and confirm the 2 valid ones are genuinely persisted, not rolled back.
    refreshed = client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()
    assert sorted(cp["timestamp_seconds"] for cp in refreshed["interactive_video_checkpoints"]) == [5, 15]
    assert refreshed["interactive_video_drafts"] == []


async def test_accept_all_interactive_video_drafts_rejects_wrong_role_and_wrong_teacher(client):
    owner_token = await _login(client, Role.TEACHER, "accept-all-owner@example.com")
    other_token = await _login(client, Role.TEACHER, "accept-all-other@example.com")
    student_token = await _login(client, Role.STUDENT, "accept-all-student@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=owner_headers).json()

    assert client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-drafts/accept-all",
        headers={"Authorization": f"Bearer {student_token}"},
    ).status_code == 403
    assert client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-drafts/accept-all",
        headers={"Authorization": f"Bearer {other_token}"},
    ).status_code == 403


async def test_teacher_can_convert_an_exercise_checkpoint_to_practice_with_mariam(client):
    token = await _login(client, Role.TEACHER, "convert-checkpoint@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "What greeting did you hear?", "options": ["Hello", "Bye"], "correct_option": 0}],
    }, headers=headers).json()
    put = client.put(f"/api/v1/topics/{topic['id']}/interactive-video-timeline", headers=headers, json={
        "checkpoints": [{"timestamp_seconds": 5, "exercise_index": 0, "completion_rule": "submitted"}],
    })
    assert put.status_code == 200, put.text
    checkpoint_id = put.json()["interactive_video_checkpoints"][0]["id"]

    converted = client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-timeline/{checkpoint_id}/convert-to-practice",
        headers=headers,
        json={
            "learning_objective": "Greet a colleague naturally.",
            "conversation_goal": "Practice a natural workplace greeting with Mariam.",
            "opening_line": "Hi! Let's practice greeting a new colleague.",
            "session_duration_minutes": 3,
        },
    )
    assert converted.status_code == 200, converted.text
    result = converted.json()["interactive_video_checkpoints"][0]
    assert result["id"] == checkpoint_id
    assert result["kind"] == "practice_with_mariam"
    assert result["exercise_index"] is None
    assert result["conversation_goal"] == "Practice a natural workplace greeting with Mariam."
    assert result["opening_line"] == "Hi! Let's practice greeting a new colleague."
    assert result["session_duration_minutes"] == 3
    # The exercise itself is untouched, just no longer linked.
    assert converted.json()["lesson_exercises"][0]["prompt"] == "What greeting did you hear?"


async def test_convert_checkpoint_unknown_topic_or_checkpoint_is_404(client):
    token = await _login(client, Role.TEACHER, "convert-checkpoint-404@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "conversation_goal": "Practice.", "opening_line": "Hi!", "session_duration_minutes": 2,
    }
    assert client.post(
        "/api/v1/topics/not-a-real-topic/interactive-video-timeline/not-a-real-checkpoint/convert-to-practice",
        headers=headers, json=body,
    ).status_code == 404

    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=headers).json()
    assert client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-timeline/not-a-real-checkpoint/convert-to-practice",
        headers=headers, json=body,
    ).status_code == 404


async def test_convert_checkpoint_rejects_wrong_role_and_wrong_teacher(client):
    owner_token = await _login(client, Role.TEACHER, "convert-checkpoint-owner@example.com")
    other_token = await _login(client, Role.TEACHER, "convert-checkpoint-other@example.com")
    student_token = await _login(client, Role.STUDENT, "convert-checkpoint-student@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Q", "options": ["a", "b"], "correct_option": 0}],
    }, headers=owner_headers).json()
    put = client.put(f"/api/v1/topics/{topic['id']}/interactive-video-timeline", headers=owner_headers, json={
        "checkpoints": [{"timestamp_seconds": 5, "exercise_index": 0, "completion_rule": "submitted"}],
    })
    checkpoint_id = put.json()["interactive_video_checkpoints"][0]["id"]
    body = {"conversation_goal": "Practice.", "opening_line": "Hi!", "session_duration_minutes": 2}

    assert client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-timeline/{checkpoint_id}/convert-to-practice",
        headers={"Authorization": f"Bearer {student_token}"}, json=body,
    ).status_code == 403
    assert client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-timeline/{checkpoint_id}/convert-to-practice",
        headers={"Authorization": f"Bearer {other_token}"}, json=body,
    ).status_code == 403


async def test_the_full_timeline_put_authors_and_edits_a_practice_with_mariam_checkpoint(client):
    """The existing full-array PUT - not a separate single-checkpoint PATCH -
    is how a teacher both authors a brand-new Practice with Mariam checkpoint
    and later edits its timestamp/goal/opening line/duration."""
    token = await _login(client, Role.TEACHER, "put-practice-checkpoint@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
    }, headers=headers).json()

    created = client.put(f"/api/v1/topics/{topic['id']}/interactive-video-timeline", headers=headers, json={
        "checkpoints": [{
            "timestamp_seconds": 6, "kind": "practice_with_mariam",
            "learning_objective": "Greet a colleague naturally.",
            "conversation_goal": "Practice a workplace greeting with Mariam.",
            "opening_line": "Hi! Let's practice greeting a colleague.",
            "session_duration_minutes": 2,
        }],
    })
    assert created.status_code == 200, created.text
    checkpoint = created.json()["interactive_video_checkpoints"][0]
    assert checkpoint["kind"] == "practice_with_mariam"
    assert checkpoint["exercise_index"] is None
    assert checkpoint["session_duration_minutes"] == 2
    checkpoint_id = checkpoint["id"]

    edited = client.put(f"/api/v1/topics/{topic['id']}/interactive-video-timeline", headers=headers, json={
        "checkpoints": [{
            "id": checkpoint_id, "timestamp_seconds": 9, "kind": "practice_with_mariam",
            "learning_objective": "Greet a colleague naturally.",
            "conversation_goal": "Updated goal: practice a formal workplace greeting.",
            "opening_line": "Hi! Nice to meet you - how would you greet me?",
            "session_duration_minutes": 4,
        }],
    })
    assert edited.status_code == 200, edited.text
    edited_checkpoint = edited.json()["interactive_video_checkpoints"][0]
    assert edited_checkpoint["id"] == checkpoint_id
    assert edited_checkpoint["timestamp_seconds"] == 9
    assert edited_checkpoint["conversation_goal"] == "Updated goal: practice a formal workplace greeting."
    assert edited_checkpoint["opening_line"] == "Hi! Nice to meet you - how would you greet me?"
    assert edited_checkpoint["session_duration_minutes"] == 4


async def test_convert_checkpoint_still_validates_missing_fields(client):
    token = await _login(client, Role.TEACHER, "convert-checkpoint-invalid@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={
        "title": "Video greetings", "level": "beginner", "lesson_flow": "interactive_video",
        "lesson_exercises": [{"kind": "multiple_choice", "prompt": "Q", "options": ["a", "b"], "correct_option": 0}],
    }, headers=headers).json()
    put = client.put(f"/api/v1/topics/{topic['id']}/interactive-video-timeline", headers=headers, json={
        "checkpoints": [{"timestamp_seconds": 5, "exercise_index": 0, "completion_rule": "submitted"}],
    })
    checkpoint_id = put.json()["interactive_video_checkpoints"][0]["id"]

    # Blank conversation_goal/opening_line are rejected by the request schema
    # itself (min_length=1).
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/interactive-video-timeline/{checkpoint_id}/convert-to-practice",
        headers=headers, json={"conversation_goal": "", "opening_line": "Hi!"},
    )
    assert resp.status_code == 422


async def test_exercise_draft_rejects_a_genuinely_concurrent_duplicate_request(client):
    """Slice B: simulates a true concurrent duplicate (two overlapping
    requests racing on the same generation_key) by pre-claiming the lock
    directly, the same way the service itself would mid-request - the
    second, still-overlapping request must be refused (409) rather than
    spending a second Gemini call, while a genuinely later request (after
    the first finishes) is never blocked - see test_ai_cost_control.py for
    the unit-level coverage of that distinction.
    """
    from app.core.ai_cost_control import claim_generation, compute_generation_key
    from app.core.config import get_settings as _get_settings
    from app.models.ai_generation import AIGenerationType

    token = await _login(client, Role.TEACHER, "ai-exercise-concurrent@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her morning routine. " * 5
    await stored.save()

    body = {"kind": "reading", "count": 1}
    bounded_source = stored.approved_transcript.strip()[: _get_settings().MAX_DRAFTING_SOURCE_CHARS]
    key = compute_generation_key(
        AIGenerationType.EXERCISE_DRAFT, topic_id=topic["id"], kind="reading", count=1, focus="", content="",
        source=bounded_source, model="gemini-3.5-flash",
    )
    claim = await claim_generation(
        generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model="gemini-3.5-flash", topic_id=topic["id"],
    )
    try:
        resp = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json=body, headers=headers)
        assert resp.status_code == 409
    finally:
        from app.core.ai_cost_control import complete_generation
        await complete_generation(claim, input_tokens=1, output_tokens=1)


async def test_exercise_draft_enforces_the_per_topic_daily_budget(client, monkeypatch):
    class FakeDrafter:
        async def draft(self, **kwargs):
            return [LessonExerciseIn(
                kind="reading", prompt="What does Maya do every morning in her routine?",
                content="Maya describes her morning routine in detail.",
                options=["Her morning routine", "Nothing at all"], correct_option=0,
            )]

    from app.core.config import get_settings as _get_settings

    monkeypatch.setenv("MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY", "1")
    _get_settings.cache_clear()

    token = await _login(client, Role.TEACHER, "ai-exercise-budget@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner"}, headers=headers).json()
    stored = await TopicContent.get(topic["id"])
    stored.approved_transcript = "Maya describes her morning routine. " * 5
    await stored.save()

    app.dependency_overrides[get_exercise_draft_service] = lambda: ExerciseDraftService(get_topic_content_service(), FakeDrafter())
    try:
        first = client.post(f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "reading", "count": 1}, headers=headers)
        assert first.status_code == 200
        second = client.post(
            f"/api/v1/topics/{topic['id']}/draft-exercises", json={"kind": "grammar", "count": 1}, headers=headers
        )
        assert second.status_code == 422
        assert "limit" in second.text.lower()
    finally:
        app.dependency_overrides.pop(get_exercise_draft_service, None)
        monkeypatch.delenv("MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY", raising=False)
        _get_settings.cache_clear()


async def test_student_topic_read_and_exercise_submission_never_call_an_ai_drafter(client, monkeypatch):
    """Slice B's student-side zero-generation guarantee: opening a lesson
    and submitting an exercise must never reach any AI drafting/review
    provider, regardless of how many students do it. Monkeypatches every
    drafting entry point to raise if called at all, then exercises the
    real student-facing read/submit paths (same setup as
    test_teacher_can_author_and_reorder_lesson_exercises above).
    """
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("student-facing request must never call an AI provider")

    import app.core.content_drafting as content_drafting
    import app.core.exercise_drafting as exercise_drafting
    import app.core.transcript_english_review as transcript_english_review

    monkeypatch.setattr(content_drafting.GeminiContentDrafter, "draft", _must_not_be_called)
    monkeypatch.setattr(content_drafting.GeminiContentDrafter, "translate_to_arabic", _must_not_be_called)
    monkeypatch.setattr(exercise_drafting.GeminiExerciseDrafter, "draft", _must_not_be_called)
    monkeypatch.setattr(transcript_english_review.GeminiTranscriptEnglishReviewer, "review", _must_not_be_called)

    from app.utils.time import utcnow

    token = await _login(client, Role.TEACHER, "ai-zero-gen-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json={"title": "Zero-gen lesson", "level": "beginner"}, headers=headers)
    topic_id = created.json()["id"]
    exercises = [
        {"kind": "multiple_choice", "prompt": "2 + 2?", "options": ["3", "4"], "correct_option": 1, "explanation": "Four."},
    ]
    updated = client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": exercises}, headers=headers)
    assert updated.status_code == 200
    revision = updated.json()["updated_at"]

    teacher_id = client.get("/api/v1/me", headers=headers).json()["id"]
    student_token = await _login(client, Role.STUDENT, "ai-zero-gen-student@example.com", plan=PlanType.STANDARD,
                                 assigned_level=Level.BEGINNER, program_start_date=utcnow(), created_by_id=teacher_id)
    student_headers = {"Authorization": f"Bearer {student_token}"}
    template = client.post("/api/v1/calendar-templates", json={"name": "Zero-gen plan", "kind": "standard", "level": "beginner",
                                                           "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=headers).status_code == 200

    read = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert read.status_code == 200

    submitted = client.post(
        "/api/v1/exercise-attempts",
        json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1},
        headers=student_headers,
    )
    assert submitted.status_code == 201
    assert submitted.json()["correct"] is True


async def test_creating_a_topic_rejects_a_playlist_url_instead_of_a_video_url(client):
    """A playlist link (`youtube.com/playlist?list=...`) passes the
    host/scheme allowlist but has no `v=` for any single video - it must be
    rejected here, with a message telling the admin what to do instead,
    rather than accepted and only failing much later, deep in the transcript
    pipeline.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={
            **TOPIC_PAYLOAD,
            "youtube_urls": ["https://www.youtube.com/playlist?list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "playlist" in resp.text.lower()


async def test_creating_a_topic_accepts_a_video_url_that_also_carries_playlist_params(client):
    """A video copied from inside a playlist (`watch?v=X&list=Y&index=N`)
    still has a real `v=` and must still work - only a *bare* playlist link
    with no video id at all should be rejected.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={
            **TOPIC_PAYLOAD,
            "youtube_urls": [
                "https://www.youtube.com/watch?v=n72v-tPu8FI&list=PLVk4pyGm9neFRgHDzqYj552aNbfuh653c&index=22"
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


async def test_student_cannot_create_a_topic(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_root_cannot_create_a_topic(client):
    """Content management is Instructor territory - Root's scope is
    deliberately narrow (create/reset Administrators only), not inherited
    Administrator powers.
    """
    token = await _login(client, Role.ROOT, "root@example.com")
    resp = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_administrator_list_includes_every_topic(client):
    """Administrators/Root manage content - never entitlement-gated, on the
    list or the single fetch. See tests/api/test_entitlement.py for how a
    Student's list is scoped (Kanban card #15).
    """
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()

    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {admin_token}"})
    assert listed.status_code == 200
    assert any(t["id"] == created["id"] for t in listed.json())


async def test_student_with_no_plan_sees_an_empty_topic_list(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"})

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {student_token}"})
    assert listed.status_code == 200
    assert listed.json() == []


async def test_administrator_can_fetch_any_topic(client):
    """Entitlement is a Student concept - Administrators manage content, so
    they're never gated by it.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = await _create_topic(client, token)

    fetched = client.get(f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {token}"})
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Talking about Music"


async def test_student_with_no_plan_cannot_fetch_any_topic(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = await _create_topic(client, admin_token)

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.get(f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 403


async def test_unknown_topic_is_404(client):
    token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.get("/api/v1/topics/not-a-real-id", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_administrator_can_partially_update_a_topic(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.patch(
        f"/api/v1/topics/{created['id']}",
        json={"title": "Talking about Jazz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Talking about Jazz"
    assert body["level"] == "intermediate"  # untouched fields survive a partial update


async def test_student_cannot_update_a_topic(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.patch(
        f"/api/v1/topics/{created['id']}",
        json={"title": "Hijacked"},
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert resp.status_code == 403


async def test_administrator_can_delete_a_topic_and_it_disappears_from_list(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    ).json()

    resp = client.delete(f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204

    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {token}"})
    assert not any(t["id"] == created["id"] for t in listed.json())

    fetched = client.get(f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {token}"})
    assert fetched.status_code == 404

    # Soft delete, not physical - the document itself is still there.
    raw = await TopicContent.get(created["id"])
    assert raw is not None
    assert raw.is_deleted is True


async def test_created_topic_can_be_submitted_against_end_to_end(client):
    """The point of this card: a submission needs a real topic_id, and there
    was previously no way to get one except inserting directly into the test
    DB. This proves the full chain - create a topic through the real API,
    then submit real (fake-storage-backed) audio against it.
    """
    from app.core.application import app
    from app.core.storage import UploadResult
    from app.services.recording_submission_service import get_recording_submission_service
    from app.repositories.recording_submission_repository import RecordingSubmissionRepository
    from app.repositories.topic_content_repository import TopicContentRepository
    from app.repositories.user_repository import UserRepository
    from app.services.recording_submission_service import RecordingSubmissionService

    class FakeAudioStorage:
        async def upload(self, file_bytes: bytes, public_id: str) -> UploadResult:
            return UploadResult(public_id=public_id, secure_url=f"https://cdn.example/{public_id}", duration_seconds=30.0)

        async def delete(self, public_id: str) -> None:
            pass

        def signed_url(self, public_id: str) -> str:
            return f"https://cdn.example/{public_id}?__cld_token__=signed"

    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    admin_id = client.get("/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    topic = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()

    # Entitled via an active calendar template covering this topic today:
    # `POST /submissions` applies the same rule as `GET /topics/{id}`, so an
    # unentitled/locked student would 403 here and this would stop testing
    # the chain it exists to test.
    from app.services.progress_service import get_progress_service
    from app.utils.time import utcnow

    student_token = await _login(
        client,
        Role.STUDENT,
        "student@example.com",
        plan=PlanType.STANDARD,
        assigned_level=Level.INTERMEDIATE,
        program_start_date=utcnow(),
        created_by_id=admin_id,
    )
    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "T", "kind": "standard", "level": "intermediate", "days": [{"day": 1, "topic_ids": [topic["id"]]}]},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers={"Authorization": f"Bearer {admin_token}"})
    app.dependency_overrides[get_recording_submission_service] = lambda: RecordingSubmissionService(
        RecordingSubmissionRepository(), TopicContentRepository(), FakeAudioStorage(), UserRepository(), get_progress_service()
    )
    try:
        resp = client.post(
            "/api/v1/submissions",
            data={"topic_id": topic["id"]},
            files={"file": ("rec.webm", io.BytesIO(b"fake-audio"), "audio/webm")},
            headers={"Authorization": f"Bearer {student_token}"},
        )
    finally:
        app.dependency_overrides.pop(get_recording_submission_service, None)

    assert resp.status_code == 200
    assert resp.json()["topic_id"] == topic["id"]
    assert resp.json()["label"] == "Talking about Music"


# --- Card #11: transcript pipeline ------------------------------------------


async def _create_topic(client, token) -> dict:
    return client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    ).json()


async def test_generate_transcript_reaches_pending_review(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    _override_transcript_service(transcriber=FakeTranscriber(text="hi there"))

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 202
    assert resp.json()["transcript_state"] == "extracting"

    final = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"})
    assert final.json()["transcript_state"] == "pending_review"
    assert final.json()["raw_transcript"] == [{"text": "hi there", "start": 0.0, "duration": 0.0}]


async def test_generate_transcript_student_forbidden(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token)

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert resp.status_code == 403


async def test_generate_transcript_unknown_topic_is_404(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/topics/not-a-real-id/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


async def test_generate_transcript_rejects_regenerating_an_approved_transcript(client):
    """`TestClient` runs `BackgroundTasks` synchronously, so by the time a
    request returns, `run_generation` has already finished - there's no way
    to observe "still extracting" through the HTTP layer to test the
    already-extracting conflict that way (that's covered directly at the
    service level, calling `start_generation` twice with no `run_generation`
    in between). What IS reachable here: `approved_for_ai` is terminal, so a
    regenerate attempt after approval must still be rejected.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    _override_transcript_service(transcriber=FakeTranscriber(text="hi"))
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})
    client.post(f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"})

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 409


async def test_generate_transcript_reverts_to_queued_on_gemini_failure(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    _override_transcript_service(transcriber=FakeTranscriber(error=TranscriptionError("gemini is down")))

    client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )

    final = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"})
    assert final.json()["transcript_state"] == "queued"


async def test_review_queue_filter_returns_only_pending_review_topics(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    ready_topic = await _create_topic(client, token)
    untouched_topic = client.post(
        "/api/v1/topics",
        json={**TOPIC_PAYLOAD, "title": "Talking about Travel"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    _override_transcript_service(transcriber=FakeTranscriber(text="hi"))
    client.post(
        f"/api/v1/topics/{ready_topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )

    resp = client.get(
        "/api/v1/topics?transcript_state=pending_review", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert ready_topic["id"] in ids
    assert untouched_topic["id"] not in ids


async def test_approve_transcript_freezes_approved_transcript(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    _override_transcript_service(transcriber=FakeTranscriber(text="Hello world."))
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["transcript_state"] == "approved_for_ai"
    assert resp.json()["approved_transcript"] == "Hello world."


async def test_approve_transcript_triggers_content_drafting(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    drafter = FakeContentDrafter(
        draft=DraftedContent(
            avatar_prompt="Discuss greetings.",
            opening_line="Hi! Ready to practice?",
            warm_up_questions=["What was the video about?"],
            vocabulary=[VocabularyItem(term="hello", theme="Greetings", example_sentence="Hello, how are you?")],
        )
    )
    source_text = "Hello world, this is today's lesson transcript for testing."
    _override_transcript_service(
        transcriber=FakeTranscriber(text=source_text),
        content_drafter=drafter,
    )
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    # `TestClient` runs `BackgroundTasks` synchronously within the request, so
    # the draft has already landed by the time this response returns.
    assert drafter.calls == [(source_text, Level.INTERMEDIATE, "Talking about Music")]

    fetched = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert fetched["vocabulary"] == [
        {"term": "hello", "theme": "Greetings", "example_sentence": "Hello, how are you?", "arabic_meaning": ""}
    ]
    assert fetched["avatar_prompt"] == "Discuss greetings."
    assert fetched["opening_line"] == "Hi! Ready to practice?"
    # `TOPIC_PAYLOAD` already gave this topic warm-up questions at creation -
    # drafting only fills fields still empty, so this one is untouched.
    assert fetched["warm_up_questions"] == TOPIC_PAYLOAD["warm_up_questions"]


async def test_draft_content_manually_fills_remaining_empty_fields(client):
    """The manual re-run endpoint - same gap-fill pass as the automatic
    post-approval one, callable again on demand (e.g. the first draft missed
    a field, or an admin wants another attempt at just what's still empty).
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    # No warm_up_questions this time, unlike TOPIC_PAYLOAD - every field
    # starts empty so the fill can be observed end to end.
    topic = client.post(
        "/api/v1/topics",
        json={"title": "Talking about Music", "level": "intermediate", "youtube_urls": TOPIC_PAYLOAD["youtube_urls"]},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    # First pass (the automatic one after approval) only manages to fill
    # vocabulary - a real, if rare, outcome (e.g. a malformed field from the
    # model) that the admin now wants to retry via the manual endpoint.
    first_drafter = FakeContentDrafter(
        draft=DraftedContent(vocabulary=[VocabularyItem(term="hello", theme="Greetings", example_sentence="Hi.")])
    )
    _override_transcript_service(
        transcriber=FakeTranscriber(text="Hello world, this is today's lesson transcript for testing."),
        content_drafter=first_drafter,
    )
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})
    client.post(f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"})

    after_first_pass = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert after_first_pass["vocabulary"] != []
    assert after_first_pass["avatar_prompt"] == ""
    assert after_first_pass["opening_line"] == ""
    assert after_first_pass["warm_up_questions"] == []

    # Manual re-run: a second drafter that would fill everything, including
    # vocabulary - but vocabulary is no longer empty, so it must be left
    # exactly as the first pass set it.
    second_drafter = FakeContentDrafter(
        draft=DraftedContent(
            avatar_prompt="Discuss greetings.",
            opening_line="Hi! Ready to practice?",
            warm_up_questions=["What was the video about?"],
            vocabulary=[VocabularyItem(term="goodbye", theme="Greetings", example_sentence="Goodbye!")],
        )
    )
    _override_transcript_service(content_drafter=second_drafter)

    resp = client.post(f"/api/v1/topics/{topic['id']}/draft-content", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 202

    # Like `approve-transcript`, the response reflects the topic as of the
    # synchronous half of the request - fetch again for what the background
    # task (run synchronously by `TestClient`, but after the response body
    # is already built) actually wrote.
    after_second_pass = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert after_second_pass["avatar_prompt"] == "Discuss greetings."
    assert after_second_pass["opening_line"] == "Hi! Ready to practice?"
    assert after_second_pass["warm_up_questions"] == ["What was the video about?"]
    assert after_second_pass["vocabulary"] == after_first_pass["vocabulary"]  # untouched - it wasn't empty


async def test_draft_content_before_approval_is_unprocessable(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    resp = client.post(f"/api/v1/topics/{topic['id']}/draft-content", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


async def test_draft_content_student_forbidden(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token)

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/draft-content", headers={"Authorization": f"Bearer {student_token}"}
    )
    assert resp.status_code == 403


async def test_approve_transcript_accepts_admin_edited_text(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    _override_transcript_service(
        transcriber=FakeTranscriber(text="Hello")
    )
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved_transcript": "Corrected wording."},
    )
    assert resp.status_code == 200
    assert resp.json()["approved_transcript"] == "Corrected wording."


async def test_approve_transcript_before_review_is_a_conflict(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 409


async def test_approve_transcript_student_forbidden(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token)

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert resp.status_code == 403


# --- review-transcript-english ------------------------------------------------


def _override_review_service(reviewer=None):
    app.dependency_overrides[get_transcript_review_service] = lambda: TranscriptReviewService(
        TopicContentRepository(), reviewer or FakeEnglishReviewer()
    )


@pytest.fixture(autouse=True)
def _clear_review_override():
    yield
    app.dependency_overrides.pop(get_transcript_review_service, None)


async def test_review_transcript_english_returns_corrections(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    reviewer = FakeEnglishReviewer(
        result=TranscriptReviewResult(
            corrected_transcript="Hello, world.",
            corrections=[
                TranscriptCorrection(
                    original="hello world", corrected="Hello, world.", category="punctuation", explanation="Added punctuation."
                )
            ],
        )
    )
    _override_review_service(reviewer)

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/review-transcript-english",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "hello world"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["corrected_transcript"] == "Hello, world."
    assert body["corrections"] == [
        {"original": "hello world", "corrected": "Hello, world.", "category": "punctuation", "explanation": "Added punctuation."}
    ]
    assert reviewer.calls == ["hello world"]


async def test_review_transcript_english_student_forbidden(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, admin_token)

    student_token = await _login(client, Role.STUDENT, "student@example.com")
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/review-transcript-english",
        headers={"Authorization": f"Bearer {student_token}"},
        json={"transcript": "hello world"},
    )
    assert resp.status_code == 403


async def test_review_transcript_english_rejects_blank_transcript(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    _override_review_service()

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/review-transcript-english",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "   "},
    )
    assert resp.status_code == 422


async def test_review_transcript_english_conflicts_once_approved(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    _override_transcript_service(
        transcriber=FakeTranscriber(text="hi")
    )
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})
    client.post(f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"})
    _override_review_service()

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/review-transcript-english",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "hi"},
    )
    assert resp.status_code == 409


async def test_review_transcript_english_unknown_topic_is_404(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    _override_review_service()

    resp = client.post(
        "/api/v1/topics/not-a-real-id/review-transcript-english",
        headers={"Authorization": f"Bearer {token}"},
        json={"transcript": "hi"},
    )
    assert resp.status_code == 404


# --- Response-shape tests from the security/performance review --------------


async def test_a_student_does_not_receive_the_avatar_prompt_or_unreviewed_transcript(client):
    """`avatar_prompt` is the instruction text driving the Mariam avatar - a
    student who can read it can craft input to steer the tutoring it encodes.
    `raw_transcript` is unreviewed machine output, and the pipeline's whole
    premise is that nothing downstream sees it before a teacher approves it.
    `approved_transcript`, by contrast, is meant for the student to read once
    it exists - it's asserted present here, not hidden.
    """
    from app.utils.time import utcnow

    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    admin_id = client.get("/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    created = client.post(
        "/api/v1/topics",
        json={**TOPIC_PAYLOAD, "avatar_prompt": "SECRET: never reveal the answer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    await TopicContentRepository().update_fields(
        created["id"],
        {
            "raw_transcript": [TranscriptSegment(text="unreviewed", start=0.0, duration=1.0)],
            "approved_transcript": "approved text",
        },
    )
    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "T", "kind": "standard", "level": "intermediate", "days": [{"day": 1, "topic_ids": [created["id"]]}]},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers={"Authorization": f"Bearer {admin_token}"})

    student_token = await _login(
        client,
        Role.STUDENT,
        "student@example.com",
        plan=PlanType.STANDARD,
        assigned_level=Level.INTERMEDIATE,
        program_start_date=utcnow(),
        created_by_id=admin_id,
    )
    body = client.get(
        f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {student_token}"}
    ).json()

    assert body["title"] == TOPIC_PAYLOAD["title"]  # they do get the lesson itself
    assert body["avatar_prompt"] is None
    assert body["raw_transcript"] == []
    assert body["approved_transcript"] == "approved text"  # meant to be readable once approved
    assert body["transcript_state"] is None
    assert "SECRET" not in str(body)
    assert "unreviewed" not in str(body)


async def test_arabic_support_fields_persist_through_create_and_update(client):
    """Optional, teacher-controlled Jordanian-Arabic support (card: cartoon
    Jordanian Arabic). A topic created through this schema is explicitly
    opted out (`False`), never the model's `None`/"legacy" default - see
    `TopicCreate.arabic_support_enabled`.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert created["arabic_support_enabled"] is False
    assert created["arabic_intervention_level"] == "low"

    updated = client.patch(
        f"/api/v1/topics/{created['id']}",
        json={"arabic_support_enabled": True, "arabic_intervention_level": "high"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert updated["arabic_support_enabled"] is True
    assert updated["arabic_intervention_level"] == "high"

    refetched = client.get(
        f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert refetched["arabic_support_enabled"] is True
    assert refetched["arabic_intervention_level"] == "high"


async def test_arabic_support_is_none_for_a_legacy_topic_with_no_field_recorded(client):
    """A topic inserted directly (bypassing `TopicCreate`, the same way a
    topic from before this feature existed sits in the real database) has no
    `arabic_support_enabled` field at all - Beanie fills in the model's
    `None` default on load, distinct from a teacher's explicit `False`."""
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    admin_id = client.get("/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    legacy_topic = TopicContent(title="Legacy Lesson", level=Level.INTERMEDIATE, created_by_id=admin_id)
    await legacy_topic.insert()

    body = client.get(
        f"/api/v1/topics/{legacy_topic.id}", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    assert body["arabic_support_enabled"] is None
    assert body["arabic_intervention_level"] == "low"


async def test_arabic_support_fields_are_not_exposed_to_students(client):
    """Same disclosure rule as `avatar_prompt`/`voice_prompt_mode` - teacher-
    authored agent configuration, not a student-facing control (no student
    input should ever influence this instruction block)."""
    from app.utils.time import utcnow

    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    admin_id = client.get("/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["id"]
    created = client.post(
        "/api/v1/topics",
        json={**TOPIC_PAYLOAD, "arabic_support_enabled": True, "arabic_intervention_level": "high"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    template = client.post(
        "/api/v1/calendar-templates",
        json={"name": "T", "kind": "standard", "level": "intermediate", "days": [{"day": 1, "topic_ids": [created["id"]]}]},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    client.post(f"/api/v1/calendar-templates/{template['id']}/activate", headers={"Authorization": f"Bearer {admin_token}"})

    student_token = await _login(
        client,
        Role.STUDENT,
        "student@example.com",
        plan=PlanType.STANDARD,
        assigned_level=Level.INTERMEDIATE,
        program_start_date=utcnow(),
        created_by_id=admin_id,
    )
    body = client.get(
        f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {student_token}"}
    ).json()
    assert body["arabic_support_enabled"] is None
    assert body["arabic_intervention_level"] is None


async def test_an_administrator_still_receives_every_internal_field(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics",
        json={**TOPIC_PAYLOAD, "avatar_prompt": "stay in character"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    await TopicContentRepository().update_fields(
        created["id"],
        {"raw_transcript": [TranscriptSegment(text="segment", start=0.0, duration=1.0)]},
    )

    body = client.get(
        f"/api/v1/topics/{created['id']}", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()

    assert body["avatar_prompt"] == "stay in character"
    assert [s["text"] for s in body["raw_transcript"]] == ["segment"]
    assert body["transcript_state"] == "queued"


async def test_the_topic_list_never_carries_transcripts(client):
    """A list row used to serialize the full transcript, which for a catalogue
    of lesson videos turned this endpoint into a multi-megabyte response.
    """
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    await TopicContentRepository().update_fields(
        created["id"],
        {"raw_transcript": [TranscriptSegment(text="a" * 500, start=0.0, duration=1.0)]},
    )

    rows = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {admin_token}"}).json()

    assert len(rows) == 1
    # Derived from the schema itself (plus BaseSchema's id/created_at/
    # updated_at) rather than a hand-maintained literal set, so a future
    # field added to TopicListItem doesn't make this assertion stale again -
    # see the prior hardcoded set, which failed this exact way once
    # author_name/has_video/has_audio/exercise_count/exercise_kinds were
    # added. The actual guarantee this test protects - no transcript text
    # ever reaches a list row - is still asserted explicitly below,
    # independent of whatever fields the schema happens to carry.
    assert set(rows[0]) == set(TopicListItem.model_fields) | {"id", "created_at", "updated_at"}
    assert "raw_transcript" not in rows[0]
    assert "approved_transcript" not in rows[0]
    assert "transcript" not in rows[0]
    assert "a" * 500 not in str(rows)


async def test_the_topic_list_is_paged(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    for index in range(5):
        client.post("/api/v1/topics", json={**TOPIC_PAYLOAD, "title": f"T{index}"}, headers=headers)

    assert len(client.get("/api/v1/topics?limit=2", headers=headers).json()) == 2
    assert len(client.get("/api/v1/topics?limit=2&skip=4", headers=headers).json()) == 1
    # An unbounded page size is not on offer.
    assert client.get("/api/v1/topics?limit=100000", headers=headers).status_code == 422


async def test_vocabulary_arabic_meaning_round_trips(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    body = client.patch(
        f"/api/v1/topics/{created['id']}",
        json={"vocabulary": [{"term": "melody", "theme": "Music", "example_sentence": "A catchy melody.", "arabic_meaning": "لحن"}]},
        headers=headers,
    ).json()

    assert body["vocabulary"] == [
        {"term": "melody", "theme": "Music", "example_sentence": "A catchy melody.", "arabic_meaning": "لحن"}
    ]


class FakeImageStorage:
    """Same shape as `tests/api/test_submissions.py`'s `FakeAudioStorage` -
    no real Cloudinary credentials in tests.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    async def upload(self, file_bytes: bytes, public_id: str) -> ImageUploadResult:
        if self.fail:
            raise UploadFailedError("simulated failure")
        self.uploaded.append(public_id)
        return ImageUploadResult(public_id=public_id, secure_url=f"https://cloudinary.example/{public_id}.jpg")

    async def delete(self, public_id: str) -> None:
        self.deleted.append(public_id)


def _override_image_storage(fake: FakeImageStorage):
    app.dependency_overrides[get_topic_content_service] = lambda: TopicContentService(
        TopicContentRepository(), get_progress_service(), image_storage=fake
    )


class FakeDirectVideoStorage:
    """No provider traffic: exercises the signed-upload handoff contract."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def issue_direct_video_upload(self, public_id: str) -> dict[str, str | int]:
        return {
            "timestamp": 123,
            "signature": "test-signature",
            "api_key": "public-key",
            "cloud_name": "test-cloud",
            "public_id": public_id,
        }

    def direct_video_url(self, public_id: str) -> str:
        return f"https://video.example/{public_id}.mp4"

    async def delete(self, public_id: str) -> None:
        self.deleted.append(public_id)


def _override_direct_video_storage(fake: FakeDirectVideoStorage):
    app.dependency_overrides[get_topic_content_service] = lambda: TopicContentService(
        TopicContentRepository(), get_progress_service(), video_storage=fake
    )


async def test_direct_source_video_signature_and_finalize_keep_topic_ownership(client):
    token = await _login(client, Role.TEACHER, "video-owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic = client.post("/api/v1/topics", json={"title": "Video source", "level": "beginner"}, headers=headers).json()
    fake = FakeDirectVideoStorage()
    _override_direct_video_storage(fake)
    try:
        signature = client.post(
            f"/api/v1/topics/{topic['id']}/source-video-signature",
            json={"filename": "lesson.mp4", "content_type": "video/mp4", "size_bytes": 42},
            headers=headers,
        )
        assert signature.status_code == 200, signature.text
        public_id = signature.json()["public_id"]
        assert public_id.startswith(f"Tamakkan/topic-videos/{topic['id']}-")
        saved = client.post(
            f"/api/v1/topics/{topic['id']}/source-video-finalize",
            json={"public_id": public_id, "filename": "lesson.mp4"},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["uploaded_video_filename"] == "lesson.mp4"
        assert saved.json()["uploaded_video_url"] == f"https://video.example/{public_id}.mp4"
        rejected = client.post(
            f"/api/v1/topics/{topic['id']}/source-video-finalize",
            json={"public_id": "Tamakkan/topic-videos/some-other-topic-x", "filename": "bad.mp4"},
            headers=headers,
        )
        assert rejected.status_code == 422
    finally:
        app.dependency_overrides.pop(get_topic_content_service, None)


async def test_topic_thumbnail_falls_back_to_the_derived_youtube_one(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    assert created["thumbnail_url"] == "https://img.youtube.com/vi/abc123/hqdefault.jpg"
    assert created["has_custom_thumbnail"] is False

    row = client.get("/api/v1/topics", headers=headers).json()[0]
    assert row["thumbnail_url"] == "https://img.youtube.com/vi/abc123/hqdefault.jpg"


async def test_uploading_a_thumbnail_overrides_the_derived_one(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    fake = FakeImageStorage()
    _override_image_storage(fake)
    try:
        body = client.post(
            f"/api/v1/topics/{created['id']}/thumbnail",
            files={"file": ("cover.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")},
            headers=headers,
        ).json()
    finally:
        app.dependency_overrides.pop(get_topic_content_service, None)

    assert body["has_custom_thumbnail"] is True
    assert body["thumbnail_url"] == f"https://cloudinary.example/{fake.uploaded[0]}.jpg"
    assert len(fake.uploaded) == 1
    assert fake.deleted == []  # nothing to replace yet


async def test_uploading_a_second_thumbnail_deletes_the_first_one(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    fake = FakeImageStorage()
    _override_image_storage(fake)
    try:
        client.post(
            f"/api/v1/topics/{created['id']}/thumbnail",
            files={"file": ("first.jpg", io.BytesIO(b"first"), "image/jpeg")},
            headers=headers,
        )
        client.post(
            f"/api/v1/topics/{created['id']}/thumbnail",
            files={"file": ("second.jpg", io.BytesIO(b"second"), "image/jpeg")},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_topic_content_service, None)

    assert len(fake.uploaded) == 2
    assert fake.deleted == [fake.uploaded[0]]


async def test_removing_a_thumbnail_reverts_to_the_derived_one(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    fake = FakeImageStorage()
    _override_image_storage(fake)
    try:
        client.post(
            f"/api/v1/topics/{created['id']}/thumbnail",
            files={"file": ("cover.jpg", io.BytesIO(b"cover"), "image/jpeg")},
            headers=headers,
        )
        body = client.delete(f"/api/v1/topics/{created['id']}/thumbnail", headers=headers).json()
    finally:
        app.dependency_overrides.pop(get_topic_content_service, None)

    assert body["has_custom_thumbnail"] is False
    assert body["thumbnail_url"] == "https://img.youtube.com/vi/abc123/hqdefault.jpg"
    assert fake.deleted == fake.uploaded  # the custom asset was cleaned up


async def test_a_non_image_thumbnail_upload_is_rejected(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/api/v1/topics", json=TOPIC_PAYLOAD, headers=headers).json()

    fake = FakeImageStorage()
    _override_image_storage(fake)
    try:
        resp = client.post(
            f"/api/v1/topics/{created['id']}/thumbnail",
            files={"file": ("cover.txt", io.BytesIO(b"not an image"), "text/plain")},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_topic_content_service, None)

    assert resp.status_code == 422
    assert fake.uploaded == []


# --- Teacher isolation (topic ownership) -------------------------------------


async def test_topic_created_by_an_administrator_is_owned_by_them(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    admin_id = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]
    created = await _create_topic(client, token)
    assert created["created_by_id"] == admin_id


async def test_teacher_b_does_not_see_teacher_a_topic_in_list(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)

    listed_b = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {token_b}"})
    assert listed_b.status_code == 200
    assert not any(t["id"] == topic_a["id"] for t in listed_b.json())

    listed_a = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {token_a}"})
    assert any(t["id"] == topic_a["id"] for t in listed_a.json())


async def test_teacher_b_cannot_fetch_teacher_a_topic_directly(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)

    resp = client.get(f"/api/v1/topics/{topic_a['id']}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 403

    own = client.get(f"/api/v1/topics/{topic_a['id']}", headers={"Authorization": f"Bearer {token_a}"})
    assert own.status_code == 200


async def test_teacher_b_cannot_update_teacher_a_topic(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)

    resp = client.patch(
        f"/api/v1/topics/{topic_a['id']}",
        json={"title": "Hijacked"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403

    own = client.patch(
        f"/api/v1/topics/{topic_a['id']}",
        json={"title": "Still mine"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert own.status_code == 200
    assert own.json()["title"] == "Still mine"


async def test_teacher_b_cannot_delete_teacher_a_topic(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)

    resp = client.delete(f"/api/v1/topics/{topic_a['id']}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 403

    own = client.delete(f"/api/v1/topics/{topic_a['id']}", headers={"Authorization": f"Bearer {token_a}"})
    assert own.status_code == 204


async def test_teacher_b_cannot_run_the_transcript_pipeline_on_teacher_a_topic(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)
    _override_transcript_service(transcriber=FakeTranscriber(text="hi"))

    resp = client.post(
        f"/api/v1/topics/{topic_a['id']}/generate-transcript", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 403


async def test_root_can_see_any_topic_unfiltered(client):
    """Root stands in for this feature's 'Admin may still see/manage all
    Topics' requirement - this codebase's actual role names are Administrator
    (the teacher-facing role being isolated here) and Root (the super-admin
    tier above it, see `app/models/user.py`'s module docstring). Root's own
    topic *write* access is a pre-existing, separate restriction (every write
    route is `require_administrator`-gated and already 403s Root - see
    `test_root_cannot_create_a_topic`); teacher isolation does not change
    that boundary, only what Root can read stays unfiltered either way.
    """
    teacher_token = await _login(client, Role.TEACHER, "teacher@example.com")
    root_token = await _login(client, Role.ROOT, "root@example.com")
    topic = await _create_topic(client, teacher_token)

    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {root_token}"})
    assert any(t["id"] == topic["id"] for t in listed.json())

    fetched = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {root_token}"})
    assert fetched.status_code == 200


async def test_legacy_topic_with_no_owner_is_visible_and_deletable_but_not_editable(client):
    """Production already has topics with no recorded author. They must stay
    visible to every Administrator rather than vanish from every roster,
    and no Administrator may edit one until Root explicitly re-owns it - see
    `TopicContentService.get_own_topic`. Deleting one is a deliberate
    exception: any Administrator may remove a shared topic they don't want,
    even though they can't edit it - see
    `TopicContentService.get_own_or_shared_topic_for_delete`.
    """
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    legacy = TopicContent(**{k: v for k, v in TOPIC_PAYLOAD.items()}, created_by_id=None)
    await legacy.insert()

    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {admin_token}"})
    assert any(t["id"] == legacy.id for t in listed.json())

    fetched = client.get(f"/api/v1/topics/{legacy.id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert fetched.status_code == 200

    updated = client.patch(
        f"/api/v1/topics/{legacy.id}", json={"title": "Hijacked legacy"}, headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert updated.status_code == 403

    deleted = client.delete(f"/api/v1/topics/{legacy.id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert deleted.status_code == 204

    # Soft delete - gone from listings, but still a real document underneath.
    raw = await TopicContent.get(legacy.id)
    assert raw is not None
    assert raw.is_deleted is True


async def test_deleting_a_shared_topic_removes_it_for_every_teacher(client):
    """A shared topic has no single owner - deleting it is a shared action
    with a shared consequence, not scoped to the deleting teacher's own
    view. Teacher B must lose it too, same as Teacher A would.
    """
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    legacy = TopicContent(**{k: v for k, v in TOPIC_PAYLOAD.items()}, created_by_id=None)
    await legacy.insert()

    deleted = client.delete(f"/api/v1/topics/{legacy.id}", headers={"Authorization": f"Bearer {token_a}"})
    assert deleted.status_code == 204

    listed_b = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {token_b}"})
    assert not any(t["id"] == legacy.id for t in listed_b.json())


# --- Video-optional lesson source (card #16: PDF content) --------------------


async def test_creating_a_topic_with_no_video_is_accepted(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    resp = client.post(
        "/api/v1/topics",
        json={"title": "PDF-only lesson", "level": "intermediate"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["youtube_urls"] == []


async def test_generate_transcript_with_no_sources_is_unprocessable(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = client.post(
        "/api/v1/topics",
        json={"title": "PDF-only lesson", "level": "intermediate"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


async def test_uploading_a_non_pdf_file_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    resp = client.post(
        f"/api/v1/topics/{topic['id']}/pdf",
        files={"file": ("notes.txt", io.BytesIO(b"not a pdf"), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_uploading_a_pdf_with_no_text_layer_is_rejected(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)

    scanned_pdf = b"%PDF-1.4\n%%EOF"  # too malformed to parse at all - same rejection path
    resp = client.post(
        f"/api/v1/topics/{topic['id']}/pdf",
        files={"file": ("scan.pdf", io.BytesIO(scanned_pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


class FakeDocumentStorage:
    """Same reasoning as `FakeImageStorage` - no real Cloudinary credentials
    in tests.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    async def upload(self, file_bytes: bytes, public_id: str) -> DocumentUploadResult:
        if self.fail:
            raise UploadFailedError("simulated failure")
        self.uploaded.append(public_id)
        return DocumentUploadResult(public_id=public_id, secure_url=f"https://cloudinary.example/{public_id}.pdf")

    async def delete(self, public_id: str) -> None:
        self.deleted.append(public_id)


def _override_document_storage(fake: FakeDocumentStorage):
    app.dependency_overrides[get_topic_content_service] = lambda: TopicContentService(
        TopicContentRepository(), get_progress_service(), document_storage=fake
    )


@pytest.fixture(autouse=True)
def _clear_document_storage_override():
    yield
    app.dependency_overrides.pop(get_topic_content_service, None)


async def test_pdf_only_topic_reaches_pending_review_and_is_approvable(client):
    """The whole point of card #16: a teacher can go from a PDF upload all
    the way to an AI-ready lesson with no video at any point.
    """
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = client.post(
        "/api/v1/topics",
        json={"title": "Workplace meetings", "level": "intermediate"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    _override_document_storage(FakeDocumentStorage())
    uploaded = client.post(
        f"/api/v1/topics/{topic['id']}/pdf",
        files={"file": ("meetings.pdf", io.BytesIO(build_minimal_pdf(b"Useful meeting phrases")), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["pdf_filename"] == "meetings.pdf"
    assert uploaded.json()["pdf_extracted_text"] == "Useful meeting phrases"

    listed = client.get("/api/v1/topics", headers={"Authorization": f"Bearer {token}"}).json()
    assert next(t for t in listed if t["id"] == topic["id"])["has_pdf"] is True

    generated = client.post(
        f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert generated.status_code == 202

    final = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert final["transcript_state"] == "pending_review"
    assert final["raw_transcript"] == [{"text": "Useful meeting phrases", "start": 0.0, "duration": 0.0}]

    approved = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"}
    )
    assert approved.status_code == 200
    assert approved.json()["approved_transcript"] == "Useful meeting phrases"


async def test_video_and_pdf_combine_into_one_approved_transcript(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)  # already has a youtube_url, see TOPIC_PAYLOAD

    _override_document_storage(FakeDocumentStorage())
    client.post(
        f"/api/v1/topics/{topic['id']}/pdf",
        files={"file": ("notes.pdf", io.BytesIO(build_minimal_pdf(b"Extra PDF context")), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    _override_transcript_service(transcriber=FakeTranscriber(text="Video transcript text"))
    client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers={"Authorization": f"Bearer {token}"})

    final = client.get(f"/api/v1/topics/{topic['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(final["raw_transcript"]) == 2
    texts = {segment["text"] for segment in final["raw_transcript"]}
    assert texts == {"Video transcript text", "Extra PDF context"}

    approved = client.post(
        f"/api/v1/topics/{topic['id']}/approve-transcript", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert "Video transcript text" in approved["approved_transcript"]
    assert "Extra PDF context" in approved["approved_transcript"]


async def test_removing_a_pdf_clears_its_fields(client):
    token = await _login(client, Role.TEACHER, "admin@example.com")
    topic = await _create_topic(client, token)
    _override_document_storage(FakeDocumentStorage())
    client.post(
        f"/api/v1/topics/{topic['id']}/pdf",
        files={"file": ("notes.pdf", io.BytesIO(build_minimal_pdf()), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )

    removed = client.delete(f"/api/v1/topics/{topic['id']}/pdf", headers={"Authorization": f"Bearer {token}"})
    assert removed.status_code == 200
    assert removed.json()["pdf_url"] is None
    assert removed.json()["pdf_extracted_text"] is None


async def test_teacher_b_cannot_upload_a_pdf_to_teacher_as_topic(client):
    token_a = await _login(client, Role.TEACHER, "teacher-a@example.com")
    token_b = await _login(client, Role.TEACHER, "teacher-b@example.com")
    topic_a = await _create_topic(client, token_a)

    resp = client.post(
        f"/api/v1/topics/{topic_a['id']}/pdf",
        files={"file": ("notes.pdf", io.BytesIO(build_minimal_pdf()), "application/pdf")},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403


async def test_multiple_document_and_text_sources_reach_review_and_approval(client):
    token = await _login(client, Role.TEACHER, "document-sources@example.com")
    topic = client.post(
        "/api/v1/topics", json={"title": "Work source lesson", "level": "intermediate"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    _override_document_storage(FakeDocumentStorage())
    headers = {"Authorization": f"Bearer {token}"}

    docx = client.post(
        f"/api/v1/topics/{topic['id']}/documents",
        files={"file": ("notes.docx", io.BytesIO(build_docx("Meeting vocabulary")), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=headers,
    )
    assert docx.status_code == 200
    assert docx.json()["source_documents"][0]["kind"] == "docx"

    pptx = client.post(
        f"/api/v1/topics/{topic['id']}/documents",
        files={"file": ("slides.pptx", io.BytesIO(build_pptx("Workplace phrases")), "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        headers=headers,
    )
    assert pptx.status_code == 200
    pasted = client.post(
        f"/api/v1/topics/{topic['id']}/text-source", json={"label": "Teacher notes", "text": "Ask about their daily routine."}, headers=headers,
    )
    assert pasted.status_code == 200
    assert len(pasted.json()["source_documents"]) == 3

    generated = client.post(f"/api/v1/topics/{topic['id']}/generate-transcript", headers=headers)
    assert generated.status_code == 202
    final = client.get(f"/api/v1/topics/{topic['id']}", headers=headers).json()
    assert final["transcript_state"] == "pending_review"
    assert {segment["text"] for segment in final["raw_transcript"]} == {
        "Meeting vocabulary", "Slide 1\nWorkplace phrases", "Ask about their daily routine.",
    }
    approved = client.post(f"/api/v1/topics/{topic['id']}/approve-transcript", headers=headers)
    assert approved.status_code == 200
    assert "Teacher notes" in approved.json()["approved_transcript"]


async def test_document_upload_rejects_unsupported_or_malformed_files(client):
    token = await _login(client, Role.TEACHER, "bad-document@example.com")
    topic = await _create_topic(client, token)
    _override_document_storage(FakeDocumentStorage())
    headers = {"Authorization": f"Bearer {token}"}
    unsupported = client.post(
        f"/api/v1/topics/{topic['id']}/documents", files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}, headers=headers,
    )
    assert unsupported.status_code == 422
    malformed = client.post(
        f"/api/v1/topics/{topic['id']}/documents", files={"file": ("notes.docx", io.BytesIO(b"not a docx"), "application/octet-stream")}, headers=headers,
    )
    assert malformed.status_code == 422


async def test_a_non_administrator_cannot_manage_thumbnails(client):
    admin_token = await _login(client, Role.TEACHER, "admin@example.com")
    created = client.post(
        "/api/v1/topics", json=TOPIC_PAYLOAD, headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    student_token = await _login(
        client, Role.STUDENT, "student@example.com", plan=PlanType.STANDARD, assigned_level=Level.INTERMEDIATE
    )
    headers = {"Authorization": f"Bearer {student_token}"}

    upload = client.post(
        f"/api/v1/topics/{created['id']}/thumbnail",
        files={"file": ("cover.jpg", io.BytesIO(b"cover"), "image/jpeg")},
        headers=headers,
    )
    remove = client.delete(f"/api/v1/topics/{created['id']}/thumbnail", headers=headers)

    assert upload.status_code == 403
    assert remove.status_code == 403
    assert client.get("/api/v1/topics?limit=0", headers=headers).status_code == 422
