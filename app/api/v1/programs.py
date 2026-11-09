from fastapi import APIRouter, Depends

from app.core.security import get_current_user, require_administrator, require_teacher_or_administrator
from app.models.user import User
from app.schema.program import ProgramPublic, ProgramWrite
from app.schema.program_topic import ProgramTopicPublic, ProgramTopicUpdate, ProgramTopicWrite
from app.services.program_service import ProgramService, get_program_service

router = APIRouter()


@router.get("/programs", response_model=list[ProgramPublic])
async def list_programs(_caller: User = Depends(get_current_user),
                        service: ProgramService = Depends(get_program_service)) -> list[ProgramPublic]:
    return [ProgramPublic.from_program(item) for item in await service.list_active()]


@router.post("/programs", response_model=ProgramPublic, status_code=201)
async def create_program(body: ProgramWrite, admin: User = Depends(require_administrator),
                         service: ProgramService = Depends(get_program_service)) -> ProgramPublic:
    return ProgramPublic.from_program(await service.create(body, admin))


@router.put("/programs/{program_id}", response_model=ProgramPublic)
async def update_program(program_id: str, body: ProgramWrite, _admin: User = Depends(require_administrator),
                         service: ProgramService = Depends(get_program_service)) -> ProgramPublic:
    return ProgramPublic.from_program(await service.update(program_id, body))


@router.delete("/programs/{program_id}", status_code=204)
async def archive_program(program_id: str, _admin: User = Depends(require_administrator),
                          service: ProgramService = Depends(get_program_service)) -> None:
    await service.archive(program_id)


@router.get("/programs/{program_id}/topics", response_model=list[ProgramTopicPublic])
async def list_program_topics(program_id: str, _caller: User = Depends(require_teacher_or_administrator),
                              service: ProgramService = Depends(get_program_service)) -> list[ProgramTopicPublic]:
    return [ProgramTopicPublic.from_assignment(item, topic.title) for item, topic in await service.list_topics(program_id)]


@router.post("/programs/{program_id}/topics", response_model=ProgramTopicPublic, status_code=201)
async def assign_program_topic(program_id: str, body: ProgramTopicWrite,
                               caller: User = Depends(require_teacher_or_administrator),
                               service: ProgramService = Depends(get_program_service)) -> ProgramTopicPublic:
    item = await service.assign_topic(program_id, body, caller)
    topic = await service._topic_for_write(item.topic_id, caller)
    return ProgramTopicPublic.from_assignment(item, topic.title)


@router.patch("/programs/{program_id}/topics/{topic_id}", response_model=ProgramTopicPublic)
async def update_program_topic(program_id: str, topic_id: str, body: ProgramTopicUpdate,
                               caller: User = Depends(require_teacher_or_administrator),
                               service: ProgramService = Depends(get_program_service)) -> ProgramTopicPublic:
    item = await service.update_topic(program_id, topic_id, body, caller)
    topic = await service._topic_for_write(topic_id, caller)
    return ProgramTopicPublic.from_assignment(item, topic.title)


@router.delete("/programs/{program_id}/topics/{topic_id}", status_code=204)
async def remove_program_topic(program_id: str, topic_id: str,
                               caller: User = Depends(require_teacher_or_administrator),
                               service: ProgramService = Depends(get_program_service)) -> None:
    await service.remove_topic(program_id, topic_id, caller)


@router.get("/topics/{topic_id}/program-assignments", response_model=list[ProgramTopicPublic])
async def list_topic_program_assignments(topic_id: str, caller: User = Depends(require_teacher_or_administrator),
                                         service: ProgramService = Depends(get_program_service)) -> list[ProgramTopicPublic]:
    assignments = await service.list_assignments_for_topic(topic_id, caller)
    topic = await service._topic_for_write(topic_id, caller)
    return [ProgramTopicPublic.from_assignment(item, topic.title) for item in assignments]
