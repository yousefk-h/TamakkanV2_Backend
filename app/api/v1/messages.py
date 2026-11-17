"""In-platform Teacher/Student/Admin messaging (Teacher Workspace Increment
3). See `app.services.messaging_service` for the authorization and
delivery rules - every recipient list is resolved server-side from the
caller's own existing reach, never from a client-supplied id list.
"""

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.core.security import get_current_user
from app.models.message import AnnouncementScope
from app.models.user import User
from app.schema.message import (
    AnnouncementPage,
    AnnouncementSummary,
    ConversationPage,
    MessagePage,
    MessagePublic,
    NewConversationRequest,
    RecipientPreview,
    ReplyRequest,
    SendAnnouncementRequest,
)
from app.services.messaging_service import MessagingService, get_messaging_service

router = APIRouter()


@router.get("/messages/conversations", response_model=ConversationPage)
async def list_conversations(
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> ConversationPage:
    """A caller's own inbox - every 1:1 conversation they're a participant
    in, newest activity first, with an unread count. Works for a Student,
    Teacher, or Admin alike (same model, same endpoint)."""
    items, total = await service.list_inbox(caller, page, limit)
    return ConversationPage(items=items, total=total, page=page, limit=limit)


@router.get("/messages/conversations/{conversation_id}", response_model=MessagePage)
async def list_conversation_messages(
    conversation_id: str, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> MessagePage:
    """Every message in one conversation, newest first. Marks the other
    participant's unread messages as read as a side effect of this read -
    403 if the caller isn't actually one of its two participants."""
    return await service.list_messages(caller, conversation_id, page, limit)


@router.post("/messages/conversations/{conversation_id}/reply", response_model=MessagePublic, status_code=201)
async def reply_to_conversation(
    conversation_id: str, body: ReplyRequest,
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> MessagePublic:
    """Either participant may reply to an existing conversation - this is
    how a Student responds to a Teacher/Admin; a Student can never use this
    to start a conversation with someone new (see `/messages/new`)."""
    return await service.reply(caller, conversation_id, body.body)


@router.post("/messages/conversations/{conversation_id}/image", response_model=MessagePublic, status_code=201)
async def send_image_message(
    conversation_id: str, file: UploadFile = File(...),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> MessagePublic:
    """Either participant may attach an image to an existing conversation -
    same authorization boundary as `reply`. The actual bytes are validated
    server-side (real magic-number sniff, then Cloudinary's own decode),
    never just the browser's claimed Content-Type."""
    data = await file.read()
    return await service.send_image_message(caller, conversation_id, data, file.content_type or "")


@router.post("/messages/conversations/{conversation_id}/audio", response_model=MessagePublic, status_code=201)
async def send_voice_message(
    conversation_id: str, file: UploadFile = File(...),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> MessagePublic:
    """Same authorization boundary as `reply`/the image attachment above.
    Duration is Cloudinary's own reported value, checked after upload - see
    `MessagingService.send_voice_message`'s own doc for why."""
    data = await file.read()
    return await service.send_voice_message(caller, conversation_id, data, file.content_type or "")


@router.post("/messages/new", response_model=MessagePublic, status_code=201)
async def start_conversation(
    body: NewConversationRequest,
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> MessagePublic:
    """Teacher/Admin only - starts (or reuses) a 1:1 conversation with one
    Student they are actually authorized to reach (their own Cohort roster,
    or a student they created)."""
    return await service.send_new_message(caller, body.student_id, body.body)


@router.get("/messages/announcements/recipient-count", response_model=RecipientPreview)
async def preview_announcement_recipients(
    scope: AnnouncementScope, scope_id: str | None = Query(None),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> RecipientPreview:
    """How many distinct students a bulk announcement with this scope would
    actually reach, resolved with the exact same authorization/dedup the
    real send uses - shown to the composer before they commit to sending."""
    count = await service.preview_recipients(caller, scope, scope_id)
    return RecipientPreview(recipient_count=count)


@router.post("/messages/announcements", response_model=AnnouncementSummary, status_code=201)
async def send_announcement(
    body: SendAnnouncementRequest,
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> AnnouncementSummary:
    """One message, delivered once into each resolved recipient's own
    private conversation with the sender - never a shared thread, never
    exposing the recipient list to the recipients themselves. Retried with
    the same `client_token` (e.g. a double-clicked submit) returns the
    original result instead of delivering a second time."""
    return await service.send_announcement(caller, body.scope, body.scope_id, body.body, body.client_token)


@router.get("/messages/announcements", response_model=AnnouncementPage)
async def list_my_announcements(
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
    caller: User = Depends(get_current_user), service: MessagingService = Depends(get_messaging_service),
) -> AnnouncementPage:
    """A caller's own sent-announcement history - never another sender's."""
    items, total = await service.list_announcements(caller, page, limit)
    return AnnouncementPage(items=items, total=total, page=page, limit=limit)
