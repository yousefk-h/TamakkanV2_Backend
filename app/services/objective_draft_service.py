"""Teacher-reviewed learning-objective suggestions.

This service intentionally persists nothing from the provider.  It reuses
the existing generation ledger for cost, quota and duplicate-click guards;
only the teacher's later PUT writes objectives to the lesson.
"""

from app.core.ai_cost_control import claim_generation, complete_generation, compute_generation_key, fail_generation
from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableException, UnprocessableException, UpstreamServiceException
from app.core.objectives_drafting import (
    MODEL,
    ObjectiveSuggestion,
    ObjectivesDrafter,
    ObjectivesDraftingError,
    ObjectivesDraftingNotConfigured,
    get_objectives_drafter,
)
from app.models.ai_generation import AIGenerationType
from app.models.user import User
from app.services.topic_content_service import TopicContentService, get_topic_content_service


class ObjectiveDraftService:
    def __init__(self, topics: TopicContentService, drafter: ObjectivesDrafter) -> None:
        self.topics = topics
        self.drafter = drafter

    async def draft(self, topic_id: str, teacher: User, refresh: bool = False) -> list[ObjectiveSuggestion]:
        topic = await self.topics.get_own_topic(topic_id, teacher)
        source = (topic.approved_transcript or "").strip()
        if len(source) < 100:
            raise UnprocessableException("Approve at least 100 characters of lesson source text before suggesting objectives")
        bounded_source = source[: get_settings().MAX_DRAFTING_SOURCE_CHARS]
        generation_key = compute_generation_key(
            AIGenerationType.OBJECTIVES_DRAFT,
            topic_id=topic_id,
            title=topic.title,
            level=topic.level.value,
            source=bounded_source,
            model=MODEL,
        )
        # Browser refresh/retry returns the same pending teacher drafts. A
        # caller must explicitly request `refresh` to incur another provider
        # call, and a changed title/source/level naturally produces a new key.
        if not refresh and topic.objective_draft_generation_key == generation_key and topic.objective_drafts:
            return [ObjectiveSuggestion(text=item.text) for item in topic.objective_drafts]
        claim = await claim_generation(
            generation_type=AIGenerationType.OBJECTIVES_DRAFT,
            generation_key=generation_key,
            model=MODEL,
            topic_id=topic_id,
            requested_by_id=teacher.id,
        )
        try:
            texts = await self.drafter.draft(title=topic.title, level=topic.level, source=bounded_source)
            unique = list(dict.fromkeys(text.strip() for text in texts if text.strip()))[:8]
            if not unique:
                raise ObjectivesDraftingError("Provider returned no usable objective suggestions")
            suggestions = [ObjectiveSuggestion(text=text) for text in unique]
        except ObjectivesDraftingNotConfigured as exc:
            await fail_generation(claim, exc)
            raise ServiceUnavailableException("AI objective suggestions are not configured in this development environment") from exc
        except ObjectivesDraftingError as exc:
            await fail_generation(claim, exc)
            raise UpstreamServiceException("AI objective suggestions could not be generated. Try again or write them manually.") from exc
        usage = getattr(self.drafter, "last_usage", None)
        await complete_generation(
            claim, input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None)
        )
        await self.topics.topic_repository.update_fields(
            topic_id,
            {
                "objective_drafts": [
                    {"text": item.text, "source": "ai"} for item in suggestions
                ],
                "objective_draft_generation_key": generation_key,
            },
        )
        return suggestions


def get_objective_draft_service() -> ObjectiveDraftService:
    return ObjectiveDraftService(get_topic_content_service(), get_objectives_drafter())
