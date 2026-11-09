"""AI generation cost controls (Slice B): block genuinely concurrent
duplicate generation requests, enforce a per-topic daily generation budget,
and record usage/cost for every paid preparation-time AI call this module
guards.

Governing principle: generate once, persist, reuse - never spend provider
tokens twice for the same lesson content. In practice that means two
different things for two different kinds of waste, and this module handles
both without conflating them:

- A genuinely concurrent duplicate (a double-click, a retried request that
  actually reached the server twice, two browser tabs) must only ever
  result in one provider call - `claim_generation`'s short-lived mutex
  (`AIGenerationLock`) is what prevents this.
- A sequential, deliberate re-request (a teacher manually re-running
  content drafting because an earlier pass left a field empty, or asking
  for a fresh exercise draft after reviewing the first one) is a real,
  legitimate, already-documented product workflow, not waste - it is never
  blocked here, and the one real, content-hash-independent waste this
  module does eliminate directly is calling the provider at all when there
  is nothing left for it to fill (see `TranscriptPipelineService._run_draft`'s
  own "nothing to fill" short-circuit, upstream of this module).

`AIGenerationRecord` is the separate, permanent usage/cost ledger (never a
lock) - see `app.models.ai_generation` for why these are two collections,
not one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from beanie.odm.operators.update.general import Set
from loguru import logger
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.exceptions import ConflictException, UnprocessableException
from app.models.ai_generation import AIGenerationLock, AIGenerationRecord, AIGenerationStatus, AIGenerationType
from app.utils.time import utcnow


def compute_generation_key(generation_type: AIGenerationType, **components: Any) -> str:
    """A deterministic identity for "this exact generation request" - the
    same generation_type, source content, instructions/settings, and model
    always hash to the same key; anything genuinely different (edited
    source, a different kind/count/focus, a different model) hashes to a
    different one. Used only to detect a truly concurrent duplicate (see
    `claim_generation`) - never to decide whether to skip calling the
    provider for an otherwise-identical-but-later request.
    """
    payload = {"generation_type": generation_type.value, **components}
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass
class GenerationClaim:
    record: AIGenerationRecord
    generation_key: str


class GenerationBudgetExceeded(UnprocessableException):
    pass


async def enforce_topic_budget(topic_id: Optional[str]) -> None:
    """A topic accumulating an unreasonable number of generation attempts in
    a day is almost certainly a stuck UI or a runaway script - not a real
    authoring need. Checked before claiming, so a budget-exhausted request
    never even reaches the provider.
    """
    if topic_id is None:
        return
    limit = get_settings().MAX_AI_GENERATIONS_PER_TOPIC_PER_DAY
    since = utcnow() - timedelta(days=1)
    count = await AIGenerationRecord.find(
        AIGenerationRecord.topic_id == topic_id,
        AIGenerationRecord.started_at >= since,
    ).count()
    if count >= limit:
        raise GenerationBudgetExceeded(
            f"This lesson has reached its generation limit for today ({limit} AI requests in 24h). "
            "Try again later, or ask an administrator to raise the limit."
        )


async def claim_generation(
    *,
    generation_type: AIGenerationType,
    generation_key: str,
    model: str,
    topic_id: Optional[str] = None,
    session_id: Optional[str] = None,
    requested_by_id: Optional[str] = None,
) -> GenerationClaim:
    """Enforces the topic budget, then claims a short-lived mutex on
    `generation_key` (see `AIGenerationLock`'s own docstring): a second,
    genuinely concurrent request for the identical generation raises
    `ConflictException` (409); a crashed/abandoned claim older than
    `AI_GENERATION_STALE_CLAIM_MINUTES` is reclaimed instead of blocking
    forever. Always creates a fresh `AIGenerationRecord` - the permanent
    usage-log entry for this one attempt - and returns both, so the caller
    can `complete_generation`/`fail_generation` it afterward.
    """
    await enforce_topic_budget(topic_id)

    now = utcnow()
    lock = AIGenerationLock(generation_key=generation_key, claimed_at=now)
    try:
        await lock.insert()
    except DuplicateKeyError:
        existing = await AIGenerationLock.find_one(AIGenerationLock.generation_key == generation_key)
        if existing is None:
            # Lost the row to a release between the failed insert and this
            # read - vanishingly rare; the caller's own retry claims cleanly.
            raise ConflictException("This generation is already in progress - try again in a moment.")
        stale_after = timedelta(minutes=get_settings().AI_GENERATION_STALE_CLAIM_MINUTES)
        if (now - existing.claimed_at) <= stale_after:
            raise ConflictException(
                "This exact content is already being generated - wait for it to finish before trying again."
            )
        reclaimed = await AIGenerationLock.find_one(
            AIGenerationLock.id == existing.id,
            AIGenerationLock.claimed_at == existing.claimed_at,
        ).update(Set({"claimed_at": now}))
        if reclaimed.matched_count == 0:
            # Another caller reclaimed it first - theirs proceeds, this one waits.
            raise ConflictException(
                "This exact content is already being generated - wait for it to finish before trying again."
            )
        logger.info("AI generation lock reclaimed (abandoned): type={} key={} topic={}",
                    generation_type.value, generation_key[:12], topic_id)

    record = AIGenerationRecord(
        generation_type=generation_type,
        generation_key=generation_key,
        topic_id=topic_id,
        session_id=session_id,
        requested_by_id=requested_by_id,
        model=model,
        status=AIGenerationStatus.IN_PROGRESS,
        started_at=now,
    )
    await record.insert()
    return GenerationClaim(record=record, generation_key=generation_key)


def estimate_cost_usd(model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    """Returns `None` (never a guessed figure) unless this project's own
    verified $/1K-token pricing is configured for the model actually used -
    see `Settings.GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD`/
    `_OUTPUT_PRICE_PER_1K_TOKENS_USD`. Only exact-matches "gemini-3.5-flash"
    today, the one model every drafting/review call site in this codebase
    uses - a different model name simply yields `None` rather than silently
    applying the wrong per-token rate.
    """
    if input_tokens is None or output_tokens is None:
        return None
    settings = get_settings()
    if model != "gemini-3.5-flash":
        return None
    input_rate = settings.GEMINI_FLASH_INPUT_PRICE_PER_1K_TOKENS_USD
    output_rate = settings.GEMINI_FLASH_OUTPUT_PRICE_PER_1K_TOKENS_USD
    if input_rate is None or output_rate is None:
        return None
    return round((input_tokens / 1000) * input_rate + (output_tokens / 1000) * output_rate, 6)


async def _release_lock(generation_key: str) -> None:
    await AIGenerationLock.find_one(AIGenerationLock.generation_key == generation_key).delete()


async def complete_generation(
    claim: GenerationClaim,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    record = claim.record
    cost = estimate_cost_usd(record.model, input_tokens, output_tokens)
    record.status = AIGenerationStatus.COMPLETED
    record.input_tokens = input_tokens
    record.output_tokens = output_tokens
    record.estimated_cost_usd = cost
    record.cost_is_estimate = True
    record.completed_at = utcnow()
    await record.save()
    await _release_lock(claim.generation_key)


async def fail_generation(claim: GenerationClaim, exc: Exception) -> None:
    record = claim.record
    record.status = AIGenerationStatus.FAILED
    record.error_type = type(exc).__name__  # never the message - it can echo lesson/source content
    record.completed_at = utcnow()
    await record.save()
    await _release_lock(claim.generation_key)


async def record_completed_without_tokens(
    *,
    generation_type: AIGenerationType,
    model: str,
    topic_id: Optional[str] = None,
    session_id: Optional[str] = None,
    requested_by_id: Optional[str] = None,
) -> None:
    """For the two call sites not yet wired for per-call token capture
    (transcript extraction, session scoring) - still logs that a paid
    generation happened, for the same per-topic analytics/budget
    visibility, with tokens/cost honestly `None` rather than invented.
    These two already have their own duplicate-call protection (the
    transcript state machine's CAS, and the session-scoring service's own
    completed-session guard), so this only ever logs - it never claims a
    lock or blocks anything.
    """
    now = utcnow()
    await AIGenerationRecord(
        generation_type=generation_type,
        generation_key=compute_generation_key(generation_type, topic_id=topic_id, session_id=session_id, logged_at=now),
        topic_id=topic_id,
        session_id=session_id,
        requested_by_id=requested_by_id,
        model=model,
        status=AIGenerationStatus.COMPLETED,
        started_at=now,
        completed_at=now,
    ).insert()
