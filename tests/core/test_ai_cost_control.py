"""Unit tests for app.core.ai_cost_control (Slice B) - the shared
dedup/budget/cost-tracking layer wired into content drafting, exercise
drafting, Arabic vocabulary backfill, and transcript English review (see
tests/services/test_transcript_pipeline_service.py and
tests/api/test_topics.py for the end-to-end wiring through those call
sites). `mock_db` (tests/conftest.py, autouse) gives every test here a real
in-memory Mongo-backed Document layer.
"""

import pytest

from app.core.ai_cost_control import (
    GenerationBudgetExceeded,
    claim_generation,
    compute_generation_key,
    complete_generation,
    enforce_topic_budget,
    estimate_cost_usd,
    fail_generation,
)
from app.core.config import get_settings
from app.core.exceptions import ConflictException
from app.models.ai_generation import AIGenerationLock, AIGenerationRecord, AIGenerationStatus, AIGenerationType

MODEL = "gemini-3.5-flash"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # Several tests below monkeypatch env vars this module reads through
    # `get_settings()`, which is `lru_cache`d - clear before and after so
    # one test's override can never leak into another.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_second_concurrent_identical_request_is_rejected_while_first_is_in_progress():
    key = compute_generation_key(AIGenerationType.EXERCISE_DRAFT, topic_id="t1", kind="grammar")
    claim1 = await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t1")

    with pytest.raises(ConflictException):
        await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t1")

    await complete_generation(claim1, input_tokens=10, output_tokens=20)

    # The lock releases on completion - a later, sequential identical
    # request is never permanently blocked (see the module's own docstring
    # on why: several teacher workflows rely on being able to re-request
    # the same drafting operation).
    claim2 = await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t1")
    await complete_generation(claim2, input_tokens=5, output_tokens=5)

    records = await AIGenerationRecord.find(AIGenerationRecord.topic_id == "t1").to_list()
    assert len(records) == 2
    assert all(r.status == AIGenerationStatus.COMPLETED for r in records)


async def test_different_content_hashes_to_a_different_key_and_is_never_blocked():
    key_a = compute_generation_key(AIGenerationType.CONTENT_DRAFT, topic_id="t2", source="Hello world")
    key_b = compute_generation_key(AIGenerationType.CONTENT_DRAFT, topic_id="t2", source="Something else entirely")
    assert key_a != key_b

    claim_a = await claim_generation(generation_type=AIGenerationType.CONTENT_DRAFT, generation_key=key_a, model=MODEL, topic_id="t2")
    claim_b = await claim_generation(generation_type=AIGenerationType.CONTENT_DRAFT, generation_key=key_b, model=MODEL, topic_id="t2")
    # Both proceed - a different request is never treated as a duplicate.
    await complete_generation(claim_a, input_tokens=1, output_tokens=1)
    await complete_generation(claim_b, input_tokens=1, output_tokens=1)


async def test_a_failed_generation_releases_its_lock_for_an_immediate_retry():
    key = compute_generation_key(AIGenerationType.CONTENT_REGENERATE, topic_id="t3")
    claim1 = await claim_generation(generation_type=AIGenerationType.CONTENT_REGENERATE, generation_key=key, model=MODEL, topic_id="t3")
    await fail_generation(claim1, RuntimeError("boom"))

    # Immediately retriable - no stale-claim wait required for a FAILED attempt.
    claim2 = await claim_generation(generation_type=AIGenerationType.CONTENT_REGENERATE, generation_key=key, model=MODEL, topic_id="t3")
    await complete_generation(claim2, input_tokens=1, output_tokens=1)

    records = await AIGenerationRecord.find(AIGenerationRecord.topic_id == "t3").sort("+started_at").to_list()
    assert [r.status for r in records] == [AIGenerationStatus.FAILED, AIGenerationStatus.COMPLETED]
    assert await AIGenerationLock.find(AIGenerationLock.generation_key == key).count() == 0


async def test_an_abandoned_in_progress_claim_is_reclaimed_after_the_stale_window(monkeypatch):
    monkeypatch.setenv("AI_GENERATION_STALE_CLAIM_MINUTES", "0")
    get_settings.cache_clear()
    key = compute_generation_key(AIGenerationType.EXERCISE_DRAFT, topic_id="t4")
    await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t4")

    # Simulates a crashed worker: never completed or failed. With a 0-minute
    # stale window, this is immediately reclaimable rather than permanently
    # blocking the topic's drafting.
    claim2 = await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t4")
    await complete_generation(claim2, input_tokens=1, output_tokens=1)
    assert await AIGenerationLock.find(AIGenerationLock.generation_key == key).count() == 0


async def test_budget_exceeded_blocks_a_new_claim_before_any_provider_call(monkeypatch):
    monkeypatch.setenv("MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY", "2")
    get_settings.cache_clear()
    for i in range(2):
        key = compute_generation_key(AIGenerationType.EXERCISE_DRAFT, topic_id="t5", i=i)
        claim = await claim_generation(generation_type=AIGenerationType.EXERCISE_DRAFT, generation_key=key, model=MODEL, topic_id="t5")
        await complete_generation(claim, input_tokens=1, output_tokens=1)

    with pytest.raises(GenerationBudgetExceeded):
        await claim_generation(
            generation_type=AIGenerationType.EXERCISE_DRAFT,
            generation_key=compute_generation_key(AIGenerationType.EXERCISE_DRAFT, topic_id="t5", i=99),
            model=MODEL,
            topic_id="t5",
        )
    # A different topic is entirely unaffected by another topic's budget.
    await enforce_topic_budget("some-other-topic")


async def test_estimate_cost_usd_is_none_when_pricing_is_not_configured():
    assert estimate_cost_usd(MODEL, 1000, 1000) is None


async def test_estimate_cost_usd_is_none_when_token_counts_are_unavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD", "0.001")
    monkeypatch.setenv("GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD", "0.002")
    get_settings.cache_clear()
    assert estimate_cost_usd(MODEL, None, None) is None


async def test_estimate_cost_usd_applies_configured_per_model_pricing(monkeypatch):
    monkeypatch.setenv("GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD", "0.001")
    monkeypatch.setenv("GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD", "0.002")
    get_settings.cache_clear()
    cost = estimate_cost_usd(MODEL, 1000, 1000)
    assert cost == pytest.approx(0.001 + 0.002)


async def test_estimate_cost_usd_is_none_for_an_unrecognized_model(monkeypatch):
    monkeypatch.setenv("GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD", "0.001")
    monkeypatch.setenv("GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD", "0.002")
    get_settings.cache_clear()
    assert estimate_cost_usd("some-other-model", 1000, 1000) is None


async def test_complete_generation_persists_token_and_cost_fields(monkeypatch):
    monkeypatch.setenv("GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD", "0.001")
    monkeypatch.setenv("GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD", "0.002")
    get_settings.cache_clear()
    key = compute_generation_key(AIGenerationType.CONTENT_DRAFT, topic_id="t6")
    claim = await claim_generation(generation_type=AIGenerationType.CONTENT_DRAFT, generation_key=key, model=MODEL, topic_id="t6")
    await complete_generation(claim, input_tokens=2000, output_tokens=500)

    stored = await AIGenerationRecord.get(claim.record.id)
    assert stored.status == AIGenerationStatus.COMPLETED
    assert stored.input_tokens == 2000 and stored.output_tokens == 500
    assert stored.estimated_cost_usd == pytest.approx(0.002 + 0.001)
    assert stored.cost_is_estimate is True


async def test_fail_generation_never_stores_the_exception_message():
    class HostileLessonContentError(Exception):
        pass

    key = compute_generation_key(AIGenerationType.CONTENT_REGENERATE, topic_id="t7")
    claim = await claim_generation(generation_type=AIGenerationType.CONTENT_REGENERATE, generation_key=key, model=MODEL, topic_id="t7")
    await fail_generation(claim, HostileLessonContentError("contains real lesson transcript text that must never be logged"))

    stored = await AIGenerationRecord.get(claim.record.id)
    assert stored.status == AIGenerationStatus.FAILED
    assert stored.error_type == "HostileLessonContentError"
    assert "lesson transcript" not in (stored.error_type or "")
