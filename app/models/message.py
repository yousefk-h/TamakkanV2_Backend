"""In-platform Teacher/Student/Admin messaging (Teacher Workspace Increment
3). Never uses AI - a message body is exactly what its sender typed,
rendered as plain text (never raw HTML) by every reader.

Three documents, three different jobs:

- `Conversation` is always exactly one Student and one staff member (a
  Teacher or an Admin) - a private, 1:1 thread. A student never sees who
  else a teacher messages, and a bulk announcement never creates a shared
  thread multiple students can see into (see `Announcement` below).
- `Message` is one real message inside one Conversation.
- `Announcement` is the permanent record of one bulk send (a teacher's
  Cohort/Program/"my students" announcement, or an Admin broadcast) - it
  never holds the recipient list itself (that would let anyone who can read
  it enumerate every recipient); it only holds who sent it, the resolved
  scope, and how many students it went to. The actual delivery is: one
  ordinary `Message` per recipient, in that recipient's OWN private
  Conversation with the sender, tagged with this Announcement's id - so a
  reply to an announcement is, structurally, just a reply in that
  student's own 1:1 conversation, private to the teacher, never visible to
  any other recipient.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from beanie import Indexed
from pydantic import Field

from app.models.base import BaseDocument
from app.models.user import Role


class AnnouncementScope(str, Enum):
    MY_STUDENTS = "my_students"
    COHORT = "cohort"
    PROGRAM = "program"
    CENTER = "center"


class Conversation(BaseDocument):
    # Always exactly two ids: one Student, one staff member. Order is not
    # meaningful - every query filters with `$all`, never relies on position.
    participant_ids: list[str]
    last_message_at: datetime
    last_message_preview: str = ""

    class Settings:
        name = "conversations"
        indexes = [[("participant_ids", 1)]]


class Message(BaseDocument):
    conversation_id: Annotated[str, Indexed()]
    sender_id: Annotated[str, Indexed()]
    sender_role: Role
    body: str
    # `None` until the OTHER participant reads it - a sender's own messages
    # are never "unread" to themselves.
    read_at: Optional[datetime] = None
    # Set only on a message delivered as part of a bulk announcement - see
    # this module's own docstring. `None` for an ordinary 1:1 message.
    announcement_id: Annotated[Optional[str], Indexed()] = None

    class Settings:
        name = "messages"
        indexes = [[("conversation_id", 1), ("created_at", 1)]]


class Announcement(BaseDocument):
    sender_id: Annotated[str, Indexed()]
    sender_role: Role
    scope: AnnouncementScope
    # A Cohort id, a Program id, or `None` for "my students"/"center".
    scope_id: Optional[str] = None
    body: str
    recipient_count: int
    # A client-generated idempotency key (see `MessagingService
    # .send_announcement`'s own docstring) - unique, so a double-click/
    # retried request can never deliver the same announcement twice.
    client_token: Annotated[str, Indexed(unique=True)]

    class Settings:
        name = "announcements"
        indexes = [[("sender_id", 1), ("created_at", -1)]]
