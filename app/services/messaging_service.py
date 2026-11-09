"""In-platform Teacher/Student/Admin messaging (Teacher Workspace Increment
3). See `app.models.message`'s own module docstring for the Conversation/
Message/Announcement split. Every recipient list is resolved server-side
from the caller's own existing authorization (Cohort/Enrollment reach, or
Center Admin's center-wide scope) - a client never supplies a raw recipient
id list for a bulk send, only a scope to resolve. No AI is ever involved;
a message body is exactly what its sender typed.
"""

from datetime import timedelta
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.cohort import Cohort
from app.models.enrollment import Enrollment, EnrollmentStatus
from app.models.message import Announcement, AnnouncementScope, Conversation, Message
from app.models.program import Program
from app.models.user import Role, User
from app.schema.message import (
    AnnouncementSummary,
    ConversationSummary,
    MessagePage,
    MessagePublic,
)
from app.services.cohort_service import CohortService, accessible_stage_ids
from app.utils.time import utcnow

# A generous ceiling, not a normal-use limit - catches a spam script or a
# runaway retry loop, not a teacher legitimately messaging several students
# in a row.
MAX_MESSAGES_PER_MINUTE = 30


class MessagingService:
    def __init__(self, cohort_service: CohortService) -> None:
        self.cohort_service = cohort_service

    async def _enforce_rate_limit(self, sender_id: str) -> None:
        since = utcnow() - timedelta(minutes=1)
        count = await Message.find({"sender_id": sender_id, "created_at": {"$gte": since}}).count()
        if count >= MAX_MESSAGES_PER_MINUTE:
            raise UnprocessableException("You're sending messages too quickly - wait a moment and try again.")

    async def _get_student(self, student_id: str) -> User:
        student = await User.find_one({"_id": student_id, "is_deleted": False, "role": Role.STUDENT.value})
        if student is None:
            raise NotFoundException("Student not found")
        return student

    async def _get_or_create_conversation(self, staff_id: str, student_id: str) -> Conversation:
        existing = await Conversation.find_one({"participant_ids": {"$all": [staff_id, student_id]}})
        if existing is not None:
            return existing
        conversation = Conversation(participant_ids=[staff_id, student_id], last_message_at=utcnow())
        await conversation.insert()
        return conversation

    async def _append_message(
        self, conversation: Conversation, sender: User, body: str, announcement_id: Optional[str] = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation.id, sender_id=sender.id, sender_role=sender.role,
            body=body, announcement_id=announcement_id,
        )
        await message.insert()
        conversation.last_message_at = message.created_at
        conversation.last_message_preview = body[:200]
        await conversation.save()
        return message

    async def send_new_message(self, sender: User, student_id: str, body: str) -> MessagePublic:
        """Teacher/Admin only - a Student replies to an existing conversation
        instead (`reply` below); they never cold-start one to an arbitrary
        staff member."""
        if sender.role not in (Role.TEACHER, Role.ADMINISTRATOR, Role.ROOT):
            raise ForbiddenException("Only a teacher or administrator can start a new conversation")
        await self._enforce_rate_limit(sender.id)
        student = await self._get_student(student_id)
        if not await self.cohort_service.may_reach_student(sender, student):
            raise ForbiddenException("You are not authorized to message this student")
        conversation = await self._get_or_create_conversation(sender.id, student.id)
        message = await self._append_message(conversation, sender, body)
        return MessagePublic(
            id=message.id, created_at=message.created_at, updated_at=message.updated_at,
            conversation_id=conversation.id, sender_id=sender.id, sender_role=sender.role,
            sender_name=sender.full_name, body=body, read_at=None, is_mine=True,
        )

    async def reply(self, sender: User, conversation_id: str, body: str) -> MessagePublic:
        await self._enforce_rate_limit(sender.id)
        conversation = await Conversation.find_one({"_id": conversation_id, "is_deleted": False})
        if conversation is None or sender.id not in conversation.participant_ids:
            raise ForbiddenException("You are not part of this conversation")
        message = await self._append_message(conversation, sender, body)
        return MessagePublic(
            id=message.id, created_at=message.created_at, updated_at=message.updated_at,
            conversation_id=conversation.id, sender_id=sender.id, sender_role=sender.role,
            sender_name=sender.full_name, body=body, read_at=None, is_mine=True,
        )

    async def list_inbox(self, caller: User, page: int, limit: int) -> tuple[list[ConversationSummary], int]:
        total = await Conversation.find({"participant_ids": caller.id, "is_deleted": False}).count()
        conversations = await Conversation.find({"participant_ids": caller.id, "is_deleted": False}) \
            .sort("-last_message_at").skip((page - 1) * limit).limit(limit).to_list()
        other_ids = {next(pid for pid in c.participant_ids if pid != caller.id) for c in conversations}
        others = {u.id: u for u in await User.find({"_id": {"$in": list(other_ids)}}).to_list()} if other_ids else {}
        summaries = []
        for conversation in conversations:
            other_id = next(pid for pid in conversation.participant_ids if pid != caller.id)
            other = others.get(other_id)
            unread = await Message.find({
                "conversation_id": conversation.id, "sender_id": {"$ne": caller.id}, "read_at": None,
            }).count()
            summaries.append(ConversationSummary(
                id=conversation.id, created_at=conversation.created_at, updated_at=conversation.updated_at,
                other_participant_id=other_id, other_participant_name=other.full_name if other else None,
                other_participant_role=other.role if other else None,
                last_message_at=conversation.last_message_at, last_message_preview=conversation.last_message_preview,
                unread_count=unread,
            ))
        return summaries, total

    async def list_messages(self, caller: User, conversation_id: str, page: int, limit: int) -> MessagePage:
        conversation = await Conversation.find_one({"_id": conversation_id, "is_deleted": False})
        if conversation is None or caller.id not in conversation.participant_ids:
            raise ForbiddenException("You are not part of this conversation")
        total = await Message.find({"conversation_id": conversation_id}).count()
        messages = await Message.find({"conversation_id": conversation_id}) \
            .sort("-created_at").skip((page - 1) * limit).limit(limit).to_list()
        sender_ids = {m.sender_id for m in messages}
        senders = {u.id: u for u in await User.find({"_id": {"$in": list(sender_ids)}}).to_list()} if sender_ids else {}
        # Mark every unread message from the OTHER participant as read, now
        # that this participant has actually fetched this page - a bulk
        # update, not an N+1 loop.
        await Message.find({"conversation_id": conversation_id, "sender_id": {"$ne": caller.id}, "read_at": None}) \
            .update({"$set": {"read_at": utcnow()}})
        items = [
            MessagePublic(
                id=m.id, created_at=m.created_at, updated_at=m.updated_at, conversation_id=m.conversation_id,
                sender_id=m.sender_id, sender_role=m.sender_role, sender_name=senders.get(m.sender_id).full_name if senders.get(m.sender_id) else None,
                body=m.body, read_at=m.read_at, announcement_id=m.announcement_id, is_mine=(m.sender_id == caller.id),
            )
            for m in messages
        ]
        return MessagePage(items=items, total=total, page=page, limit=limit)

    # --- Bulk announcements ------------------------------------------------

    async def _resolve_recipients(self, caller: User, scope: AnnouncementScope, scope_id: Optional[str]) -> list[User]:
        if scope == AnnouncementScope.CENTER:
            if caller.role not in (Role.ADMINISTRATOR, Role.ROOT):
                raise ForbiddenException("Only a Center Admin can broadcast to the whole center")
            return await User.find({"role": Role.STUDENT.value, "is_deleted": False}).to_list()

        if scope == AnnouncementScope.MY_STUDENTS:
            if caller.role != Role.TEACHER:
                raise ForbiddenException("Only a Teacher can message \"my students\" - an Admin uses Cohort/Program/Center")
            rows, _total = await self.cohort_service.list_my_students(caller, limit=10_000)
            return [row["user"] for row in rows]

        if scope == AnnouncementScope.COHORT:
            if not scope_id:
                raise UnprocessableException("A Cohort must be specified for this scope")
            cohort = await Cohort.find_one({"_id": scope_id, "is_deleted": False})
            if cohort is None:
                raise NotFoundException("Cohort not found")
            if caller.role not in (Role.ADMINISTRATOR, Role.ROOT):
                roster = await self.cohort_service.get_roster(scope_id, caller)
                return [student for _enrollment, student in roster if student is not None]
            enrollments = await Enrollment.find({"cohort_id": scope_id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value}).to_list()
            students = await User.find({"_id": {"$in": [e.student_id for e in enrollments]}, "is_deleted": False}).to_list()
            return students

        if scope == AnnouncementScope.PROGRAM:
            if not scope_id:
                raise UnprocessableException("A Program must be specified for this scope")
            program = await Program.find_one({"_id": scope_id, "is_deleted": False})
            if program is None:
                raise NotFoundException("Program not found")
            if caller.role in (Role.ADMINISTRATOR, Role.ROOT):
                enrollments = await Enrollment.find({"program_id": scope_id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value}).to_list()
                return await User.find({"_id": {"$in": [e.student_id for e in enrollments]}, "is_deleted": False}).to_list()
            cohorts = [c for c in await self.cohort_service.list_for_teacher(caller) if c.program_id == scope_id]
            if not cohorts:
                raise ForbiddenException("You are not assigned to any Cohort in this Program")
            student_ids: set[str] = set()
            for cohort in cohorts:
                stage_ids = set(accessible_stage_ids(cohort, caller))
                enrollments = await Enrollment.find({
                    "cohort_id": cohort.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
                }).to_list()
                student_ids |= {e.student_id for e in enrollments if e.program_level_id in stage_ids}
            if not student_ids:
                return []
            return await User.find({"_id": {"$in": list(student_ids)}, "is_deleted": False}).to_list()

        raise UnprocessableException("Unknown recipient scope")

    async def preview_recipients(self, caller: User, scope: AnnouncementScope, scope_id: Optional[str]) -> int:
        students = await self._resolve_recipients(caller, scope, scope_id)
        return len({s.id for s in students})

    async def send_announcement(
        self, caller: User, scope: AnnouncementScope, scope_id: Optional[str], body: str, client_token: str,
    ) -> AnnouncementSummary:
        if caller.role not in (Role.TEACHER, Role.ADMINISTRATOR, Role.ROOT):
            raise ForbiddenException("Only a teacher or administrator can send an announcement")
        await self._enforce_rate_limit(caller.id)
        students = await self._resolve_recipients(caller, scope, scope_id)
        unique_students = {s.id: s for s in students}.values()

        announcement = Announcement(
            sender_id=caller.id, sender_role=caller.role, scope=scope, scope_id=scope_id,
            body=body, recipient_count=len(unique_students), client_token=client_token,
        )
        try:
            await announcement.insert()
        except DuplicateKeyError:
            # The exact same compose action was already delivered - return
            # that original result rather than sending a second time.
            existing = await Announcement.find_one({"client_token": client_token})
            if existing is None:
                raise
            return AnnouncementSummary(
                id=existing.id, created_at=existing.created_at, updated_at=existing.updated_at,
                sender_id=existing.sender_id, sender_role=existing.sender_role, scope=existing.scope,
                scope_id=existing.scope_id, body=existing.body, recipient_count=existing.recipient_count,
            )

        for student in unique_students:
            conversation = await self._get_or_create_conversation(caller.id, student.id)
            await self._append_message(conversation, caller, body, announcement_id=announcement.id)

        return AnnouncementSummary(
            id=announcement.id, created_at=announcement.created_at, updated_at=announcement.updated_at,
            sender_id=caller.id, sender_role=caller.role, scope=scope, scope_id=scope_id,
            body=body, recipient_count=len(unique_students),
        )

    async def list_announcements(self, caller: User, page: int, limit: int) -> tuple[list[AnnouncementSummary], int]:
        total = await Announcement.find({"sender_id": caller.id, "is_deleted": False}).count()
        rows = await Announcement.find({"sender_id": caller.id, "is_deleted": False}) \
            .sort("-created_at").skip((page - 1) * limit).limit(limit).to_list()
        program_ids = {r.scope_id for r in rows if r.scope == AnnouncementScope.PROGRAM and r.scope_id}
        cohort_ids = {r.scope_id for r in rows if r.scope == AnnouncementScope.COHORT and r.scope_id}
        programs = {p.id: p.name for p in await Program.find({"_id": {"$in": list(program_ids)}}).to_list()} if program_ids else {}
        cohorts = {c.id: c.name for c in await Cohort.find({"_id": {"$in": list(cohort_ids)}}).to_list()} if cohort_ids else {}
        items = [
            AnnouncementSummary(
                id=r.id, created_at=r.created_at, updated_at=r.updated_at, sender_id=r.sender_id,
                sender_role=r.sender_role, scope=r.scope, scope_id=r.scope_id,
                scope_name=programs.get(r.scope_id) if r.scope == AnnouncementScope.PROGRAM else cohorts.get(r.scope_id) if r.scope == AnnouncementScope.COHORT else None,
                body=r.body, recipient_count=r.recipient_count,
            )
            for r in rows
        ]
        return items, total


def get_messaging_service() -> MessagingService:
    from app.services.cohort_service import get_cohort_service

    return MessagingService(get_cohort_service())
