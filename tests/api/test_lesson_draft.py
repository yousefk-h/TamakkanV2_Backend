"""Slice C: the unified "Generate Lesson Draft" teacher action -
app.services.lesson_draft_service.LessonDraftService, reached over
POST /topics/{id}/generate-lesson-draft and the accept/reject/regenerate
routes for its pending exercise suggestions. Reuses the exact same fakes as
the transcript pipeline and exercise draft tests, never a real provider.
"""

from datetime import timedelta

from app.core.application import app
from app.core.content_drafting import DraftedContent
from app.core.security import hash_password
from app.models.topic_content import ExerciseKind, Level, TopicContent, VocabularyItem
from app.models.user import Role, User
from app.repositories.topic_content_repository import TopicContentRepository
from app.schema.topic import LessonExerciseIn
from app.services.exercise_draft_service import ExerciseDraftingError
from app.services.lesson_draft_service import LessonDraftService, get_lesson_draft_service
from app.services.topic_content_service import get_topic_content_service
from app.services.transcript_pipeline_service import TranscriptPipelineService, get_transcript_pipeline_service
from tests.services.test_transcript_pipeline_service import FakeContentDrafter, FakeTranscriber

APPROVED_SOURCE = "Maya describes her typical morning routine in detail. " * 5


class FakeExerciseDrafter:
    def __init__(self, error=None):
        self.error = error
        self.calls: list[ExerciseKind] = []
        self.last_usage = None

    # Deliberately distinct per kind (not just a different kind-name word
    # with otherwise-identical content) - a real drafted batch must not
    # produce near-duplicate exercises across different skills, and
    # Slice 4's quality gate now checks for exactly that.
    _BY_KIND = {
        "multiple_choice": ("Which activity is part of Maya's typical morning routine?",
                             "Maya describes her typical morning routine in the passage.",
                             ["Making coffee", "Going to the gym"]),
        "reading": ("What time does Maya usually start her typical morning routine?",
                    "Maya wakes up early and reviews her schedule before heading out.",
                    ["Early in the morning", "Late at night"]),
        "grammar": ("Which verb tense does Maya use to describe her typical morning routine?",
                    "Maya's routine is described using the present simple tense.",
                    ["Present simple", "Past continuous"]),
    }

    async def draft(self, *, title, level, source, kind, count, focus, content):
        self.calls.append(kind)
        if self.error is not None:
            raise self.error
        prompt, body, options = self._BY_KIND.get(
            kind.value, (f"What does Maya's typical morning routine show about her {kind.value} skills?",
                         "Maya describes her typical morning routine in detail.",
                         ["Her morning routine", "Nothing relevant"]),
        )
        return [LessonExerciseIn(kind=kind, prompt=prompt, content=body, options=options, correct_option=0)]


def _override_lesson_draft_service(content_drafter=None, exercise_drafter=None):
    transcript_pipeline = TranscriptPipelineService(
        TopicContentRepository(), FakeTranscriber(), content_drafter or FakeContentDrafter(),
        extraction_timeout=timedelta(minutes=30),
    )
    service = LessonDraftService(
        TopicContentRepository(), get_topic_content_service(), transcript_pipeline,
        exercise_drafter or FakeExerciseDrafter(),
    )
    app.dependency_overrides[get_lesson_draft_service] = lambda: service
    return service


async def _login(client, role: Role, email: str, **extra) -> str:
    user = User(email=email, password_hash=hash_password("correct-password"), full_name="Test User", role=role, **extra)
    await user.insert()
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"})
    return resp.json()["access_token"]


async def _topic_with_approved_source(client, headers, **overrides) -> str:
    created = client.post("/api/v1/topics", json={"title": "Routine", "level": "beginner", **overrides}, headers=headers)
    topic_id = created.json()["id"]
    stored = await TopicContent.get(topic_id)
    stored.approved_transcript = APPROVED_SOURCE
    await stored.save()
    return topic_id


async def test_generate_lesson_draft_fills_content_and_drafts_default_exercise_kinds(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-teacher@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(client, headers)

    exercise_drafter = FakeExerciseDrafter()
    _override_lesson_draft_service(exercise_drafter=exercise_drafter)
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 202, resp.text
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    final = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    assert final["avatar_prompt"] != ""
    assert final["vocabulary"] != []
    assert final["lesson_draft_job"]["state"] == "done"
    assert final["lesson_draft_job"]["content_step"] == "done"
    assert set(final["lesson_draft_job"]["exercise_steps"].values()) == {"done"}
    assert len(final["draft_exercises"]) == 3
    assert {item["kind"] for item in final["draft_exercises"]} == {"multiple_choice", "reading", "grammar"}
    # Nothing was published without explicit teacher approval.
    assert final["lesson_exercises"] == []
    assert sorted(exercise_drafter.calls, key=lambda k: k.value) == sorted(
        [ExerciseKind.MULTIPLE_CHOICE, ExerciseKind.READING, ExerciseKind.GRAMMAR], key=lambda k: k.value
    )


async def test_generate_lesson_draft_skips_content_step_when_already_filled(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-skip-content@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(
        client, headers,
        avatar_prompt="Already written.", opening_line="Hi!", warm_up_questions=["Q?"],
    )
    stored = await TopicContent.get(topic_id)
    stored.vocabulary = [VocabularyItem(term="hello", theme="Greetings", example_sentence="Hi.")]
    await stored.save()

    content_drafter = FakeContentDrafter()
    _override_lesson_draft_service(content_drafter=content_drafter)
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 202
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    final = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    assert final["lesson_draft_job"]["content_step"] == "skipped"
    assert content_drafter.calls == []  # never called - nothing was empty


async def test_generate_lesson_draft_never_drafts_a_kind_the_teacher_already_authored(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-no-dup@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(client, headers)
    client.patch(f"/api/v1/topics/{topic_id}", json={"lesson_exercises": [
        {"kind": "multiple_choice", "prompt": "Manual question", "options": ["A", "B"], "correct_option": 0},
    ]}, headers=headers)

    exercise_drafter = FakeExerciseDrafter()
    _override_lesson_draft_service(exercise_drafter=exercise_drafter)
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 202
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    assert ExerciseKind.MULTIPLE_CHOICE not in exercise_drafter.calls
    final = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    # The manually-authored multiple_choice exercise is untouched.
    assert final["lesson_exercises"] == [
        {**final["lesson_exercises"][0], "kind": "multiple_choice", "prompt": "Manual question"},
    ]
    assert {item["kind"] for item in final["draft_exercises"]} == {"reading", "grammar"}


async def test_a_failed_exercise_kind_does_not_block_the_others_and_can_be_retried(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-partial-fail@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(
        client, headers, avatar_prompt="X", opening_line="X", warm_up_questions=["X"],
    )
    stored = await TopicContent.get(topic_id)
    stored.vocabulary = [VocabularyItem(term="x", theme="x", example_sentence="x")]
    await stored.save()

    failing_drafter = FakeExerciseDrafter(error=ExerciseDraftingError("boom"))
    _override_lesson_draft_service(exercise_drafter=failing_drafter)
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 202
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    after_failure = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    assert after_failure["lesson_draft_job"]["state"] == "failed"
    assert set(after_failure["lesson_draft_job"]["exercise_steps"].values()) == {"failed"}
    assert after_failure["draft_exercises"] == []

    # Retry: a working drafter now succeeds, with no record of the earlier
    # failed attempts blocking it (failed attempts are never appended to
    # draft_exercises, so replanning sees all three kinds as still needed).
    working_drafter = FakeExerciseDrafter()
    _override_lesson_draft_service(exercise_drafter=working_drafter)
    try:
        retry = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert retry.status_code == 202
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    after_retry = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
    assert after_retry["lesson_draft_job"]["state"] == "done"
    assert len(after_retry["draft_exercises"]) == 3


async def test_generate_lesson_draft_rejects_a_concurrent_duplicate_start(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-concurrent@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(client, headers)

    from app.models.topic_content import LessonDraftJob, LessonDraftStepState
    from app.repositories.topic_content_repository import TopicContentRepository as Repo
    await Repo().update_fields(topic_id, {
        "lesson_draft_job": LessonDraftJob(state=LessonDraftStepState.RUNNING).model_dump(),
    })

    _override_lesson_draft_service()
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 409
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)


async def test_generate_lesson_draft_enforces_the_daily_budget_before_any_provider_call(client, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY", "1")
    get_settings.cache_clear()

    token = await _login(client, Role.TEACHER, "lesson-draft-budget@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(client, headers)

    exercise_drafter = FakeExerciseDrafter()
    _override_lesson_draft_service(exercise_drafter=exercise_drafter)
    try:
        resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        assert resp.status_code == 422
        assert "AI requests" in resp.text
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)
        monkeypatch.delenv("MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY", raising=False)
        get_settings.cache_clear()
    assert exercise_drafter.calls == []


async def test_teacher_can_accept_reject_and_regenerate_individual_suggestions(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-review@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(
        client, headers, avatar_prompt="X", opening_line="X", warm_up_questions=["X"],
    )
    stored = await TopicContent.get(topic_id)
    stored.vocabulary = [VocabularyItem(term="x", theme="x", example_sentence="x")]
    await stored.save()

    _override_lesson_draft_service(exercise_drafter=FakeExerciseDrafter())
    try:
        client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
        before = client.get(f"/api/v1/topics/{topic_id}", headers=headers).json()
        assert len(before["draft_exercises"]) == 3

        accepted_kind = before["draft_exercises"][0]["kind"]
        accept_resp = client.post(f"/api/v1/topics/{topic_id}/draft-exercises/0/accept", headers=headers)
        assert accept_resp.status_code == 200
        after_accept = accept_resp.json()
        assert len(after_accept["draft_exercises"]) == 2
        assert [item["kind"] for item in after_accept["lesson_exercises"]] == [accepted_kind]

        reject_resp = client.post(f"/api/v1/topics/{topic_id}/draft-exercises/0/reject", headers=headers)
        assert reject_resp.status_code == 200
        assert len(reject_resp.json()["draft_exercises"]) == 1

        remaining_kind = reject_resp.json()["draft_exercises"][0]["kind"]
        regenerate_resp = client.post(f"/api/v1/topics/{topic_id}/draft-exercises/0/regenerate", headers=headers)
        assert regenerate_resp.status_code == 200
        regenerated = regenerate_resp.json()["draft_exercises"][0]
        assert regenerated["kind"] == remaining_kind
        # Still just one accepted exercise - regenerating a suggestion never
        # touches the already-published list.
        assert len(regenerate_resp.json()["lesson_exercises"]) == 1
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)


async def test_a_student_can_never_see_draft_exercises_or_the_job_status(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-student-view@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(
        client, headers, avatar_prompt="X", opening_line="X", warm_up_questions=["X"],
    )
    stored = await TopicContent.get(topic_id)
    stored.vocabulary = [VocabularyItem(term="x", theme="x", example_sentence="x")]
    await stored.save()
    _override_lesson_draft_service(exercise_drafter=FakeExerciseDrafter())
    try:
        client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft", headers=headers)
    finally:
        app.dependency_overrides.pop(get_lesson_draft_service, None)

    from app.models.user import PlanType
    from app.utils.time import utcnow
    teacher_id = client.get("/api/v1/me", headers=headers).json()["id"]
    student_token = await _login(client, Role.STUDENT, "lesson-draft-student@example.com", plan=PlanType.STANDARD,
                                 assigned_level=Level.BEGINNER, program_start_date=utcnow(), created_by_id=teacher_id)
    template = client.post("/api/v1/calendar-templates", json={"name": "P", "kind": "standard", "level": "beginner",
                                                           "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=headers).status_code == 200

    student_resp = client.get(f"/api/v1/topics/{topic_id}", headers={"Authorization": f"Bearer {student_token}"})
    assert student_resp.status_code == 200, student_resp.text
    student_view = student_resp.json()
    assert student_view["draft_exercises"] == []
    assert student_view["lesson_draft_job"] is None


async def test_student_cannot_call_generate_lesson_draft(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    topic_id = await _topic_with_approved_source(client, headers)
    student_token = await _login(client, Role.STUDENT, "lesson-draft-forbidden-student@example.com")
    resp = client.post(f"/api/v1/topics/{topic_id}/generate-lesson-draft",
                       headers={"Authorization": f"Bearer {student_token}"})
    assert resp.status_code == 403


async def test_generate_lesson_draft_requires_an_approved_transcript(client):
    token = await _login(client, Role.TEACHER, "lesson-draft-no-transcript@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/topics", json={"title": "No source yet", "level": "beginner"}, headers=headers)
    resp = client.post(f"/api/v1/topics/{created.json()['id']}/generate-lesson-draft", headers=headers)
    assert resp.status_code == 422
