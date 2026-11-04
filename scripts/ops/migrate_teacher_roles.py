"""PROMPT-049: inspect the REAL Tamakkan production database and (once explicitly approved) convert historical
teacher accounts stored as `role: "administrator"` to `role: "teacher"`.

This script is deliberately independent of `app.core.config`/`app.core.database` - it never reads `backend/.env`,
never touches the app's own DB settings, and connects ONLY to whatever `--uri`/`TAMAKKAN_PROD_MONGODB_URI` you
give it. It talks to MongoDB directly via `pymongo`, bypassing Beanie entirely, specifically so running this
script can never trigger Beanie's automatic index creation/sync against a production database as a side effect
of just connecting.

The connection string is read once into a local variable and is NEVER printed, logged, written to a file, or
included in any exception message this script raises itself (a driver-level connection error could still echo
it - if that happens, it prints to your terminal only, never to a file this script writes).

SAFETY MODEL
------------
- Every run needs --db, the exact database name, as a sanity check independent of what's embedded in the URI.
  The script fails loudly if the live connection's actual db name doesn't match.
- `inspect` (default) is READ-ONLY. It cannot write anything, by construction - it has no write call in its
  code path at all.
- `migrate` defaults to --dry-run and requires EVERY account's fate to be named explicitly on the command line
  (--keep-admin, one or more --convert) - nothing is converted by inference, by name-matching, or by "everyone
  except the ones I named". An administrator account that is listed in neither bucket makes the whole run
  refuse to proceed (fail closed), so a newly-created or forgotten account can never be silently swept into
  either outcome.
- `--apply` additionally requires --confirm-db (must repeat the exact db name) and --backup-ref (a short string
  identifying where you already put the backup - this script will not take the backup for you, see below).
- The only field ever written is `role`, and only for exactly the user ids resolved from --convert. created_by_id,
  topic/student/class/session ownership, ids, passwords, tokens - nothing else is read for writing, nothing else
  is touched.
- Idempotent: converting an account that is already "teacher" (or "keeping" one already "administrator") is a
  no-op - running --apply twice does not change anything about the second run's outcome.

USAGE
-----
Read-only inspection (list every current `administrator` account + their owned student/topic counts):

    export TAMAKKAN_PROD_MONGODB_URI="<the uri - never put this in your shell history file if you can avoid it>"
    python scripts/ops/migrate_teacher_roles.py inspect --db tamakkan

Dry run of a proposed migration (no writes, ever, regardless of flags):

    python scripts/ops/migrate_teacher_roles.py migrate --db tamakkan \\
        --keep-admin center-admin@example.com \\
        --convert randa@example.com --convert mahmoud@example.com --convert abdulrahman@example.com

Apply (only after the account list above has been explicitly reviewed and approved, and a backup exists):

    python scripts/ops/migrate_teacher_roles.py migrate --db tamakkan --apply \\
        --confirm-db tamakkan --backup-ref "mongodump 2026-10-20 -> /root/tamakkan-recovery/pre-teacher-migration-20261020" \\
        --keep-admin center-admin@example.com \\
        --convert randa@example.com --convert mahmoud@example.com --convert abdulrahman@example.com

PROMPT-051: `center-admin-setup` is the richer, one-shot variant - create the new Center Admin, convert named
Teacher accounts, and deactivate named legacy accounts, all in one plan with a single dry-run/apply gate.
Confirms the existing Root account unchanged (read-only) and captures each convert target's full ownership
snapshot (students/topics/units/classes) both before and after, printed for comparison. The new account's
password is NEVER a CLI argument (shell history/process-list exposure) - it is read only from the environment
variable named by --new-admin-password-env (default TAMAKKAN_NEW_ADMIN_PASSWORD) and hashed with the
application's own `app.core.security.hash_password` (plain bcrypt call, no DB/Beanie dependency) before ever
touching Mongo.

    export TAMAKKAN_PROD_MONGODB_URI="..."
    export TAMAKKAN_NEW_ADMIN_PASSWORD="..."
    python scripts/ops/migrate_teacher_roles.py center-admin-setup --db tamakkan \\
        --new-admin-name mohammad --new-admin-email mohammad_tamakkan@tamakkan.com \\
        --convert rand@tamakkan.com --convert teacher@example.com \\
        --deactivate admin-test@example.com --deactivate verify_admin@confide.app

    # ... review the dry-run output, then:
    python scripts/ops/migrate_teacher_roles.py center-admin-setup --db tamakkan --apply \\
        --confirm-db tamakkan --backup-ref "<path/timestamp from the backup step>" \\
        --new-admin-name mohammad --new-admin-email mohammad_tamakkan@tamakkan.com \\
        --convert rand@tamakkan.com --convert teacher@example.com \\
        --deactivate admin-test@example.com --deactivate verify_admin@confide.app
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pymongo import AsyncMongoClient

# backend/ - the root `app` package's parent, for the one call in `center-admin-setup` that needs
# `app.core.security.hash_password` (a pure, DB-free bcrypt call, not the app's DB layer).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Collections this script ever opens - see each model's `Settings.name` (`app/models/user.py`,
# `app/models/topic_content.py`, `app/models/unit.py`, `app/models/live_class.py`,
# `app/models/refresh_token.py`). `inspect` and `migrate` only ever touch the first two; `center-admin-setup`
# additionally reads units/classes (ownership snapshot only, never written) and writes refresh_tokens only to
# revoke sessions for an account it is deactivating - never anything else.
USERS_COLLECTION = "users"
TOPICS_COLLECTION = "topic_contents"
UNITS_COLLECTION = "units"
CLASSES_COLLECTION = "live_classes"
REFRESH_TOKENS_COLLECTION = "refresh_tokens"

# A production db should never collide with a name scripts/local/tamakkan-local.ps1 would ever use for a local
# database - every local db name there starts with "tamakkan_" (see its own refusal: "a non-local database name").
# This is a tripwire, not the real safety boundary (--confirm-db is): it exists so a copy-pasted local --uri
# can't silently "succeed" against a throwaway local db and look like it migrated production.
_SUSPICIOUS_DB_NAME_MARKERS = ("test", "local", "integration", "_dev", "staging")

# Fields ever read from a user document - password_hash, refresh/reset tokens, email-verification hashes: none
# of them are in this list, so none of them are ever fetched into this process's memory in the first place.
USER_PROJECTION = {
    "_id": 1,
    "full_name": 1,
    "email": 1,
    "role": 1,
    "is_active": 1,
    "created_at": 1,
    "created_by_id": 1,
}


@dataclass
class AdminAccount:
    id: str
    full_name: str
    email: str
    role: str
    is_active: bool
    created_at: Optional[datetime]
    owned_students: int
    owned_topics: int


def _uri_from_args(args: argparse.Namespace) -> str:
    uri = args.uri or os.environ.get("TAMAKKAN_PROD_MONGODB_URI")
    if not uri:
        raise SystemExit("No URI given - pass --uri or set TAMAKKAN_PROD_MONGODB_URI (never hardcode it here).")
    return uri


def _check_db_name_sanity(db_name: str) -> None:
    lowered = db_name.lower()
    if any(marker in lowered for marker in _SUSPICIOUS_DB_NAME_MARKERS):
        raise SystemExit(
            f"Refusing: database name '{db_name}' looks like a local/test database "
            f"(matched one of {_SUSPICIOUS_DB_NAME_MARKERS}). If this really is production, rename this check's "
            f"expectations deliberately - do not just bypass it."
        )


async def _connect(uri: str, db_name: str):
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[db_name]
    # Proves the connection actually reaches a server (fails fast and clearly instead of hanging on the first
    # real query) without reading or writing a single document.
    await client.admin.command("ping")
    actual_name = db.name
    if actual_name != db_name:
        await client.close()
        raise SystemExit(f"Connected, but the resolved database name ('{actual_name}') does not match --db ('{db_name}').")
    return client, db


async def _collection_names(db) -> set[str]:
    return set(await db.list_collection_names())


async def _owned_counts(db, user_id: str) -> tuple[int, int]:
    students = await db[USERS_COLLECTION].count_documents(
        {"created_by_id": user_id, "role": "student", "is_deleted": {"$ne": True}}
    )
    topics = await db[TOPICS_COLLECTION].count_documents({"created_by_id": user_id, "is_deleted": {"$ne": True}})
    return students, topics


@dataclass
class OwnershipSnapshot:
    user_id: str
    email: str
    role: str
    students: int
    topics: int
    units: int
    classes: int


async def _ownership_snapshot(db, user_id: str, email: str, role: str) -> OwnershipSnapshot:
    """Every count this script ever reports for a convert target - read-only, taken both before and after
    --apply so the two can be compared and must come out identical (see `center-admin-setup`)."""
    students, topics = await _owned_counts(db, user_id)
    units = await db[UNITS_COLLECTION].count_documents({"created_by_id": user_id, "is_deleted": {"$ne": True}})
    classes = await db[CLASSES_COLLECTION].count_documents({"created_by_id": user_id, "is_deleted": {"$ne": True}})
    return OwnershipSnapshot(user_id=user_id, email=email, role=role, students=students, topics=topics, units=units, classes=classes)


def _print_snapshot(s: OwnershipSnapshot) -> None:
    print(f"  - {s.email:<36} id={s.user_id}  role={s.role}  students={s.students}  topics={s.topics}  units={s.units}  classes={s.classes}")


async def _find_user_by_email(db, email: str) -> Optional[dict]:
    return await db[USERS_COLLECTION].find_one({"email": email}, USER_PROJECTION)


async def _find_root(db) -> list[dict]:
    """Every current `role: "root"` account - read-only. PROMPT-051 requires confirming there is exactly one
    and that this script never touches it; it appears here only to be reported on, never written to."""
    return await db[USERS_COLLECTION].find({"role": "root"}, USER_PROJECTION).to_list(length=None)


async def _list_administrators(db) -> list[AdminAccount]:
    cursor = db[USERS_COLLECTION].find({"role": "administrator"}, USER_PROJECTION).sort("created_at", 1)
    accounts: list[AdminAccount] = []
    async for doc in cursor:
        students, topics = await _owned_counts(db, str(doc["_id"]))
        accounts.append(
            AdminAccount(
                id=str(doc["_id"]),
                full_name=doc.get("full_name", ""),
                email=doc.get("email", ""),
                role=doc.get("role", ""),
                is_active=bool(doc.get("is_active", True)),
                created_at=doc.get("created_at"),
                owned_students=students,
                owned_topics=topics,
            )
        )
    return accounts


def _fmt_date(value: Optional[datetime]) -> str:
    return value.date().isoformat() if value else "unknown"


def _print_account_line(a: AdminAccount) -> None:
    print(f"  - {a.full_name:<28} {a.email:<36} id={a.id}  active={a.is_active}  created={_fmt_date(a.created_at)}  "
          f"students_owned={a.owned_students}  topics_owned={a.owned_topics}")


async def cmd_inspect(args: argparse.Namespace) -> int:
    uri = _uri_from_args(args)
    _check_db_name_sanity(args.db)
    client, db = await _connect(uri, args.db)
    try:
        names = await _collection_names(db)
        print(f"DATABASE: {args.db}")
        print(f"collections present: {USERS_COLLECTION} = {USERS_COLLECTION in names}, {TOPICS_COLLECTION} = {TOPICS_COLLECTION in names}")
        if USERS_COLLECTION not in names:
            raise SystemExit(f"Refusing: no '{USERS_COLLECTION}' collection in '{args.db}' - this doesn't look like a Tamakkan database.")
        total_users = await db[USERS_COLLECTION].count_documents({})
        total_students = await db[USERS_COLLECTION].count_documents({"role": "student", "is_deleted": {"$ne": True}})
        total_topics = await db[TOPICS_COLLECTION].count_documents({"is_deleted": {"$ne": True}}) if TOPICS_COLLECTION in names else 0
        print(f"total users: {total_users}   students: {total_students}   topics: {total_topics}")
        print()
        admins = await _list_administrators(db)
        print(f"CURRENT ADMINISTRATOR ACCOUNTS ({len(admins)}):")
        for a in admins:
            _print_account_line(a)
        return 0
    finally:
        await client.close()


async def cmd_migrate(args: argparse.Namespace) -> int:
    uri = _uri_from_args(args)
    _check_db_name_sanity(args.db)
    keep_email = (args.keep_admin or "").strip().lower() or None
    convert_emails = {e.strip().lower() for e in (args.convert or []) if e.strip()}
    if keep_email and keep_email in convert_emails:
        raise SystemExit(f"'{keep_email}' is in both --keep-admin and --convert - pick one.")

    client, db = await _connect(uri, args.db)
    try:
        admins = await _list_administrators(db)
        by_email = {a.email.lower(): a for a in admins}

        # Any CURRENT administrator not named in either bucket blocks the whole run - fail closed rather than
        # guess. An email already converted on a previous run (now role=teacher) is intentionally NOT in `admins`
        # here, so re-running the exact same command is not blocked by its own past success.
        unnamed = [a for a in admins if a.email.lower() not in convert_emails and a.email.lower() != keep_email]
        if unnamed:
            print("REFUSING: the following administrator account(s) were not named in --keep-admin or --convert:")
            for a in unnamed:
                _print_account_line(a)
            print("\nName every current administrator account explicitly - nothing is converted by inference.")
            return 1

        keep = by_email.get(keep_email) if keep_email else None
        if keep_email and keep is None:
            # Not in `admins` could mean "doesn't exist" OR "already converted to teacher by mistake" - either
            # way, --keep-admin promising to keep it as administrator is no longer true, so this must stop.
            raise SystemExit(f"--keep-admin '{keep_email}' is not a current administrator account - check the email and its role.")

        # Resolved by direct lookup (any role), not from `admins` - so an email already converted to teacher on a
        # prior run reads as "already done" (a no-op), not "not found", making --apply safely re-runnable.
        to_convert: list[AdminAccount] = []
        already_teacher: list[str] = []
        wrong_role: list[str] = []
        for email in sorted(convert_emails):
            if email in by_email:
                to_convert.append(by_email[email])
                continue
            doc = await db[USERS_COLLECTION].find_one({"email": email}, USER_PROJECTION)
            if doc is None:
                wrong_role.append(f"{email} (no such user)")
            elif doc.get("role") == "teacher":
                already_teacher.append(email)
            else:
                wrong_role.append(f"{email} (role={doc.get('role')})")
        if wrong_role:
            raise SystemExit(f"--convert email(s) not usable: {', '.join(wrong_role)}")
        if already_teacher:
            print(f"already role=teacher (no-op, previous run likely converted these): {', '.join(already_teacher)}")

        total_students = await db[USERS_COLLECTION].count_documents({"role": "student", "is_deleted": {"$ne": True}})
        total_topics = await db[TOPICS_COLLECTION].count_documents({"is_deleted": {"$ne": True}})

        print(f"DATABASE:\n  {args.db}\n")
        print("KEEP AS ADMINISTRATOR:")
        if keep:
            _print_account_line(keep)
        else:
            print("  (none named - pass --keep-admin <email>)")
        print("\nCONVERT TO TEACHER:")
        if to_convert:
            for a in to_convert:
                _print_account_line(a)
        else:
            print("  (none named - pass one or more --convert <email>)")
        print(f"\nUNCHANGED STUDENTS:\n  {total_students}")
        print(f"\nUNCHANGED TOPICS:\n  {total_topics}")

        if not args.apply:
            print("\nDRY RUN - no database writes were made. Re-run with --apply (plus --confirm-db and --backup-ref) to write.")
            return 0

        if not keep:
            raise SystemExit("--apply requires --keep-admin to resolve to a real administrator account.")
        if args.confirm_db != args.db:
            raise SystemExit("--apply requires --confirm-db to exactly repeat --db, as a last explicit confirmation.")
        if not args.backup_ref:
            raise SystemExit("--apply requires --backup-ref: where the pre-migration backup is stored. Take it first.")

        print(f"\nBacked up at: {args.backup_ref}")
        print("APPLYING...")
        now = datetime.now(timezone.utc)
        changed = 0
        for a in to_convert:
            if a.role == "teacher":
                continue  # already converted - idempotent re-run
            result = await db[USERS_COLLECTION].update_one(
                {"_id": a.id, "role": "administrator"},  # compare-and-set: only ever flips administrator -> teacher
                {"$set": {"role": "teacher", "updated_at": now}},
            )
            changed += result.modified_count
        print(f"converted {changed} account(s) administrator -> teacher")

        # Post-write verification, read-only.
        post = await _list_administrators(db)
        post_by_email = {a.email.lower(): a for a in post}
        if set(post_by_email) != {keep_email}:
            print(f"WARNING: administrator accounts after migration = {sorted(post_by_email)} (expected only {{'{keep_email}'}})")
        for email in convert_emails:
            doc = await db[USERS_COLLECTION].find_one({"email": email}, {"role": 1})
            if doc is None or doc.get("role") != "teacher":
                print(f"WARNING: '{email}' did not end up as role=teacher - check manually.")
        print("post-migration verification complete.")
        return 0
    finally:
        await client.close()


async def cmd_center_admin_setup(args: argparse.Namespace) -> int:
    """PROMPT-051: create the new Center Admin, convert named Teacher accounts, deactivate named legacy
    accounts - one plan, one dry-run/apply gate. See the module docstring for the full safety model; this
    adds only: Root is read and reported on, never written; the new account's password never appears on the
    command line (env var only); and every convert target's ownership (students/topics/units/classes) is
    snapshotted before the write and re-checked identical after.
    """
    uri = _uri_from_args(args)
    _check_db_name_sanity(args.db)
    new_email = args.new_admin_email.strip().lower()
    convert_emails = [e.strip().lower() for e in (args.convert or []) if e.strip()]
    deactivate_emails = [e.strip().lower() for e in (args.deactivate or []) if e.strip()]
    overlap = (set(convert_emails) | set(deactivate_emails)) & {new_email}
    if overlap:
        raise SystemExit(f"--new-admin-email collides with --convert/--deactivate: {overlap}")
    if set(convert_emails) & set(deactivate_emails):
        raise SystemExit("An email is in both --convert and --deactivate - pick one.")

    keep_root_email = args.keep_root_email.strip().lower()

    client, db = await _connect(uri, args.db)
    try:
        roots = await _find_root(db)
        root = next((r for r in roots if r.get("email", "").lower() == keep_root_email), None)
        print(f"DATABASE:\n  {args.db}\n")
        print("ROOT:")
        if root is None:
            print(f"  REFUSING: --keep-root-email '{keep_root_email}' does not match any role=root account.")
            for r in roots:
                print(f"  - {r.get('full_name','')} {r.get('email','')} id={r['_id']} role={r.get('role')}")
            return 1
        if not root.get("is_active", True):
            print(f"  REFUSING: '{keep_root_email}' (the account to keep) is currently inactive - check this is really the right one.")
            return 1
        print(f"  - {root.get('full_name',''):<28} {root.get('email',''):<36} id={root['_id']}  active={root.get('is_active', True)}  -> unchanged (this command never writes it)")

        # Every OTHER role=root account must be named in --deactivate explicitly - fail closed, same policy as
        # every other account bucket in this script. Deactivating a root only ever sets is_active=False here
        # (see the shared deactivate loop below) - role, email and password are never touched for ANY account.
        other_roots = [r for r in roots if r["_id"] != root["_id"]]
        unnamed_roots = [r for r in other_roots if r.get("email", "").lower() not in deactivate_emails]
        if unnamed_roots:
            print("\n  REFUSING: the following other role=root account(s) were not named in --deactivate:")
            for r in unnamed_roots:
                print(f"    - {r.get('full_name','')} {r.get('email','')} id={r['_id']}")
            print("  Name every other root account explicitly in --deactivate (role stays root; only is_active flips).")
            return 1

        existing_new = await _find_user_by_email(db, new_email)
        print(f"\nCREATE ADMINISTRATOR:\n  {args.new_admin_name} <{new_email}>")
        if existing_new is not None:
            print(f"  REFUSING: a user with email '{new_email}' already exists (id={existing_new['_id']}, role={existing_new.get('role')}).")
            return 1

        to_convert: list[dict] = []
        for email in convert_emails:
            doc = await _find_user_by_email(db, email)
            if doc is None:
                raise SystemExit(f"--convert '{email}' not found.")
            if doc.get("role") not in ("administrator", "teacher"):
                raise SystemExit(f"--convert '{email}' has role={doc.get('role')!r}, expected administrator (or already teacher).")
            to_convert.append(doc)

        to_deactivate: list[dict] = []
        for email in deactivate_emails:
            doc = await _find_user_by_email(db, email)
            if doc is None:
                raise SystemExit(f"--deactivate '{email}' not found.")
            to_deactivate.append(doc)

        print("\nCONVERT TO TEACHER:")
        before_snapshots: list[OwnershipSnapshot] = []
        for doc in to_convert:
            snap = await _ownership_snapshot(db, str(doc["_id"]), doc.get("email", ""), doc.get("role", ""))
            before_snapshots.append(snap)
            _print_snapshot(snap)
            if doc.get("role") == "teacher":
                print(f"    (already role=teacher - no-op)")

        def _print_deactivate(doc: dict) -> None:
            already = not doc.get("is_active", True)
            print(f"  - {doc.get('full_name',''):<28} {doc.get('email',''):<36} id={doc['_id']}  role={doc.get('role')}  "
                  f"currently_active={doc.get('is_active', True)}{'  (already inactive - no-op)' if already else ''}")

        print("\nINACTIVE LEGACY ROOTS (role stays root, is_active -> false):")
        legacy_roots = [d for d in to_deactivate if d.get("role") == "root"]
        for doc in legacy_roots:
            _print_deactivate(doc)
        print("\nINACTIVE LEGACY ADMINS (role stays administrator, is_active -> false):")
        for doc in [d for d in to_deactivate if d.get("role") != "root"]:
            _print_deactivate(doc)

        print("\nOWNERSHIP CHANGES:\n  NONE - this script never writes created_by_id, on any collection.")
        print("\nSTUDENT REASSIGNMENTS:\n  NONE")
        print("\nTOPIC REASSIGNMENTS:\n  NONE")

        if not args.apply:
            print("\nDRY RUN - no database writes were made.")
            print("Re-run with --apply (plus --confirm-db, --backup-ref, and the new password in the env var named by --new-admin-password-env) to write.")
            return 0

        if args.confirm_db != args.db:
            raise SystemExit("--apply requires --confirm-db to exactly repeat --db, as a last explicit confirmation.")
        if not args.backup_ref:
            raise SystemExit("--apply requires --backup-ref: where the pre-migration backup is stored. Take it first.")
        password = os.environ.get(args.new_admin_password_env)
        if not password:
            raise SystemExit(f"--apply requires the new admin's password in the {args.new_admin_password_env} environment variable.")

        # Imported here, not at module scope: this is the only function in the whole script that needs the
        # app's own code at all, and only for this one pure, DB-free hashing call.
        sys.path.insert(0, str(_BACKEND_ROOT))
        from app.core.security import hash_password  # noqa: PLC0415

        print(f"\nBacked up at: {args.backup_ref}")
        print("APPLYING...")
        now = datetime.now(timezone.utc)

        new_id = str(uuid.uuid4())
        await db[USERS_COLLECTION].insert_one({
            "_id": new_id,
            "email": new_email,
            "password_hash": hash_password(password),
            "full_name": args.new_admin_name,
            "role": "administrator",
            "is_active": True,
            "created_by_id": str(root["_id"]),
            "email_verification_required": False,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        })
        password = None  # dereference as soon as it's no longer needed
        print(f"created administrator: {args.new_admin_name} <{new_email}> id={new_id}")

        converted = 0
        for doc in to_convert:
            result = await db[USERS_COLLECTION].update_one(
                {"_id": doc["_id"], "role": "administrator"},  # compare-and-set: administrator -> teacher only
                {"$set": {"role": "teacher", "updated_at": now}},
            )
            converted += result.modified_count
        print(f"converted {converted} account(s) administrator -> teacher")

        deactivated = 0
        for doc in to_deactivate:
            result = await db[USERS_COLLECTION].update_one(
                {"_id": doc["_id"], "is_active": True},
                {"$set": {"is_active": False, "updated_at": now}},
            )
            deactivated += result.modified_count
            await db[REFRESH_TOKENS_COLLECTION].update_many(
                {"user_id": str(doc["_id"]), "revoked": False},
                {"$set": {"revoked": True, "updated_at": now}},
            )
        print(f"deactivated {deactivated} account(s)")

        # Post-write verification, read-only.
        print("\nPOST-MIGRATION VERIFICATION:")
        root_after = await db[USERS_COLLECTION].find_one({"_id": root["_id"]}, USER_PROJECTION)
        root_ok = (
            root_after is not None and root_after.get("role") == "root"
            and root_after.get("email") == root.get("email") and root_after.get("is_active", True) is True
        )
        print(f"  root ({root.get('email')}) unchanged and active: {root_ok}")

        new_admins = await db[USERS_COLLECTION].find({"email": new_email}).to_list(length=None)
        print(f"  mohammad exists exactly once: {len(new_admins) == 1}; role=administrator: {new_admins[0].get('role') == 'administrator' if new_admins else False}; "
              f"active: {new_admins[0].get('is_active') if new_admins else None}")

        for before in before_snapshots:
            after_doc = await _find_user_by_email(db, before.email)
            if after_doc is None:
                print(f"  {before.email}: MISSING AFTER MIGRATION")
                continue
            after = await _ownership_snapshot(db, str(after_doc["_id"]), before.email, after_doc.get("role", ""))
            ok = (after.role == "teacher" and after.user_id == before.user_id and after.students == before.students
                  and after.topics == before.topics and after.units == before.units and after.classes == before.classes)
            print(f"  {before.email}: role={after.role} id_unchanged={after.user_id == before.user_id} "
                  f"students={after.students}(was {before.students}) topics={after.topics}(was {before.topics}) "
                  f"units={after.units}(was {before.units}) classes={after.classes}(was {before.classes}) -> {'OK' if ok else 'MISMATCH'}")

        for doc in to_deactivate:
            after_doc = await _find_user_by_email(db, doc.get("email", ""))
            is_inactive = after_doc is not None and after_doc.get("is_active") is False
            role_unchanged = after_doc is not None and after_doc.get("role") == doc.get("role")
            print(f"  {doc.get('email','')}: inactive={is_inactive}  role_unchanged({doc.get('role')})={role_unchanged}")

        total_users_after = await db[USERS_COLLECTION].count_documents({})
        print(f"  total users now: {total_users_after}")
        print("post-migration verification complete.")
        return 0
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uri", default=None, help="Mongo connection URI. Prefer TAMAKKAN_PROD_MONGODB_URI instead - never put this in shell history.")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="Read-only: list every administrator account and its owned student/topic counts.")
    i.add_argument("--db", required=True, help="Exact database name - verified against the live connection.")
    i.set_defaults(func=cmd_inspect)

    m = sub.add_parser("migrate", help="Dry-run by default; --apply performs the real, idempotent role conversion.")
    m.add_argument("--db", required=True)
    m.add_argument("--keep-admin", default=None, help="Email of the one account to keep as role=administrator.")
    m.add_argument("--convert", action="append", default=[], help="Email to convert administrator -> teacher. Repeatable.")
    m.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run (the default).")
    m.add_argument("--confirm-db", default=None, help="Required with --apply: must repeat --db exactly.")
    m.add_argument("--backup-ref", default=None, help="Required with --apply: where the pre-migration backup is stored.")
    m.set_defaults(func=cmd_migrate)

    c = sub.add_parser("center-admin-setup", help="PROMPT-051: create the new Center Admin, convert named Teachers, deactivate named legacy accounts.")
    c.add_argument("--db", required=True)
    c.add_argument("--new-admin-name", required=True)
    c.add_argument("--new-admin-email", required=True)
    c.add_argument("--new-admin-password-env", default="TAMAKKAN_NEW_ADMIN_PASSWORD",
                    help="Name of the env var holding the new admin's password. Never pass the password itself as a CLI argument.")
    c.add_argument("--keep-root-email", required=True, help="The one role=root account to confirm unchanged. Every OTHER role=root account must be named in --deactivate.")
    c.add_argument("--convert", action="append", default=[], help="Email to convert administrator -> teacher. Repeatable.")
    c.add_argument("--deactivate", action="append", default=[], help="Email to set is_active=false (role untouched) - legacy admins AND legacy root accounts both go here. Repeatable.")
    c.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run (the default).")
    c.add_argument("--confirm-db", default=None, help="Required with --apply: must repeat --db exactly.")
    c.add_argument("--backup-ref", default=None, help="Required with --apply: where the pre-migration backup is stored.")
    c.set_defaults(func=cmd_center_admin_setup)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
