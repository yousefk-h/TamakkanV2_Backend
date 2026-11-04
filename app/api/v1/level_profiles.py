from fastapi import APIRouter, Depends

from app.core.exceptions import NotFoundException
from app.core.security import get_current_user, require_administrator
from app.models.level_profile import LevelProfile
from app.models.user import User
from app.schema.level_profile import LevelProfileCreate, LevelProfilePublic

router = APIRouter()


@router.get("/level-profiles", response_model=list[LevelProfilePublic])
async def list_level_profiles(_caller: User = Depends(get_current_user)) -> list[LevelProfilePublic]:
    items = await LevelProfile.find({"is_deleted": False, "is_active": True}).sort("order_index").to_list()
    return [LevelProfilePublic.from_profile(item) for item in items]


@router.post("/level-profiles", response_model=LevelProfilePublic, status_code=201)
async def create_level_profile(body: LevelProfileCreate,
                               _admin: User = Depends(require_administrator)) -> LevelProfilePublic:
    item = LevelProfile(**body.model_dump())
    await item.insert()
    return LevelProfilePublic.from_profile(item)


@router.delete("/level-profiles/{profile_id}", status_code=204)
async def archive_level_profile(profile_id: str, _admin: User = Depends(require_administrator)) -> None:
    item = await LevelProfile.find_one({"_id": profile_id, "is_deleted": False})
    if item is None:
        raise NotFoundException("Level not found")
    item.is_active = False
    item.is_deleted = True
    await item.save()
