"""Cohort CRUD, stage/teacher/program validation, and the per-stage Cohort
calendar lifecycle. A Cohort never copies Program curriculum or
`TopicContent` - its per-stage `CalendarTemplate` (kind=COHORT) starts as a
copy of the Program stage's own schedule (same topic ids, not duplicated
content) and can diverge from it independently; see
`CalendarTemplateRepository.get_effective_schedule` for how a reader resolves
which one actually applies.
"""

from datetime import datetime
import re
from typing import Optional

from app.core.exceptions import ForbiddenException, NotFoundException, UnprocessableException
from app.models.calendar_template import CalendarDayEntry, CalendarTemplate, TemplateKind
from app.models.cohort import Cohort, CohortStatus, CohortTeacherAssignment
from app.models.enrollment import Enrollment, EnrollmentSource, EnrollmentStatus, LateJoinMode
from app.models.program import ProgramLevelDefinition
from app.models.user import Role, User
from app.repositories.calendar_template_repository import CalendarTemplateRepository
from app.schema.cohort import CohortCreate, CohortUpdate
from app.schema.enrollment import CohortStudentCandidate, EnrollStudentsRequest
from app.services.program_service import ProgramService, get_program_service
from app.utils.time import utcnow


def has_cohort_access(cohort: Cohort, user: User) -> bool:
    """Center Admin reaches every Cohort in the center. A Teacher reaches a
    Cohort only as its Lead Teacher or as an assigned additional teacher -
    being assigned grants access to the Cohort's roster and its stages'
    published lessons, never to the authoring Topic's private library or
    edit rights (those stay governed by `TopicContentService`'s own
    ownership checks, untouched by this)."""
    if user.role == Role.ADMINISTRATOR:
        return True
    if user.role != Role.TEACHER:
        return False
    if cohort.lead_teacher_id == user.id:
        return True
    return any(assignment.teacher_id == user.id for assignment in cohort.teacher_assignments)


def accessible_stage_ids(cohort: Cohort, user: User) -> list[str]:
    """Which of the Cohort's stages this caller may see. Center Admin and the
    Lead Teacher see every stage the Cohort runs; an additional teacher sees
    only the stages they were explicitly assigned (empty list on their
    assignment means every stage)."""
    if user.role == Role.ADMINISTRATOR or cohort.lead_teacher_id == user.id:
        return list(cohort.program_level_ids)
    assignment = next((a for a in cohort.teacher_assignments if a.teacher_id == user.id), None)
    if assignment is None:
        return []
    return list(assignment.program_level_ids) or list(cohort.program_level_ids)


class CohortService:
    def __init__(self, calendar_template_repository: CalendarTemplateRepository, program_service: ProgramService) -> None:
        self.calendar_template_repository = calendar_template_repository
        self.program_service = program_service

    async def _resolve_stages(self, program_id: str, program_level_ids: list[str]) -> list[ProgramLevelDefinition]:
        program = await self.program_service.get_active(program_id)
        available = {level.id: level for level in self.program_service.effective_levels(program)}
        unknown = set(program_level_ids) - set(available)
        if unknown:
            raise UnprocessableException(f"Unknown Program stage id(s): {', '.join(sorted(unknown))}")
        return [available[level_id] for level_id in program_level_ids]

    async def _validate_teacher(self, teacher_id: str) -> User:
        teacher = await User.find_one({"_id": teacher_id, "is_deleted": False})
        if teacher is None or teacher.role != Role.TEACHER:
            raise UnprocessableException(f"{teacher_id} is not an active Teacher")
        return teacher

    async def create(self, body: CohortCreate, admin: User) -> Cohort:
        stages = await self._resolve_stages(body.program_id, body.program_level_ids)
        await self._validate_teacher(body.lead_teacher_id)
        for assignment in body.teacher_assignments:
            await self._validate_teacher(assignment.teacher_id)
            unknown = set(assignment.program_level_ids) - set(body.program_level_ids)
            if unknown:
                raise UnprocessableException("A teacher assignment references a stage outside this Cohort")

        cohort = Cohort(
            name=body.name,
            program_id=body.program_id,
            program_level_ids=body.program_level_ids,
            lead_teacher_id=body.lead_teacher_id,
            teacher_assignments=[
                CohortTeacherAssignment(teacher_id=a.teacher_id, program_level_ids=a.program_level_ids)
                for a in body.teacher_assignments
            ],
            start_date=body.start_date,
            end_date=body.end_date,
            created_by_id=admin.id,
        )
        await cohort.insert()
        for stage in stages:
            await self._ensure_cohort_calendar(cohort, stage)
        return cohort

    async def _ensure_cohort_calendar(self, cohort: Cohort, stage: ProgramLevelDefinition) -> CalendarTemplate:
        existing = await self.calendar_template_repository.get_active_cohort_template(cohort.id, stage.id)
        if existing is not None:
            return existing
        # Seed from the Program stage's own shared schedule as a baseline the
        # Cohort can freely diverge from - same topic ids, never duplicated
        # TopicContent, and the Program's own calendar is left untouched.
        baseline = await self.calendar_template_repository.get_active_standard_for_program_level(cohort.program_id, stage.id)
        template = CalendarTemplate(
            name=f"{cohort.name} · {stage.name}",
            kind=TemplateKind.COHORT,
            owner_admin_id=cohort.created_by_id,
            program_id=cohort.program_id,
            program_level_id=stage.id,
            cohort_id=cohort.id,
            level=stage.difficulty_level,
            is_active=True,
            duration_days=baseline.duration_days if baseline else 30,
            rest_days=list(baseline.rest_days) if baseline else [],
            days=[
                CalendarDayEntry(day=entry.day, topic_ids=list(entry.topic_ids), custom_minutes=dict(entry.custom_minutes))
                for entry in (baseline.days if baseline else [])
            ],
        )
        await template.insert()
        return template

    async def get_for_admin(self, cohort_id: str) -> Cohort:
        cohort = await Cohort.find_one({"_id": cohort_id, "is_deleted": False})
        if cohort is None:
            raise NotFoundException("Cohort not found")
        return cohort

    async def get_for_user(self, cohort_id: str, user: User) -> Cohort:
        cohort = await self.get_for_admin(cohort_id)
        if not has_cohort_access(cohort, user):
            raise ForbiddenException("You are not assigned to this Cohort")
        return cohort

    async def list_for_admin(self) -> list[Cohort]:
        return await Cohort.find({"is_deleted": False}).sort("-created_at").to_list()

    async def list_for_teacher(self, teacher: User) -> list[Cohort]:
        return await Cohort.find({
            "is_deleted": False,
            "$or": [
                {"lead_teacher_id": teacher.id},
                {"teacher_assignments.teacher_id": teacher.id},
            ],
        }).sort("-created_at").to_list()

    async def update(self, cohort_id: str, body: CohortUpdate, admin: User) -> Cohort:
        cohort = await self.get_for_user(cohort_id, admin)
        changes = body.model_dump(exclude_unset=True)
        # A teacher runs the dates and availability of the Batch they are
        # assigned to. Staffing remains a Center Admin decision.
        if admin.role != Role.ADMINISTRATOR and ({"lead_teacher_id", "teacher_assignments"} & set(changes)):
            raise ForbiddenException("Only a Center Admin can change the teaching team")
        if "lead_teacher_id" in changes:
            await self._validate_teacher(changes["lead_teacher_id"])
        if "teacher_assignments" in changes:
            for assignment in body.teacher_assignments or []:
                await self._validate_teacher(assignment.teacher_id)
                unknown = set(assignment.program_level_ids) - set(cohort.program_level_ids)
                if unknown:
                    raise UnprocessableException("A teacher assignment references a stage outside this Cohort")
            changes["teacher_assignments"] = [
                CohortTeacherAssignment(teacher_id=a.teacher_id, program_level_ids=a.program_level_ids)
                for a in (body.teacher_assignments or [])
            ]
        for key, value in changes.items():
            setattr(cohort, key, value)
        await cohort.save()
        return cohort

    async def archive(self, cohort_id: str) -> None:
        cohort = await self.get_for_admin(cohort_id)
        cohort.status = CohortStatus.ARCHIVED
        cohort.is_deleted = True
        await cohort.save()

    async def get_roster(self, cohort_id: str, user: User) -> list[tuple[Enrollment, Optional[User]]]:
        cohort = await self.get_for_user(cohort_id, user)
        stage_ids = set(accessible_stage_ids(cohort, user))
        enrollments = await Enrollment.find({
            "cohort_id": cohort.id,
            "is_deleted": False,
            "status": EnrollmentStatus.ACTIVE.value,
        }).to_list()
        enrollments = [e for e in enrollments if e.program_level_id in stage_ids]
        students = await User.find({"_id": {"$in": [e.student_id for e in enrollments]}, "is_deleted": False}).to_list()
        students_by_id = {s.id: s for s in students}
        return [(enrollment, students_by_id.get(enrollment.student_id)) for enrollment in enrollments]

    async def get_roster_page(
        self, cohort_id: str, user: User, *, search: Optional[str], page: int, limit: int,
    ) -> tuple[list[tuple[Enrollment, Optional[User]]], int]:
        """Roster membership is Enrollment-owned, so filter after proving
        Cohort/stage access.  This preserves the stage scope for additional
        Teachers and gives the UI a stable, bounded list."""
        roster = await self.get_roster(cohort_id, user)
        term = (search or "").strip().casefold()
        if term:
            roster = [item for item in roster if item[1] and (
                term in item[1].full_name.casefold() or term in item[1].email.casefold()
            )]
        roster.sort(key=lambda item: ((item[1].full_name if item[1] else "").casefold(), item[0].id))
        total = len(roster)
        start = (page - 1) * limit
        return roster[start:start + limit], total

    async def list_student_candidates(
        self,
        cohort_id: str,
        admin: User,
        *,
        search: Optional[str],
        academic_level_id: Optional[str],
        unassigned_only: bool,
        page: int,
        limit: int,
    ) -> tuple[list[CohortStudentCandidate], int]:
        """Candidate selection source for one assigned Cohort.

        Students already actively enrolled in this Cohort are excluded here
        (and appear in the roster endpoint), rather than being silently
        selectable again.  We deliberately expose academic placement, never
        subscription-plan labels.
        """
        cohort = await self.get_for_user(cohort_id, admin)
        permitted_stages = set(accessible_stage_ids(cohort, admin))
        active = await Enrollment.find({
            "cohort_id": cohort.id, "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }).to_list()
        enrolled_ids = {item.student_id for item in active}
        stages = {stage.id: stage for stage in await self._resolve_stages(cohort.program_id, cohort.program_level_ids)}
        active_anywhere = await Enrollment.find({
            "is_deleted": False, "status": EnrollmentStatus.ACTIVE.value,
        }, projection_model=Enrollment).to_list()
        active_student_ids = {item.student_id for item in active_anywhere}
        term = (search or "").strip().casefold()
        query: dict = {"role": Role.STUDENT.value, "is_deleted": False}
        if admin.role != Role.ADMINISTRATOR:
            query["created_by_id"] = admin.id
        if enrolled_ids:
            query["_id"] = {"$nin": list(enrolled_ids)}
        if academic_level_id:
            if academic_level_id not in permitted_stages:
                raise ForbiddenException("You are not assigned to that Cohort stage")
            stage = stages.get(academic_level_id)
            if stage is None:
                raise UnprocessableException("That academic level is not offered by this Cohort")
            query["$or"] = [
                {"program_level_id": academic_level_id},
                {"assigned_level": stage.difficulty_level.value},
            ]
        if unassigned_only:
            query["_id"] = {"$nin": list(set(query.get("_id", {}).get("$nin", [])) | active_student_ids)}
            query["program_id"] = None
            query["program_level_id"] = None
            query["assigned_level"] = None
        if term:
            pattern = re.escape(term)
            text_clause = {"$or": [
                {"full_name": {"$regex": pattern, "$options": "i"}},
                {"email": {"$regex": pattern, "$options": "i"}},
            ]}
            if "$or" in query:
                query["$and"] = [{"$or": query.pop("$or")}, text_clause]
            else:
                query.update(text_clause)
        total = await User.find(query).count()
        students = await User.find(query).sort(User.full_name).skip((page - 1) * limit).limit(limit).to_list()
        candidates: list[CohortStudentCandidate] = []
        for student in students:
            stage = stages.get(student.program_level_id or "")
            academic_id = stage.id if stage else student.program_level_id
            academic_name = stage.name if stage else (student.assigned_level.value.title() if student.assigned_level else None)
            is_unassigned = student.id not in active_student_ids and not student.program_id and not student.program_level_id and not student.assigned_level
            candidates.append(CohortStudentCandidate(
                id=student.id, full_name=student.full_name, email=student.email,
                academic_level_id=academic_id, academic_level_name=academic_name,
                is_unassigned=is_unassigned,
            ))
        return candidates, total

    async def enroll_students(self, cohort_id: str, body: EnrollStudentsRequest, admin: User) -> tuple[list[Enrollment], list[str], list[Enrollment]]:
        cohort = await self.get_for_user(cohort_id, admin)
        if body.program_level_id not in cohort.program_level_ids:
            raise UnprocessableException("That stage is not offered by this Cohort")
        if body.program_level_id not in accessible_stage_ids(cohort, admin):
            raise ForbiddenException("You are not assigned to that Cohort stage")
        student_query: dict = {"_id": {"$in": body.student_ids}, "is_deleted": False}
        if admin.role != Role.ADMINISTRATOR:
            student_query["created_by_id"] = admin.id
        students = await User.find(student_query).to_list()
        found_ids = {s.id for s in students}
        missing = set(body.student_ids) - found_ids
        if missing:
            # A Teacher's candidate scope is deliberately limited to student
            # accounts they created. Do not turn an out-of-scope account into
            # an "unknown" validation detail that encourages retrying IDs.
            if admin.role != Role.ADMINISTRATOR:
                raise ForbiddenException("You can enroll only students you manage")
            raise UnprocessableException(f"Unknown student id(s): {', '.join(sorted(missing))}")
        non_students = [s.id for s in students if s.role != Role.STUDENT]
        if non_students:
            raise UnprocessableException("Only Student accounts can be enrolled")

        joined_at = body.joined_at or utcnow()
        created: list[Enrollment] = []
        already_enrolled: list[str] = []
        processed: list[Enrollment] = []
        for student in students:
            existing = await Enrollment.find_one({
                "student_id": student.id,
                "cohort_id": cohort.id,
                "is_deleted": False,
                "status": EnrollmentStatus.ACTIVE.value,
            })
            if existing is not None:
                already_enrolled.append(student.id)
                processed.append(existing)
                continue
            enrollment = Enrollment(
                student_id=student.id,
                program_id=cohort.program_id,
                program_level_id=body.program_level_id,
                cohort_id=cohort.id,
                status=EnrollmentStatus.ACTIVE,
                source=EnrollmentSource.COHORT,
                joined_at=joined_at,
                late_join_mode=body.late_join_mode,
                created_by_id=admin.id,
            )
            await enrollment.insert()
            created.append(enrollment)
            processed.append(enrollment)
        return created, already_enrolled, processed

    async def remove_student(self, cohort_id: str, student_id: str) -> None:
        enrollment = await Enrollment.find_one({
            "cohort_id": cohort_id, "student_id": student_id, "is_deleted": False,
        })
        if enrollment is None:
            raise NotFoundException("This student is not enrolled in this Cohort")
        enrollment.status = EnrollmentStatus.WITHDRAWN
        enrollment.is_deleted = True
        await enrollment.save()


def get_cohort_service() -> CohortService:
    return CohortService(CalendarTemplateRepository(), get_program_service())
