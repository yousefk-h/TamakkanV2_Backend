"""Generic CRUD every concrete repository builds on.

`save()` is Beanie's own upsert (`Document.save()` inserts if the document
isn't in the collection yet, replaces otherwise) - callers never need to know
which, and a repository never needs a separate `create`/`update` pair just to
wrap that.

Soft delete: `get_by_id()` excludes `is_deleted` documents (a "deleted"
account/record should behave as gone to every normal lookup), and
`soft_delete()` is the only way this base exposes to remove one - nothing
here ever issues a physical delete.
"""

from typing import Generic, Optional, TypeVar

from app.models.base import BaseDocument

ModelT = TypeVar("ModelT", bound=BaseDocument)


class BaseRepository(Generic[ModelT]):
    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    async def get_by_id(self, id: str) -> Optional[ModelT]:
        document = await self.model.get(id)
        if document is None or document.is_deleted:
            return None
        return document

    async def save(self, document: ModelT) -> ModelT:
        await document.save()
        return document

    async def soft_delete(self, document: ModelT) -> ModelT:
        document.is_deleted = True
        return await self.save(document)
