"""Server-side search, filters, pagination and total count for the admin's AI Reports list (`GET /sessions`).

Everything is filtered in the database, scoped to the calling admin's own roster. Without `page`/`limit` the endpoint still
returns the plain array other pages rely on; with them it returns `{items, total, page, limit}`.
"""
from datetime import datetime, timedelta

import pytest

from app.models.live_session import LiveSession, PracticeMode, SessionState, SpeakingReport
from app.models.topic_content import Level, TopicContent
from app.models.user import Role, User
from app.core.security import hash_password
from app.utils.time import utcnow
from tests.api.test_sessions import _login_with_user


def _report(score: int | None, reviewed: bool = False) -> SpeakingReport:
    return SpeakingReport(
        fluency_score=score, vocabulary_score=score, grammar_score=score, complexity_score=score, overall_score=score,
        summary="s", generated_at=utcnow(), reviewed=reviewed,
    )


async def _student(name: str, email: str, admin_id: str) -> User:
    user = User(email=email, password_hash=hash_password("x-correct-password"), full_name=name, role=Role.STUDENT, created_by_id=admin_id)
    await user.insert()
    return user


async def _session(student: User, topic: TopicContent, *, mode=PracticeMode.TOPIC_DISCUSSION, report=None, days_ago=0) -> LiveSession:
    session = LiveSession(
        student=student, student_id=student.id, topic=topic, topic_id=topic.id, mode=mode,
        assessment_level=topic.level.value, state=SessionState.ENDED, report=report, started_at=utcnow() - timedelta(days=days_ago),
    )
    await session.insert()
    return session


@pytest.fixture
async def world(client):
    """Admin A (the caller) owns Alice and Bob; admin B owns Carol - and Carol's session must never be visible to A."""
    token, admin = await _login_with_user(client, Role.TEACHER, email="admin-a@example.com")
    other_token, other = await _login_with_user(client, Role.TEACHER, email="admin-b@example.com")
    alice = await _student("Alice Anderson", "alice@school.example", admin.id)
    bob = await _student("Bob Brown", "bob@college.example", admin.id)
    carol = await _student("Carol Clark", "carol@other.example", other.id)
    music = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    routine = TopicContent(title="Daily Routine", level=Level.BEGINNER)
    await music.insert(); await routine.insert()
    s = {
        "alice_music_scored_unreviewed": await _session(alice, music, report=_report(90), days_ago=0),
        "alice_routine_notscored": await _session(alice, routine, report=_report(None), days_ago=3),
        "bob_music_voice_scored_reviewed": await _session(bob, music, mode=PracticeMode.VOICE_PRACTICE, report=_report(70, reviewed=True), days_ago=10),
        "bob_routine_noreport": await _session(bob, routine, report=None, days_ago=40),
        "carol_music_scored": await _session(carol, music, report=_report(80), days_ago=1),
    }
    return {"token": token, "other_token": other_token, "alice": alice, "bob": bob, "carol": carol, "music": music, "routine": routine, "s": s, "client": client}


def _get(world, token_key="token", **params):
    return world["client"].get("/api/v1/sessions", params=params, headers={"Authorization": f"Bearer {world[token_key]}"})


def _ids(response):
    body = response.json()
    return {item["id"] for item in (body["items"] if isinstance(body, dict) else body)}


def _expect(world, *names):
    return {world["s"][n].id for n in names}


# --- compatibility -----------------------------------------------------------------------------------------------


async def test_without_paging_params_the_response_is_still_the_plain_array_of_every_own_session(world):
    resp = _get(world)
    assert resp.status_code == 200 and isinstance(resp.json(), list)
    assert _ids(resp) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored", "bob_music_voice_scored_reviewed", "bob_routine_noreport")


async def test_the_paged_response_carries_items_total_page_and_limit(world):
    body = _get(world, page=1, limit=2).json()
    assert set(body) == {"items", "total", "page", "limit"} and body["total"] == 4 and body["page"] == 1 and body["limit"] == 2
    assert len(body["items"]) == 2


# --- search ------------------------------------------------------------------------------------------------------


async def test_search_matches_the_student_name(world):
    assert _ids(_get(world, search="alice anderson", page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored")
    assert _ids(_get(world, search="BROWN", page=1)) == _expect(world, "bob_music_voice_scored_reviewed", "bob_routine_noreport")


async def test_search_matches_the_student_email(world):
    assert _ids(_get(world, search="bob@college", page=1)) == _expect(world, "bob_music_voice_scored_reviewed", "bob_routine_noreport")
    assert _ids(_get(world, search="school.example", page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored")


async def test_search_matches_the_topic_title(world):
    assert _ids(_get(world, search="routine", page=1)) == _expect(world, "alice_routine_notscored", "bob_routine_noreport")
    assert _ids(_get(world, search="Music", page=1)) == _expect(world, "alice_music_scored_unreviewed", "bob_music_voice_scored_reviewed")


async def test_search_is_a_literal_substring_never_a_pattern_and_blank_search_means_no_search(world):
    assert _ids(_get(world, search=".*", page=1)) == set()
    assert _ids(_get(world, search="   ", page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored", "bob_music_voice_scored_reviewed", "bob_routine_noreport")


# --- id filters --------------------------------------------------------------------------------------------------


async def test_filter_by_student_id(world):
    assert _ids(_get(world, student_id=world["alice"].id, page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored")


async def test_filter_by_topic_id(world):
    assert _ids(_get(world, topic_id=world["routine"].id, page=1)) == _expect(world, "alice_routine_notscored", "bob_routine_noreport")


# --- report status -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("any", ("alice_music_scored_unreviewed", "alice_routine_notscored", "bob_music_voice_scored_reviewed")),
        ("scored", ("alice_music_scored_unreviewed", "bob_music_voice_scored_reviewed")),
        ("not_scored", ("alice_routine_notscored",)),
        ("needs_review", ("alice_music_scored_unreviewed", "alice_routine_notscored")),
        ("reviewed", ("bob_music_voice_scored_reviewed",)),
    ],
)
async def test_report_status_filters_map_to_the_stored_report_fields(world, status, expected):
    assert _ids(_get(world, report_status=status, page=1)) == _expect(world, *expected)


# --- mode, dates -------------------------------------------------------------------------------------------------


async def test_filter_by_mode_uses_the_existing_enum_values(world):
    assert _ids(_get(world, mode="voice_practice", page=1)) == _expect(world, "bob_music_voice_scored_reviewed")
    assert _ids(_get(world, mode="topic_discussion", page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored", "bob_routine_noreport")
    assert _get(world, mode="avatar").status_code == 422


async def test_time_window_is_half_open_on_explicit_instants(world):
    """`started_from` is inclusive, `started_before` is exclusive, and both are instants - not calendar days."""
    now = utcnow()
    assert _ids(_get(world, started_from=(now - timedelta(hours=1)).isoformat() + "Z", page=1)) == _expect(world, "alice_music_scored_unreviewed")
    assert _ids(_get(world, started_from=(now - timedelta(days=7)).isoformat() + "Z", page=1)) == _expect(world, "alice_music_scored_unreviewed", "alice_routine_notscored")
    window = _get(world, started_from=(now - timedelta(days=12)).isoformat() + "Z", started_before=(now - timedelta(days=8)).isoformat() + "Z", page=1)
    assert _ids(window) == _expect(world, "bob_music_voice_scored_reviewed")
    assert _ids(_get(world, started_before=(now - timedelta(days=30)).isoformat() + "Z", page=1)) == _expect(world, "bob_routine_noreport")


async def _at(world, name: str, started_at: datetime) -> LiveSession:
    session = await _session(world["alice"], world["music"], report=_report(60))
    session.started_at = started_at
    await session.save()
    return session


async def test_a_session_around_local_midnight_belongs_to_the_expected_local_day(world):
    """Stored timestamps stay UTC. A browser in Amman (UTC+3) asks for 6 Oct as [5 Oct 21:00Z, 6 Oct 21:00Z)."""
    before = await _at(world, "before", datetime(2020, 10, 5, 20, 59, 59))    # 23:59:59 on the 5th, Amman
    first = await _at(world, "first", datetime(2020, 10, 5, 21, 0, 0))        # 00:00:00 on the 6th, Amman
    last = await _at(world, "last", datetime(2020, 10, 6, 20, 59, 59, 999000))  # 23:59:59.999 on the 6th, Amman
    after = await _at(world, "after", datetime(2020, 10, 6, 21, 0, 0))        # 00:00:00 on the 7th, Amman

    sixth = _get(world, started_from="2020-10-05T21:00:00Z", started_before="2020-10-06T21:00:00Z", page=1)
    assert _ids(sixth) == {first.id, last.id}, "start inclusive, end exclusive"
    fifth = _get(world, started_from="2020-10-04T21:00:00Z", started_before="2020-10-05T21:00:00Z", page=1)
    assert _ids(fifth) == {before.id}, "the instant the 6th begins is NOT part of the 5th"
    seventh = _get(world, started_from="2020-10-06T21:00:00Z", started_before="2020-10-07T21:00:00Z", page=1)
    assert _ids(seventh) == {after.id}
    # From a UTC browser the same sessions fall on different days: that is the point of sending instants.
    utc_sixth = _get(world, started_from="2020-10-06T00:00:00Z", started_before="2020-10-07T00:00:00Z", page=1)
    assert _ids(utc_sixth) == {last.id, after.id}


async def test_offset_aware_instants_are_converted_to_utc(world):
    """`+03:00` and `Z` spellings of the same instant select the same sessions; a missing offset is read as UTC."""
    inside = await _at(world, "inside", datetime(2020, 10, 5, 21, 0, 0))
    outside = await _at(world, "outside", datetime(2020, 10, 5, 20, 59, 59))
    zulu = _get(world, started_from="2020-10-05T21:00:00Z", started_before="2020-10-06T21:00:00Z", page=1)
    offset = _get(world, started_from="2020-10-06T00:00:00+03:00", started_before="2020-10-07T00:00:00+03:00", page=1)
    naive = _get(world, started_from="2020-10-05T21:00:00", started_before="2020-10-06T21:00:00", page=1)
    assert _ids(zulu) == _ids(offset) == _ids(naive) == {inside.id}
    assert outside.id not in _ids(offset)
    west = _get(world, started_from="2020-10-05T14:00:00-07:00", started_before="2020-10-06T14:00:00-07:00", page=1)
    assert _ids(west) == {inside.id}, "21:00Z is 14:00 in Los Angeles"


async def test_an_empty_or_inverted_time_window_is_rejected(world):
    assert _get(world, started_from="2026-02-02T00:00:00Z", started_before="2026-01-01T00:00:00Z", page=1).status_code == 422
    assert _get(world, started_from="2026-02-02T00:00:00Z", started_before="2026-02-02T00:00:00Z", page=1).status_code == 422
    assert _get(world, started_from="2026-02-02T00:00:00+03:00", started_before="2026-02-01T21:00:00Z", page=1).status_code == 422, "equal instants in different spellings"
    assert _get(world, started_from="not-a-date", page=1).status_code == 422


# --- combinations, pagination ------------------------------------------------------------------------------------


async def test_filters_combine_with_and(world):
    resp = _get(world, student_id=world["bob"].id, topic_id=world["music"].id, report_status="scored", mode="voice_practice", page=1)
    assert _ids(resp) == _expect(world, "bob_music_voice_scored_reviewed")
    assert _ids(_get(world, student_id=world["bob"].id, report_status="needs_review", page=1)) == set()
    assert _ids(_get(world, search="music", report_status="scored", started_from=(utcnow() - timedelta(days=1, hours=1)).isoformat() + "Z", page=1)) == _expect(world, "alice_music_scored_unreviewed")


async def test_pagination_keeps_the_filters_and_the_total(world):
    first = _get(world, report_status="any", page=1, limit=2).json()
    second = _get(world, report_status="any", page=2, limit=2).json()
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})
    # newest first across pages
    started = [i["started_at"] for i in first["items"] + second["items"]]
    assert started == sorted(started, reverse=True)
    # a filter applied on page 2 still counts only what matched
    scored = _get(world, report_status="scored", page=2, limit=1).json()
    assert scored["total"] == 2 and len(scored["items"]) == 1 and scored["items"][0]["report"]["overall_score"] is not None


async def test_a_page_past_the_end_is_empty_but_still_reports_the_total(world):
    body = _get(world, report_status="scored", page=5, limit=2).json()
    assert body["items"] == [] and body["total"] == 2


async def test_dropping_every_filter_restores_the_full_list(world):
    filtered = _get(world, report_status="reviewed", search="bob", page=1, limit=10).json()
    assert filtered["total"] == 1
    cleared = _get(world, page=1, limit=10).json()
    assert cleared["total"] == 4


async def test_limit_is_bounded(world):
    assert _get(world, page=1, limit=101).status_code == 422
    assert _get(world, page=0, limit=10).status_code == 422


# --- access rules ------------------------------------------------------------------------------------------------


async def test_another_admins_students_never_appear_whatever_the_filters(world):
    for params in ({}, {"search": "carol"}, {"student_id": world["carol"].id}, {"report_status": "scored"}, {"topic_id": world["music"].id}):
        assert world["s"]["carol_music_scored"].id not in _ids(_get(world, page=1, **params))
    assert _get(world, student_id=world["carol"].id, page=1).json()["total"] == 0
    assert _ids(_get(world, "other_token", page=1)) == _expect(world, "carol_music_scored")


async def test_students_and_anonymous_callers_cannot_use_the_admin_list(world):
    student_token, _ = await _login_with_user(world["client"], Role.STUDENT, email="s@example.com")
    assert world["client"].get("/api/v1/sessions", params={"search": "alice"}, headers={"Authorization": f"Bearer {student_token}"}).status_code == 403
    assert world["client"].get("/api/v1/sessions", params={"search": "alice"}).status_code in (401, 403)
