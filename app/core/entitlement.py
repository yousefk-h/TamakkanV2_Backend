"""The one definition of what lesson content a Student may reach.

This lives here rather than as a private method on `TopicContentService`
because it turned out to have three callers that must not disagree: reading
a topic (`TopicContentService`), submitting a recording *against* a topic
(`RecordingSubmissionService`), and starting a Mariam session on one
(`LiveSessionService`). The read side was gated in Kanban cards #14/#15
before the other two were, which meant a Student who correctly got a 403
from `GET /topics/{id}` could still write against that same topic and get
its title back in the response. Two (or three) copies of this rule is how
that happens again, so there is exactly one.

The calendar *is* the catalogue now: a topic is reachable at all only if it
appears somewhere in the Student's own `CalendarTemplate` (Standard: the
level's active template; Pro: the template they were assigned -
`app.models.calendar_template`), and only once its scheduled day has been
reached (or it's already done) - both questions are answered by the same
`ProgressService.get_lesson_schedule` computation
(`resolve_topic_statuses`), so there's no separate "is it entitled" check
to keep in sync with a "is it unlocked" one.

Entitlement is a Student concept. Administrators and Root are authoring and
reviewing content, not consuming a plan, so they are never gated by it - see
`requester_may_open`.
"""

from app.models.user import Role, User
from app.schema.challenge import LessonDayState
from app.services.progress_service import ProgressService


async def requester_may_open(requester: User, topic_id: str, progress_service: ProgressService) -> bool:
    """Administrator/Root: always. Student: the topic must be part of their
    own calendar (Standard's level, or Pro's assigned template) AND its
    scheduled day must have already been reached - a topic that isn't in
    the calendar at all and one that's merely locked both resolve here the
    same way, since neither is currently reachable.
    """
    if requester.role != Role.STUDENT:
        return True
    statuses = await progress_service.get_topic_statuses(requester)
    status = statuses.get(topic_id)
    return status is not None and status != LessonDayState.LOCKED
