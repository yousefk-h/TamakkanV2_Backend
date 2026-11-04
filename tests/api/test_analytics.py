"""Analytics use isolated test records; no production database or AI calls."""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.learning_analysis import LearningAnalysis, TranscriptMetrics
from app.models.live_session import LiveSession, SessionState, SpeakingReport
from app.models.recording_submission import RecordingSubmission, SubmissionKind
from app.models.user import Role, User

NOW = datetime(2026, 9, 16, 12)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr("app.services.analytics_service.utcnow", lambda: NOW)


async def user(email="teacher@example.com", role=Role.TEACHER, **extra):
    item = User(email=email, role=role, password_hash="unused", full_name="Test", **extra)
    await item.insert()
    return item


def headers(item):
    return {"Authorization": f"Bearer {create_access_token(item.id, item.role)}"}


def report(score=80, *, level="intermediate", legacy=False, reviewed=False):
    analysis = None if legacy else LearningAnalysis(
        status="assessed" if score is not None else "insufficient_evidence", target_level=level,
        metrics=TranscriptMetrics(student_turns=3, student_words=40, unique_student_words=30, average_words_per_turn=13.3),
    )
    return SpeakingReport(fluency_score=score, vocabulary_score=score, grammar_score=score, complexity_score=score,
                          overall_score=score, summary="Test", generated_at=NOW, reviewed=reviewed, analysis=analysis)


async def session(student, *, start=None, minutes=10, topic="lesson-a", **extra):
    start = start or NOW - timedelta(hours=1)
    values = dict(student=student, student_id=student.id, topic_id=topic, state=SessionState.ENDED,
                  started_at=start, ended_at=start + timedelta(minutes=minutes))
    values.update(extra)
    item = LiveSession(**values)
    await item.insert()
    return item


async def recording(student, *, at=None, **extra):
    item = RecordingSubmission(student=student, student_id=student.id, kind=SubmissionKind.END_OF_WEEK_VIDEO,
                               day=7, label="Test", media_url="https://example.invalid/test", cloudinary_public_id="test",
                               duration_seconds=60, submitted_at=at or NOW - timedelta(hours=2), **extra)
    await item.insert()
    return item


async def test_empty_roster_zero_activity_but_no_invented_scores_or_retention(client):
    admin = await user()
    response = client.get("/api/v1/analytics?days=7", headers=headers(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["completed_sessions"] == body["active_students"] == body["roster_students"] == 0
    assert body["returning_rate"] is None and body["score_groups"] == []
    assert [d["date"] for d in body["daily"]] == [f"2026-09-{d}" for d in range(10, 17)]
    assert all(d["active_students"] == 0 for d in body["daily"])


async def test_totals_deduplicate_students_and_topics_and_keep_rubrics_separate(client):
    admin = await user()
    a = await user("a@example.com", Role.STUDENT, created_by_id=admin.id)
    b = await user("b@example.com", Role.STUDENT, created_by_id=admin.id, is_active=False)
    await session(a, report=report(80))
    await session(a, minutes=5, report=report(60, reviewed=True))
    await session(b, topic="lesson-b", report=report(90, level="advanced"))
    await session(a, minutes=2, report=report(None))
    await session(a, minutes=3, report=report(0, legacy=True))
    await session(a, minutes=4)  # completed but no report
    await recording(a)
    await recording(a, reviewed=True)  # each uploaded attempt counts
    response = client.get("/api/v1/analytics?days=7", headers=headers(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["active_students"], body["roster_students"], body["completed_sessions"], body["topics_practiced"]) == (2, 2, 6, 2)
    assert body["session_minutes"] == 34 and body["submissions"] == 2
    assert body["reports_available"] == 5 and body["unscored_sessions"] == 2
    assert body["reports_awaiting_review"] == 4 and body["submissions_awaiting_review"] == 1
    groups = {(g["rubric"], g["level"]): g for g in body["score_groups"]}
    assert groups[("tamakkan-text-v1", "intermediate")]["average_score"] == 70
    assert groups[("tamakkan-text-v1", "intermediate")]["report_count"] == 2
    assert groups[("tamakkan-text-v1", "intermediate")]["student_count"] == 1
    assert groups[("legacy", None)]["average_score"] == 0  # zero is a real score
    assert body["daily"][-1]["active_students"] == 2
    assert sum(d["completed_sessions"] for d in body["daily"]) == 6
    assert "transcript" not in response.text and "media_url" not in response.text and "@" not in response.text


async def test_utc_boundaries_previous_cohort_and_range_change(client):
    admin = await user()
    a = await user("a@example.com", Role.STUDENT, created_by_id=admin.id)
    b = await user("b@example.com", Role.STUDENT, created_by_id=admin.id)
    await session(a, start=datetime(2026, 9, 3))  # exact previous start
    await recording(b, at=datetime(2026, 9, 9, 23, 59, 59))
    await session(a, start=datetime(2026, 9, 10))  # exact current start
    await recording(a, at=NOW)  # today's endpoint is inclusive
    await session(b, start=datetime(2026, 9, 2, 23, 59))  # outside both 7-day periods
    body = client.get("/api/v1/analytics?days=7", headers=headers(admin)).json()
    assert body["previous_active_students"] == 2 and body["returning_students"] == 1 and body["returning_rate"] == 50
    assert body["completed_sessions"] == 1 and body["submissions"] == 1
    monthly = client.get("/api/v1/analytics?days=30", headers=headers(admin)).json()
    assert monthly["completed_sessions"] == 3 and monthly["submissions"] == 2
    assert len(monthly["daily"]) == 30 and monthly["start_date"] == "2026-08-18"


async def test_excludes_failed_deleted_future_invalid_and_foreign_records(client):
    admin = await user()
    a = await user("a@example.com", Role.STUDENT, created_by_id=admin.id)
    outsider = await user("other@example.com", Role.STUDENT, created_by_id="another-admin")
    deleted = await user("deleted@example.com", Role.STUDENT, created_by_id=admin.id, is_deleted=True)
    await session(a)
    await session(a, state=SessionState.CONNECTION_FAILED)
    await session(a, state=SessionState.CONNECTED)
    await session(a, ended_at=None)
    await session(a, is_deleted=True)
    await session(a, start=NOW + timedelta(hours=1))
    await session(a, start=NOW - timedelta(minutes=5))  # not ended as of now
    await session(a, minutes=-3)
    await session(outsider)
    await session(deleted)
    await recording(a, is_deleted=True)
    await recording(a, at=NOW + timedelta(seconds=1))
    await recording(outsider)
    body = client.get("/api/v1/analytics?days=7", headers=headers(admin)).json()
    assert body["completed_sessions"] == 1 and body["submissions"] == 0 and body["roster_students"] == 1


async def test_student_scope_and_role_guards(client):
    admin = await user()
    a = await user("a@example.com", Role.STUDENT, created_by_id=admin.id)
    b = await user("b@example.com", Role.STUDENT, created_by_id=admin.id)
    root = await user("root@example.com", Role.ROOT)
    await session(a)
    await session(b)
    response = client.get(f"/api/v1/me/analytics?days=7&student_id={b.id}", headers=headers(a))
    assert response.status_code == 200
    assert response.json()["completed_sessions"] == 1
    assert client.get("/api/v1/analytics", headers=headers(a)).status_code == 403
    assert client.get("/api/v1/me/analytics", headers=headers(admin)).status_code == 403
    assert client.get("/api/v1/analytics", headers=headers(root)).status_code == 403
    assert client.get("/api/v1/analytics").status_code in (401, 403)


@pytest.mark.parametrize("days", ["0", "8", "31", "-7", "abc"])
async def test_invalid_ranges_are_rejected(client, days):
    admin = await user()
    assert client.get(f"/api/v1/analytics?days={days}", headers=headers(admin)).status_code == 422


async def test_review_counts_refresh_from_persisted_state(client):
    admin = await user()
    a = await user("a@example.com", Role.STUDENT, created_by_id=admin.id)
    item = await session(a, report=report())
    upload = await recording(a)
    before = client.get("/api/v1/analytics?days=7", headers=headers(admin)).json()
    assert before["reports_awaiting_review"] == before["submissions_awaiting_review"] == 1
    item.report.reviewed = True
    await item.save()
    upload.reviewed = True
    await upload.save()
    after = client.get("/api/v1/analytics?days=7", headers=headers(admin)).json()
    assert after["reports_awaiting_review"] == after["submissions_awaiting_review"] == 0
