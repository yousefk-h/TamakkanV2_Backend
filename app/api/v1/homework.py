from fastapi import APIRouter, Depends

from app.core.security import require_role, require_teacher_or_administrator
from app.models.user import Role, User
from app.schema.homework import HomeworkCreate, HomeworkPublic
from app.services.homework_service import HomeworkService, get_homework_service

router = APIRouter()


async def _public(items, service):
    titles = await service.topic_titles(items)
    return [HomeworkPublic.from_homework(item, titles.get(item.topic_id or "")) for item in items]


@router.post("/homework", response_model=HomeworkPublic, status_code=201)
async def create_homework(body: HomeworkCreate, staff: User = Depends(require_teacher_or_administrator),
                          service: HomeworkService = Depends(get_homework_service)) -> HomeworkPublic:
    item = await service.create(staff, body)
    return (await _public([item], service))[0]


@router.get("/homework", response_model=list[HomeworkPublic])
async def list_homework(staff: User = Depends(require_teacher_or_administrator),
                        service: HomeworkService = Depends(get_homework_service)) -> list[HomeworkPublic]:
    return await _public(await service.list_for_staff(staff), service)


@router.get("/homework/me", response_model=list[HomeworkPublic])
async def my_homework(student: User = Depends(require_role(Role.STUDENT)),
                      service: HomeworkService = Depends(get_homework_service)) -> list[HomeworkPublic]:
    return await _public(await service.list_for_student(student), service)


@router.delete("/homework/{homework_id}", status_code=204)
async def cancel_homework(homework_id: str, staff: User = Depends(require_teacher_or_administrator),
                          service: HomeworkService = Depends(get_homework_service)) -> None:
    await service.cancel(staff, homework_id)
