from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.message import AnnouncementScope, MessageKind
from app.models.user import Role
from app.schema.base import BaseSchema
from app.schema.limits import MAX_PROMPT_LENGTH


class NewConversationRequest(BaseModel):
    student_id: str
    body: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)


class RecipientScopeRequest(BaseModel):
    scope: AnnouncementScope
    scope_id: Optional[str] = None


class RecipientPreview(BaseModel):
    recipient_count: int


class SendAnnouncementRequest(BaseModel):
    scope: AnnouncementScope
    scope_id: Optional[str] = None
    body: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    # Client-generated (the browser mints one per compose action, e.g. a
    # random UUID) - identical on a retried/double-clicked submit of the
    # exact same compose action, so the server can refuse to deliver the
    # same announcement twice. See `Announcement.client_token`.
    client_token: str = Field(min_length=8, max_length=100)


class MessagePublic(BaseSchema):
    conversation_id: str
    sender_id: str
    sender_role: Role
    sender_name: Optional[str] = None
    kind: MessageKind = MessageKind.TEXT
    body: str
    # IMAGE/AUDIO only - a freshly signed delivery URL (see
    # `MessagingService.media_url_for`), never a stored/raw public_id.
    media_url: Optional[str] = None
    # AUDIO only.
    media_duration_seconds: Optional[float] = None
    read_at: Optional[datetime] = None
    announcement_id: Optional[str] = None
    is_mine: bool = False


class ConversationSummary(BaseSchema):
    other_participant_id: str
    other_participant_name: Optional[str] = None
    other_participant_role: Optional[Role] = None
    last_message_at: datetime
    last_message_preview: str
    unread_count: int = 0


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    total: int
    page: int
    limit: int


class MessagePage(BaseModel):
    items: list[MessagePublic]
    total: int
    page: int
    limit: int


class AnnouncementSummary(BaseSchema):
    sender_id: str
    sender_role: Role
    scope: AnnouncementScope
    scope_id: Optional[str] = None
    scope_name: Optional[str] = None
    body: str
    recipient_count: int


class AnnouncementPage(BaseModel):
    items: list[AnnouncementSummary]
    total: int
    page: int
    limit: int
