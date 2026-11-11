"""Persisted lesson answers, trusted scoring, and teacher review boundaries."""

from datetime import timedelta

from app.core.security import hash_password
from app.models.topic_content import Level
from app.models.user import PlanType, Role, User
from app.utils.time import utcnow


async def _login(client, email: str, role: Role, **extra) -> tuple[str, str]:
    user = User(email=email, password_hash=hash_password("correct-password"), full_name=email.split("@")[0], role=role, **extra)
    await user.insert()
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct-password"})
    assert response.status_code == 200
    return response.json()["access_token"], user.id


async def test_student_answers_and_teacher_review_are_persisted_and_scoped(client):
    teacher_token, teacher_id = await _login(client, "lesson-teacher@example.com", Role.TEACHER)
    other_token, _ = await _login(client, "other-teacher@example.com", Role.TEACHER)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_token, _ = await _login(client, "lesson-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    outsider_token, _ = await _login(client, "other-student@example.com", Role.STUDENT)
    student_headers = {"Authorization": f"Bearer {student_token}"}
    exercises = [
        {"kind": "multiple_choice", "prompt": "Choose the greeting", "options": ["Hello", "Goodbye"], "correct_option": 0, "allow_resubmit": False},
        {"kind": "writing", "prompt": "Write two words", "min_words": 2, "deadline_at": (utcnow() + timedelta(days=1)).isoformat()},
        {"kind": "game", "prompt": "Match", "pairs": [{"left": "hello", "right": "مرحبا"}, {"left": "bye", "right": "وداعا"}]},
        {"kind": "writing", "prompt": "Expired response", "deadline_at": (utcnow() - timedelta(minutes=1)).isoformat()},
    ]
    topic_response = client.post("/api/v1/topics", json={"title": "Greetings", "level": "beginner", "lesson_exercises": exercises}, headers=teacher_headers)
    assert topic_response.status_code == 200
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert template.status_code == 200
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    assert student_topic.json()["lesson_exercises"][0]["correct_option"] is None
    revision = student_topic.json()["updated_at"]
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers={"Authorization": f"Bearer {outsider_token}"}).status_code == 403
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers=teacher_headers).status_code == 403
    choice = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 0}, headers=student_headers)
    assert choice.status_code == 201
    assert choice.json()["correct"] is True
    assert choice.json()["score"] == 100
    assert choice.json()["reviewed"] is True
    assert choice.json()["correct_option"] == 0
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1}, headers=student_headers).status_code == 409
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "one"}, headers=student_headers).status_code == 422
    writing = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "Hello teacher"}, headers=student_headers)
    assert writing.status_code == 201
    assert writing.json()["reviewed"] is False
    first_writing_id = writing.json()["id"]
    writing = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 1, "topic_updated_at": revision, "answer": "Good morning"}, headers=student_headers)
    assert writing.status_code == 201
    game = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 2, "topic_updated_at": revision, "answer": ["وداعا", "مرحبا"]}, headers=student_headers)
    assert game.status_code == 201
    assert game.json()["score"] == 0
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 3, "topic_updated_at": revision, "answer": "Too late"}, headers=student_headers).status_code == 403

    queue = client.get("/api/v1/exercise-attempts/review", headers=teacher_headers)
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()] == [writing.json()["id"]]
    assert client.get("/api/v1/exercise-attempts/review", headers={"Authorization": f"Bearer {other_token}"}).json() == []
    route = f"/api/v1/exercise-attempts/{writing.json()['id']}/review"
    assert client.patch(f"/api/v1/exercise-attempts/{first_writing_id}/review", json={"score": 50}, headers=teacher_headers).status_code == 404
    assert client.patch(route, json={"score": 85, "feedback": "Good start"}, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
    reviewed = client.patch(route, json={"score": 85, "feedback": "Good start"}, headers=teacher_headers)
    assert reviewed.status_code == 200
    assert reviewed.json()["reviewed"] is True
    mine = client.get(f"/api/v1/exercise-attempts/me?topic_id={topic_id}", headers=student_headers)
    assert mine.status_code == 200
    assert {item["exercise_index"] for item in mine.json()} == {0, 1, 2}
    assert next(item for item in mine.json() if item["exercise_index"] == 1)["feedback"] == "Good start"
    assert client.get("/api/v1/exercise-attempts/review", headers=teacher_headers).json() == []

    client.patch(f"/api/v1/topics/{topic_id}", json={"title": "Updated greetings"}, headers=teacher_headers)
    stale = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": 1}, headers=student_headers)
    assert stale.status_code == 409


async def test_word_order_exercise_is_authored_graded_and_persisted(client):
    """Slice 5: Sentence Building / Word Ordering - schema validation,
    deterministic grading (student must reconstruct the exact word order),
    resubmission, and teacher-authored vs. malformed answers."""
    teacher_token, teacher_id = await _login(client, "word-order-teacher@example.com", Role.TEACHER)
    student_token, _ = await _login(client, "word-order-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {"Authorization": f"Bearer {student_token}"}

    # Schema validation: too few words, and options on a word_order exercise, are both rejected.
    too_few = client.post("/api/v1/topics", json={
        "title": "Order", "level": "beginner",
        "lesson_exercises": [{"kind": "word_order", "prompt": "Order it", "ordering_words": ["one"]}],
    }, headers=teacher_headers)
    assert too_few.status_code == 422, too_few.text

    with_options = client.post("/api/v1/topics", json={
        "title": "Order", "level": "beginner",
        "lesson_exercises": [{"kind": "word_order", "prompt": "Order it", "ordering_words": ["I", "like", "tea"],
                              "options": ["a", "b"], "correct_option": 0}],
    }, headers=teacher_headers)
    assert with_options.status_code == 422, with_options.text

    topic_response = client.post("/api/v1/topics", json={
        "title": "Order", "level": "beginner",
        "lesson_exercises": [{"kind": "word_order", "prompt": "Put the sentence in order.",
                              "ordering_words": ["I", "like", "tea"], "allow_resubmit": True}],
    }, headers=teacher_headers)
    assert topic_response.status_code == 200, topic_response.text
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    revision = student_topic.json()["updated_at"]
    # SECURITY: the student-facing payload must NEVER contain the true
    # correct order verbatim - it's scrambled, and never submitted as-is
    # it must at least differ from the stored answer, with the exact same
    # words (a permutation, not different content).
    delivered_words = student_topic.json()["lesson_exercises"][0]["ordering_words"]
    assert sorted(delivered_words) == sorted(["I", "like", "tea"])
    assert delivered_words != ["I", "like", "tea"], "the correct order must not be sent to the student verbatim"

    # Malformed answers are rejected before any grading happens: wrong
    # length, a non-permutation (made-up words), and non-string items.
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["I", "like"]}, headers=student_headers).status_code == 422
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["I", "like", "coffee"]}, headers=student_headers).status_code == 422
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": [0, 1, 2]}, headers=student_headers).status_code == 422

    wrong = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["tea", "like", "I"]}, headers=student_headers)
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["correct"] is False
    assert wrong.json()["score"] == 0
    assert wrong.json()["answer_display"] == "tea like I"

    correct = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["I", "like", "tea"]}, headers=student_headers)
    assert correct.status_code == 201, correct.text
    assert correct.json()["correct"] is True
    assert correct.json()["score"] == 100
    assert correct.json()["answer_display"] == "I like tea"
    assert correct.json()["reviewed"] is True

    # The teacher's OWN view is unaffected - they still see/edit the true order.
    teacher_topic = client.get(f"/api/v1/topics/{topic_id}", headers=teacher_headers)
    assert teacher_topic.json()["lesson_exercises"][0]["ordering_words"] == ["I", "like", "tea"]


async def test_matching_game_does_not_leak_the_correct_pairing_and_is_graded_on_text(client):
    """Slice 5 security fix: `pairs[i].left`/`pairs[i].right` IS the correct
    match - a Student must never receive both lists in lockstep order."""
    teacher_token, teacher_id = await _login(client, "game-leak-teacher@example.com", Role.TEACHER)
    student_token, _ = await _login(client, "game-leak-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {"Authorization": f"Bearer {student_token}"}

    pairs = [{"left": "cat", "right": "qitta"}, {"left": "dog", "right": "kalb"}, {"left": "bird", "right": "ta'ir"}]
    topic_response = client.post("/api/v1/topics", json={
        "title": "Animals", "level": "beginner",
        "lesson_exercises": [{"kind": "game", "prompt": "Match the words.", "pairs": pairs}],
    }, headers=teacher_headers)
    assert topic_response.status_code == 200, topic_response.text
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    revision = student_topic.json()["updated_at"]
    delivered = student_topic.json()["lesson_exercises"][0]["pairs"]
    true_pairs = [(p["left"], p["right"]) for p in pairs]
    delivered_pairs = [(p["left"], p["right"]) for p in delivered]
    assert sorted(r for _, r in delivered_pairs) == sorted(r for _, r in true_pairs)
    assert delivered_pairs != true_pairs, "the true left/right correspondence must not be sent in lockstep order"
    assert [p["left"] for p in delivered] == ["cat", "dog", "bird"], "left order is not secret and must be unchanged"

    wrong = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["kalb", "qitta", "ta'ir"]}, headers=student_headers)
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["correct"] is False
    assert wrong.json()["score"] == 0

    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["qitta", "kalb"]}, headers=student_headers).status_code == 422
    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["qitta", "kalb", "made-up"]}, headers=student_headers).status_code == 422

    correct = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["qitta", "kalb", "ta'ir"]}, headers=student_headers)
    assert correct.status_code == 201, correct.text
    assert correct.json()["correct"] is True
    assert correct.json()["score"] == 100


async def test_error_correction_exercise_grades_with_normalization_and_hides_accepted_answers(client):
    teacher_token, teacher_id = await _login(client, "error-correction-teacher@example.com", Role.TEACHER)
    student_token, _ = await _login(client, "error-correction-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {"Authorization": f"Bearer {student_token}"}

    missing_accepted = client.post("/api/v1/topics", json={
        "title": "Grammar", "level": "beginner",
        "lesson_exercises": [{"kind": "error_correction", "prompt": "Fix it.", "content": "She go to school."}],
    }, headers=teacher_headers)
    assert missing_accepted.status_code == 422, missing_accepted.text

    topic_response = client.post("/api/v1/topics", json={
        "title": "Grammar", "level": "beginner",
        "lesson_exercises": [{"kind": "error_correction", "prompt": "Find and fix the mistake.",
                              "content": "She go to school every day.",
                              "accepted_answers": ["She goes to school every day.", "She goes to school every day"],
                              "explanation": "Third-person singular present simple needs -s.", "allow_resubmit": True}],
    }, headers=teacher_headers)
    assert topic_response.status_code == 200, topic_response.text
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    revision = student_topic.json()["updated_at"]
    assert student_topic.json()["lesson_exercises"][0]["accepted_answers"] == [], "accepted answers must never reach a Student"
    assert student_topic.json()["lesson_exercises"][0]["content"] == "She go to school every day."

    wrong = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": "She go to school everyday."}, headers=student_headers)
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["correct"] is False

    # Reasonable normalization: case, extra whitespace and trailing punctuation don't matter.
    correct = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": "  she  GOES to school every day  "}, headers=student_headers)
    assert correct.status_code == 201, correct.text
    assert correct.json()["correct"] is True
    assert correct.json()["score"] == 100


async def test_dialogue_completion_exercise_grades_each_blank_and_hides_accepted_answers(client):
    teacher_token, teacher_id = await _login(client, "dialogue-teacher@example.com", Role.TEACHER)
    student_token, _ = await _login(client, "dialogue-student@example.com", Role.STUDENT,
                                    plan=PlanType.STANDARD, assigned_level=Level.BEGINNER,
                                    program_start_date=utcnow(), created_by_id=teacher_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {"Authorization": f"Bearer {student_token}"}

    turns = [
        {"speaker": "Waiter", "text": "Good evening! Table for how many?"},
        {"speaker": "Guest", "is_blank": True, "accepted_answers": ["Table for two, please.", "A table for two, please."]},
        {"speaker": "Waiter", "text": "Right this way."},
        {"speaker": "Guest", "is_blank": True, "accepted_answers": ["Thank you."]},
    ]
    no_blanks = client.post("/api/v1/topics", json={
        "title": "Restaurant", "level": "beginner",
        "lesson_exercises": [{"kind": "dialogue_completion", "prompt": "Complete the dialogue.",
                              "dialogue_turns": [{"speaker": "A", "text": "Hi"}, {"speaker": "B", "text": "Hello"}]}],
    }, headers=teacher_headers)
    assert no_blanks.status_code == 422, no_blanks.text

    topic_response = client.post("/api/v1/topics", json={
        "title": "Restaurant", "level": "beginner",
        "lesson_exercises": [{"kind": "dialogue_completion", "prompt": "Complete the dialogue at the restaurant.", "dialogue_turns": turns}],
    }, headers=teacher_headers)
    assert topic_response.status_code == 200, topic_response.text
    topic_id = topic_response.json()["id"]
    template = client.post("/api/v1/calendar-templates", json={"name": "Week 1", "kind": "standard", "level": "beginner", "days": [{"day": 1, "topic_ids": [topic_id]}]}, headers=teacher_headers)
    assert client.post(f"/api/v1/calendar-templates/{template.json()['id']}/activate", headers=teacher_headers).status_code == 200

    student_topic = client.get(f"/api/v1/topics/{topic_id}", headers=student_headers)
    assert student_topic.status_code == 200
    revision = student_topic.json()["updated_at"]
    delivered_turns = student_topic.json()["lesson_exercises"][0]["dialogue_turns"]
    assert delivered_turns[0]["text"] == "Good evening! Table for how many?"
    assert all(turn["accepted_answers"] == [] for turn in delivered_turns), "accepted answers must never reach a Student"

    assert client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["only one"]}, headers=student_headers).status_code == 422

    partial = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["Table for two, please.", "wrong answer"]}, headers=student_headers)
    assert partial.status_code == 201, partial.text
    assert partial.json()["correct"] is False
    assert partial.json()["score"] == 50

    full = client.post("/api/v1/exercise-attempts", json={"topic_id": topic_id, "exercise_index": 0, "topic_updated_at": revision, "answer": ["A table for two, please.", "thank you"]}, headers=student_headers)
    assert full.status_code == 201, full.text
    assert full.json()["correct"] is True
    assert full.json()["score"] == 100
