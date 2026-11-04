from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.topic_content import Level
from app.models.unit import Unit
from app.models.user import Role, User
from app.repositories.unit_repository import UnitRepository
from app.schema.unit import UnitCreate, UnitUpdate


class UnitService:
    def __init__(self, unit_repository: UnitRepository) -> None:
        self.unit_repository = unit_repository

    async def create(self, body: UnitCreate, created_by: User) -> Unit:
        unit = Unit(level=body.level, name=body.name, order_index=body.order_index, created_by_id=created_by.id)
        return await self.unit_repository.save(unit)

    async def list_for_requester(self, requester: User, level: Optional[Level] = None) -> list[Unit]:
        """Root and Administrator (Center Admin) see every unit; a Teacher
        sees only their own plus legacy/shared ones - same visibility rule as
        `TopicContentService.list_for_requester`.
        """
        owner_id = None if requester.role in (Role.ROOT, Role.ADMINISTRATOR) else requester.id
        return await self.unit_repository.list_all(level=level, owner_id=owner_id)

    async def get_one(self, unit_id: str) -> Unit:
        unit = await self.unit_repository.get_by_id(unit_id)
        if unit is None:
            raise NotFoundException("Unit not found")
        return unit

    async def get_own_unit(self, unit_id: str, admin: User) -> Unit:
        """The Teacher write-path ownership gate - a legacy unit
        (`created_by_id is None`) is visible to every Teacher but writable by
        none until Root explicitly re-owns it. Root and Administrator (Center
        Admin) are unrestricted, same as `TopicContentService.get_own_topic`.
        """
        unit = await self.get_one(unit_id)
        if admin.role in (Role.ROOT, Role.ADMINISTRATOR):
            return unit
        if unit.created_by_id != admin.id:
            raise ForbiddenException("This unit belongs to a different teacher")
        return unit

    async def update(self, unit_id: str, body: UnitUpdate, admin: User) -> Unit:
        unit = await self.get_own_unit(unit_id, admin)
        changes = body.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(unit, key, value)
        return await self.unit_repository.save(unit)

    async def delete(self, unit_id: str, admin: User) -> None:
        unit = await self.get_own_unit(unit_id, admin)
        await self.unit_repository.soft_delete(unit)


def get_unit_service() -> UnitService:
    return UnitService(UnitRepository())
