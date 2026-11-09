"""Idempotent, non-destructive backfill: create an `Enrollment` row for every
existing Student who already has `program_id` + `program_level_id` set
directly on their `User` document, so the new Cohort/Enrollment layer has a
real row to read for students who enrolled before it existed. This NEVER
reads or writes `User.program_id`/`program_level_id` themselves - they stay
exactly as they are, and every other current read path (sessions, progress,
homework, exercise attempts) keeps using them unchanged.

Only runs against a LOCAL_DEV_MODE backend (`app.core.config.get_settings()`
already refuses production-looking configuration under that flag - see
`app/core/local_guard.py`). Safe to re-run: a student who already has a
matching, non-deleted Enrollment (any status) for their current
`program_id`/`program_level_id` is skipped, not duplicated.

    python scripts/local/backfill_enrollments.py             # dry run (default, no writes)
    python scripts/local/backfill_enrollments.py --apply      # actually create the missing rows
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def run(apply: bool) -> int:
    from app.core.config import get_settings
    from app.core.database import close_db, init_db
    from app.models.enrollment import Enrollment, EnrollmentSource, EnrollmentStatus
    from app.models.user import Role, User

    settings = get_settings()
    if not settings.LOCAL_DEV_MODE:
        raise SystemExit("Refusing: this script only runs with LOCAL_DEV_MODE=true (see backend/.env).")

    await init_db()
    try:
        candidates = await User.find({
            "role": Role.STUDENT.value,
            "is_deleted": False,
            "program_id": {"$ne": None},
            "program_level_id": {"$ne": None},
        }).to_list()
        print(f"Students with a direct Program/stage assignment: {len(candidates)}")

        to_create = 0
        skipped = 0
        for student in candidates:
            existing = await Enrollment.find_one({
                "student_id": student.id,
                "program_id": student.program_id,
                "program_level_id": student.program_level_id,
                "cohort_id": None,
                "is_deleted": False,
            })
            if existing is not None:
                skipped += 1
                continue
            to_create += 1
            print(
                f"  {'WOULD CREATE' if not apply else 'CREATING'} enrollment: "
                f"student={student.email} program_id={student.program_id} program_level_id={student.program_level_id}"
            )
            if apply:
                await Enrollment(
                    student_id=student.id,
                    program_id=student.program_id,
                    program_level_id=student.program_level_id,
                    cohort_id=None,
                    status=EnrollmentStatus.ACTIVE,
                    source=EnrollmentSource.LEGACY_BACKFILL,
                    joined_at=student.program_start_date or student.created_at,
                    created_by_id=student.created_by_id or student.id,
                ).insert()

        print(f"\n{'Created' if apply else 'Would create'}: {to_create}  Already present (skipped): {skipped}")
        if not apply:
            print("DRY RUN - no writes were made. Re-run with --apply to write.")
        return 0
    finally:
        await close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run (the default).")
    args = parser.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
